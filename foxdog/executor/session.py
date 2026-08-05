"""持久 shell 会话（codex unified_exec 同款）：交互式 nc / gdb / ftp 靠它。

open 启动持久 shell 进程（Windows PowerShell 7，POSIX 优先 `bash -i` 否则 `sh -i`），
stdin/stdout 管道；后台 reader task 持续把输出泵进 per-session 缓冲区
（asyncio task，与 BackgroundRegistry 同款模式，非线程）。
read 按游标只回新增输出，可等 wait_ms 收集；close kill 进程树。
"""
from __future__ import annotations

import asyncio
import base64
import shutil
from collections import deque
from dataclasses import dataclass, field

from .process import IS_WINDOWS, ProcessOutputDecoder, _kill_tree, windows_shell_executable

BUFFER_LIMIT = 256 * 1024  # per-session 缓冲区字符上限


def _session_argv() -> list[str]:
    if IS_WINDOWS:
        return [
            windows_shell_executable(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-NoExit",
            "-Command",
            "-",
        ]
    bash = shutil.which("bash")
    return [bash or shutil.which("sh") or "sh", "-i"]


@dataclass
class ShellSession:
    session_id: str
    proc: asyncio.subprocess.Process
    via_runtime: bool = False  # True=docker 容器内 shell（不做 pwsh base64-iex 包装）
    buffer: deque[str] = field(default_factory=deque)
    buffered_chars: int = 0
    cursor: int = 0  # 已读字符数（read 只回游标后的新增输出）
    eof: bool = False
    _reader: asyncio.Task | None = None

    def append(self, text: str) -> None:
        self.buffer.append(text)
        self.buffered_chars += len(text)
        while self.buffered_chars > BUFFER_LIMIT and self.buffer:
            dropped = self.buffer.popleft()
            self.buffered_chars -= len(dropped)
            self.cursor = max(0, self.cursor - len(dropped))

    def drain_new(self) -> str:
        """取走游标后的新增输出。"""
        text = "".join(self.buffer)
        if self.cursor >= len(text):
            return ""
        new = text[self.cursor :]
        self.cursor = len(text)
        return new

    def tail(self, limit: int = 4000) -> str:
        text = "".join(self.buffer)
        return text[-limit:]


class SessionRegistry:
    """session_id → ShellSession；CLI 结束时 close_all。"""

    def __init__(self) -> None:
        self._sessions: dict[str, ShellSession] = {}

    def ids(self) -> list[str]:
        return list(self._sessions)

    def get(self, session_id: str) -> ShellSession | None:
        return self._sessions.get(session_id)

    async def open(self, session_id: str, cwd: str | None = None, runtime=None, env: dict[str, str] | None = None) -> ShellSession:
        if session_id in self._sessions:
            raise ValueError(f"会话已存在: {session_id}")
        via_runtime = runtime is not None and getattr(runtime, "kind", "local") != "local"
        if via_runtime:
            # docker / docker-persistent：容器内交互 bash（镜像 bash，POSIX 语义）
            try:
                proc = await runtime.spawn(["bash", "-i"], cwd=cwd, env=env)
            except FileNotFoundError as exc:
                raise ValueError(f"无法在容器内启动 bash: {exc}") from exc
        else:
            argv = _session_argv()
            kwargs: dict = {}
            if not IS_WINDOWS:
                kwargs["start_new_session"] = True
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                    **kwargs,
                )
            except FileNotFoundError as exc:
                raise ValueError(f"无法启动 shell（{argv[0]}）: {exc}") from exc
            if IS_WINDOWS and proc.stdin is not None:
                init = (
                    "$PSStyle.OutputRendering='PlainText';"
                    "$ProgressPreference='SilentlyContinue';"
                    "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n"
                )
                proc.stdin.write(init.encode("utf-8"))
                await proc.stdin.drain()
        sess = ShellSession(session_id=session_id, proc=proc, via_runtime=via_runtime)

        async def _pump() -> None:
            assert proc.stdout is not None
            decoder = ProcessOutputDecoder()
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                text = decoder.feed(chunk)
                if text:
                    sess.append(text)
            tail = decoder.feed(b"", final=True)
            if tail:
                sess.append(tail)
            sess.eof = True

        sess._reader = asyncio.create_task(_pump())
        self._sessions[session_id] = sess
        return sess

    async def write(self, session_id: str, data: str) -> None:
        sess = self._require(session_id)
        if sess.proc.stdin is None or sess.eof:
            raise ValueError(f"会话已结束: {session_id}")
        if not data.endswith("\n"):
            data += "\n"  # 自动补换行（回车）
        # Windows 乱码修复：pwsh -Command - 把重定向 stdin 按系统 ANSI（如 GBK）
        # 解码，UTF-8 中文命令还没执行就先变成乱码（2026-08-03 全量回归翻车）。
        # BOM/中途改 [Console]::InputEncoding 均无效（解码器在启动时锁定）。
        # 非 ASCII 命令一律包成 base64-iex（包装层纯 ASCII，pwsh 内部按 UTF-8 还原）。
        # 仅宿主 pwsh 会话生效；docker 容器内 POSIX bash 不套这层（via_runtime）。
        # 已知取舍：会话里跑着交互式子程序（nc/gdb/python REPL）时非 ASCII 输入
        # 会被子程序当作包装文本——但改前它收到的本就是乱码，只赚不亏。
        if IS_WINDOWS and not sess.via_runtime and not data.isascii():
            b64 = base64.b64encode(data.encode("utf-8")).decode("ascii")
            data = f"iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))\n"
        sess.proc.stdin.write(data.encode("utf-8", errors="replace"))
        await sess.proc.stdin.drain()

    async def read(self, session_id: str, wait_ms: int = 0) -> str:
        """非阻塞读新增输出；wait_ms>0 时在该窗口内持续收集后一并返回。"""
        sess = self._require(session_id)
        if wait_ms <= 0:
            return sess.drain_new()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_ms / 1000
        out = ""
        while True:
            out += sess.drain_new()
            if loop.time() >= deadline or sess.eof:
                break
            await asyncio.sleep(0.05)
        out += sess.drain_new()
        return out

    async def close(self, session_id: str) -> str:
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            raise ValueError(f"未知会话: {session_id}")
        if sess.proc.returncode is None:
            # 先礼貌 exit（docker exec 的容器内 bash 收到后自然退出，避免 client
            # 被杀但容器进程残留），再等 1s，最后才 kill 进程树
            if sess.proc.stdin is not None and not sess.eof:
                try:
                    sess.proc.stdin.write(b"exit\n")
                    await sess.proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(sess.proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                await _kill_tree(sess.proc)
        if sess._reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(sess._reader), timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                sess._reader.cancel()
        return sess.tail()

    async def close_all(self) -> None:
        for sid in list(self._sessions):
            try:
                await self.close(sid)
            except ValueError:
                pass

    def _require(self, session_id: str) -> ShellSession:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise ValueError(f"未知会话: {session_id}")
        return sess

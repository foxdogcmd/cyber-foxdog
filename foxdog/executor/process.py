"""子进程执行器（借鉴 CyberStrikeAI executor.go 语义，Python 实现）。

- 数组传参（POSIX `sh -c` / Windows PowerShell 7），绝不 shell=True 拼接。
- 流式读 stdout/stderr 合并。
- 无输出空闲超时（默认 300s，到点 kill 进程树）+ 总时限（默认 1800s）。
- 输出超 max_output（默认 64KB）截断，全文 spill 到 CAS，返回 handle。
"""
from __future__ import annotations

import asyncio
import codecs
import locale
import os
import signal
import shutil
import subprocess  # noqa: S404 - 仅用于 DEVNULL 常量
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_TOTAL_TIMEOUT = 1800.0
DEFAULT_MAX_OUTPUT = 64 * 1024


def _output_fallback_encodings() -> tuple[str, ...]:
    """Return likely native process encodings after UTF-8, in priority order."""
    candidates: list[str] = []
    if IS_WINDOWS:
        try:
            import ctypes

            candidates.extend((f"cp{ctypes.windll.kernel32.GetOEMCP()}", f"cp{ctypes.windll.kernel32.GetACP()}"))
        except (AttributeError, OSError):
            pass
    candidates.append(locale.getpreferredencoding(False))
    unique: list[str] = []
    for encoding in candidates:
        if not encoding:
            continue
        try:
            canonical = codecs.lookup(encoding).name
        except LookupError:
            continue
        if canonical != "utf-8" and canonical not in unique:
            unique.append(canonical)
    return tuple(unique)


def decode_process_output(data: bytes, fallback_encodings: tuple[str, ...] | None = None) -> str:
    """Decode command output as UTF-8 first, then the host's native code page."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    fallbacks = _output_fallback_encodings() if fallback_encodings is None else fallback_encodings
    for encoding in fallbacks:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    replacement_encoding = fallbacks[0] if fallbacks else "utf-8"
    try:
        return data.decode(replacement_encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


class ProcessOutputDecoder:
    """Incremental decoder for streams that may mix UTF-8 and a native code page."""

    def __init__(self, fallback_encodings: tuple[str, ...] | None = None) -> None:
        self._fallback_encodings = fallback_encodings
        self._pending = bytearray()

    def feed(self, data: bytes, *, final: bool = False) -> str:
        self._pending.extend(data)
        if not self._pending:
            return ""

        first_non_ascii = next((i for i, byte in enumerate(self._pending) if byte >= 0x80), None)
        if first_non_ascii is None:
            text = self._pending.decode("ascii")
            self._pending.clear()
            return text

        prefix = self._pending[:first_non_ascii].decode("ascii")
        if first_non_ascii:
            del self._pending[:first_non_ascii]

        if final:
            tail = decode_process_output(bytes(self._pending), self._fallback_encodings)
            self._pending.clear()
            return prefix + tail

        try:
            text = self._pending.decode("utf-8")
        except UnicodeDecodeError as exc:
            incomplete_tail = exc.reason == "unexpected end of data" and exc.end == len(self._pending)
            if incomplete_tail:
                complete = bytes(self._pending[: exc.start])
                if complete:
                    prefix += complete.decode("utf-8")
                    del self._pending[: exc.start]
                return prefix
        else:
            self._pending.clear()
            return prefix + text

        fallbacks = _output_fallback_encodings() if self._fallback_encodings is None else self._fallback_encodings
        for encoding in fallbacks:
            try:
                text = self._pending.decode(encoding)
            except LookupError:
                continue
            except UnicodeDecodeError as exc:
                incomplete_tail = exc.end == len(self._pending) and "incomplete" in exc.reason
                if incomplete_tail:
                    complete = bytes(self._pending[: exc.start])
                    if complete:
                        prefix += complete.decode(encoding)
                        del self._pending[: exc.start]
                    return prefix
                continue
            self._pending.clear()
            return prefix + text

        replacement_encoding = fallbacks[0] if fallbacks else "utf-8"
        try:
            text = self._pending.decode(replacement_encoding, errors="replace")
        except LookupError:
            text = self._pending.decode("utf-8", errors="replace")
        self._pending.clear()
        return prefix + text


@dataclass
class ExecResult:
    exit_code: int
    output: str  # 截断后的输出
    truncated: bool = False
    handle: str | None = None  # CAS 句柄（仅截断时非空）
    timed_out: bool = False  # 总时限
    idle_timed_out: bool = False  # 无输出空闲超时
    error: str | None = None


def windows_shell_executable() -> str:
    """Prefer PowerShell 7; retain Windows PowerShell only as a compatibility fallback."""
    return shutil.which("pwsh") or shutil.which("powershell") or "pwsh"


def shell_argv(command: str) -> list[str]:
    if IS_WINDOWS:
        return [
            windows_shell_executable(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    return ["sh", "-c", command]


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """尽力杀掉整个进程树。"""
    if IS_WINDOWS:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await killer.wait()
        except Exception:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def _pump(
    proc: asyncio.subprocess.Process,
    idle_timeout: float,
    total_timeout: float,
    max_output: int,
    cas_store: Callable[[bytes], str] | None,
    on_output: Callable[[str], None] | None,
) -> ExecResult:
    """流式读输出 + 空闲/总超时 kill + 截断 spill（run_shell / run_argv 共用）。"""
    buf = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    timed_out = idle_timed_out = False
    stream_decoder = ProcessOutputDecoder()
    decoded_parts: list[str] = []
    assert proc.stdout is not None
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=min(idle_timeout, remaining))
            except asyncio.TimeoutError:
                if loop.time() >= deadline:
                    timed_out = True
                else:
                    idle_timed_out = True
                break
            if not chunk:  # EOF
                break
            buf.extend(chunk)
            text = stream_decoder.feed(chunk)
            if text:
                decoded_parts.append(text)
                if on_output:
                    on_output(text)
        tail = stream_decoder.feed(b"", final=True)
        if tail:
            decoded_parts.append(tail)
            if on_output:
                on_output(tail)
        if timed_out or idle_timed_out:
            await _kill_tree(proc)
        await proc.wait()
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise

    full = bytes(buf)
    decoded = "".join(decoded_parts)
    normalized = decoded.encode("utf-8")
    truncated = len(full) > max_output
    handle: str | None = None
    if truncated:
        if cas_store:
            handle = cas_store(normalized)
        output = normalized[:max_output].decode("utf-8", errors="ignore")
    else:
        output = decoded
    return ExecResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        output=output,
        truncated=truncated,
        handle=handle,
        timed_out=timed_out,
        idle_timed_out=idle_timed_out,
    )


async def run_argv(
    argv: list[str],
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    cas_store: Callable[[bytes], str] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ExecResult:
    """数组传参执行（YAML 配方用）：绝不经过 shell，参数零拼接防注入。"""
    kwargs: dict = {}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True  # 便于 killpg 杀整棵树
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
            **kwargs,
        )
    except FileNotFoundError as exc:
        return ExecResult(exit_code=127, output="", error=str(exc))
    return await _pump(proc, idle_timeout, total_timeout, max_output, cas_store, on_output)


async def run_shell(
    command: str,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    cas_store: Callable[[bytes], str] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ExecResult:
    """同步执行一条 shell 命令，返回 ExecResult。"""
    argv = shell_argv(command)
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # 便于 killpg 杀整棵树
    shell_coro = asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
        **kwargs,
    )
    try:
        proc = await shell_coro
    except FileNotFoundError as exc:
        return ExecResult(exit_code=127, output="", error=str(exc))
    return await _pump(proc, idle_timeout, total_timeout, max_output, cas_store, on_output)

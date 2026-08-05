"""外部 CLI 引擎驱动（muteki solver/cli_driver.py 思路：拼 argv、流式收割、marker 协议）。

Claude Code headless：`claude -p "<prompt>" --output-format stream-json --verbose
--safe-mode --no-session-persistence --dangerously-skip-permissions`。Claude Code 2.1.220
要求 print + stream-json 同时开启 --verbose；safe-mode 禁止自动加载工作区内不可信的
CLAUDE.md/hooks/plugins，且不持久化 race 会话。--dangerously-skip-permissions 说明：race 引擎必须自主跑 CTF 工具
（nmap/curl/python），headless 下任何权限询问都会卡死——信任模型与 foxdog yolo
一致（CTF 信任靶机、防误伤本机）。默认经 Docker runtime 执行，工作目录是
run 工作区 scratch/ 子目录，并直接使用镜像内的 CTF 工具。

收割：① marker 正则 FOUND_FLAG=(\\S+)；② 兜底 flag_format 正则扫全文。
命中 → 引擎完整输出归档 CAS（溯源证据）→ gate.submit（正常四重校验；引擎 prompt
不经 orchestrator，taint_scan 只扫内置工具调用，prompt 里的格式串不会误伤）。
取消：runtime 模式走 DockerRuntime 的 pidfile + 进程组终止；显式宿主兼容模式
仍由 _kill_tree 杀进程树。
facts 收割本轮不做（注释标注后续可加）。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from ..executor.process import _kill_tree
from ..executor.runtime import ExecutionRuntime
from ..gate.flag_gate import FlagGate
from ..protocol.types import SwarmNote
from ..store.workspace import Workspace

_MARKER_RE = re.compile(r"FOUND_FLAG=(\S+)")

PROMPT_TEMPLATE = """\
你在独立解一道 CTF 题（异构引擎竞速，另有 agent 并行在打）。

题目：{objective}
{target_line}{format_line}工作目录是当前 scratch 目录（可自由写文件）。
约定：
1. 直奔 flag，别闲聊；全过程别改目标的全局状态（不重启服务、不写无关文件）。
2. 找到候选 flag 后，**必须在输出里单独一行打印**：FOUND_FLAG=<flag 原文>
3. 拿不到就输出你排除的方向，别空转。
"""


class CliEngine:
    """一个外部 CLI 引擎实例（单发跑整题）。"""

    def __init__(
        self,
        name: str,
        binary: str,
        workspace: Workspace,
        gate: FlagGate,
        emit: Callable[[Any], None],
        extra_args: list[str] | None = None,  # 测试注入（如脚本路径）
        total_timeout: float = 600.0,
        runtime: ExecutionRuntime | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        self.binary = binary
        self.workspace = workspace
        self.gate = gate
        self.emit = emit
        self.extra_args = list(extra_args or [])
        self.total_timeout = total_timeout
        self.runtime = runtime
        self.env = dict(env or {})
        self._proc: asyncio.subprocess.Process | None = None

    def build_argv(self, prompt: str) -> list[str]:
        return [
            self.binary,
            *self.extra_args,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
        ]

    def _prompt(self, objective: str, target: str | None, flag_format: str | None) -> str:
        target_line = f"靶机地址：{target}\n" if target else ""
        format_line = f"flag 格式（正则）：{flag_format}\n" if flag_format else ""
        return PROMPT_TEMPLATE.format(objective=objective, target_line=target_line, format_line=format_line)

    async def run(self, objective: str, target: str | None, flag_format: str | None) -> None:
        """跑引擎直到 flag 命中 / 进程退出 / 超时 / 被取消。"""
        scratch = self.workspace.work_dir / self.name
        scratch.mkdir(parents=True, exist_ok=True)
        argv = self.build_argv(self._prompt(objective, target, flag_format))
        if self.runtime is not None:
            await self._run_in_runtime(argv, scratch, flag_format)
            return
        await self._run_on_host(argv, scratch, flag_format)

    def _output_consumer(
        self,
        buffer: list[str],
        flag_re: re.Pattern[str] | None,
        found: asyncio.Event,
    ) -> Callable[[str], None]:
        seen: set[str] = set()
        scan_tail = ""

        def consume(text: str) -> None:
            nonlocal scan_tail
            buffer.append(text)
            scan_tail = (scan_tail + text)[-16384:]
            hits = [match.group(1).strip() for match in _MARKER_RE.finditer(scan_tail)]
            if flag_re is not None:
                hits.extend(match.group(0) for match in flag_re.finditer(scan_tail))
            for hit in hits:
                if hit in seen:
                    continue
                seen.add(hit)
                self._harvest(hit, "".join(buffer))
                if self.gate.solved:
                    found.set()
                    return

        return consume

    async def _run_in_runtime(self, argv: list[str], scratch: Path, flag_format: str | None) -> None:
        """Stream Claude through the selected runtime and cancel immediately on a valid flag."""
        assert self.runtime is not None
        buffer: list[str] = []
        found = asyncio.Event()
        consume = self._output_consumer(buffer, re.compile(flag_format) if flag_format else None, found)
        self.emit(SwarmNote(message=f"race 引擎启动：{self.name}（容器 {Path(self.binary).name}）", worker=self.name))
        command_task = asyncio.create_task(
            self.runtime.run_argv(
                argv,
                cwd=scratch,
                env=self.env,
                idle_timeout=self.total_timeout,
                total_timeout=self.total_timeout,
                max_output=8 * 1024 * 1024,
                on_output=consume,
            )
        )
        found_task = asyncio.create_task(found.wait())
        try:
            done, _pending = await asyncio.wait(
                {command_task, found_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if found_task in done and found.is_set():
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
                return
            result = await command_task
            if result.timed_out or result.idle_timed_out:
                self.emit(SwarmNote(message=f"引擎 {self.name} 超时（{self.total_timeout:g}s）", worker=self.name))
            elif result.error is not None or result.exit_code != 0:
                detail = result.error or f"exit={result.exit_code}"
                self.emit(SwarmNote(message=f"引擎 {self.name} 退出异常：{detail}", worker=self.name))
        finally:
            found_task.cancel()
            if not command_task.done():
                command_task.cancel()
            await asyncio.gather(found_task, command_task, return_exceptions=True)

    async def _run_on_host(self, argv: list[str], scratch: Path, flag_format: str | None) -> None:
        """Explicit compatibility mode for users who accept host execution risk."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=scratch,
                env={**os.environ, **self.env} if self.env else None,
            )
        except FileNotFoundError as exc:
            self.emit(SwarmNote(message=f"引擎 {self.name} 启动失败: {exc}"))
            return
        self.emit(SwarmNote(message=f"race 引擎启动：{self.name}（{Path(self.binary).name}）", worker=self.name))
        buffer: list[str] = []
        found = asyncio.Event()
        consume = self._output_consumer(buffer, re.compile(flag_format) if flag_format else None, found)
        try:
            assert self._proc.stdout is not None
            while True:
                try:
                    raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.total_timeout)
                except asyncio.TimeoutError:
                    self.emit(SwarmNote(message=f"引擎 {self.name} 超时（{self.total_timeout:g}s）", worker=self.name))
                    break
                if not raw:
                    break  # EOF：进程退出
                line = raw.decode("utf-8", errors="replace")
                consume(line)
                if found.is_set():
                    return
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    def _harvest(self, flag: str, full_output: str) -> None:
        """归档引擎输出进 CAS（溯源证据）→ 过闸。"""
        handle = self.workspace.cas_store(full_output.encode("utf-8", errors="replace"))
        result = self.gate.submit(flag, evidence=handle)
        if result.ok:
            self.emit(SwarmNote(message=f"引擎 {self.name} 命中 flag：{flag}（第 {len(self.gate.flags)}/{self.gate.expected_flags} 面）", worker=self.name))
        else:
            self.emit(SwarmNote(message=f"引擎 {self.name} 候选被拒：{result.reason}", worker=self.name))

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            await _kill_tree(proc)

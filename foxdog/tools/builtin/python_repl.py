"""python_eval：持久 Python REPL（CTF 杀手锏工具）。

local：进程内 threading + code.InteractiveConsole（M1 方案，行为不变），
变量跨调用保留；启动时预 import pwntools/z3/gmpy2/pycryptodome/sympy。
docker/docker-persistent：经 runtime.spawn 在容器内跑 `python3 -u -i -q`
（镜像已装 pwntools/z3/pycryptodome），sentinel 同步输出，变量跨调用保留。
swarm/子 agent worker 按 worker_id 拆分 REPL 状态，避免变量和 cwd 互相污染。
"""
from __future__ import annotations

import asyncio
import code
import contextlib
import io
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ...core.context import spill_text
from .. import Tool, ToolAccess, ToolContext, ToolResult

PRELOAD = ["pwn", "z3", "gmpy2", "Crypto", "sympy"]
_PRELOAD_PROBE = (
    "import importlib.util as _u\n"
    "print('|'.join(m for m in ['pwn','z3','gmpy2','Crypto','sympy'] if _u.find_spec(m)))"
)

_REPL_TIMEOUT = 180.0  # 单次执行上限；超时杀进程重启（变量丢失，明确告知）


@dataclass
class _LocalReplState:
    ns: dict[str, Any] = field(default_factory=dict)
    console: code.InteractiveConsole = field(init=False)
    checked: bool = False
    preloaded: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.console = code.InteractiveConsole(locals=self.ns)


class _SpawnRepl:
    """经 runtime.spawn 的持久 python3 REPL（docker 模式：容器内 python3）。

    同步协议：写入 code + print(sentinel)，读 stdout 直到 sentinel 行出现；
    过滤 `>>> ` / `... ` 提示符行（-q 已关 banner）。
    """

    SENTINEL = "__FOXDOG_REPL_END__"

    def __init__(self, runtime: Any, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._runtime = runtime
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()  # 共享命名空间，执行串行

    async def _ensure(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        self._proc = await self._runtime.spawn(["python3", "-u", "-i", "-q"], cwd=self._cwd, env=self._env)
        await self._roundtrip("print('boot')", timeout=30.0)  # 起握手，冲掉首个提示符

    async def run(self, src: str, timeout: float = _REPL_TIMEOUT) -> str:
        async with self._lock:
            try:
                await self._ensure()
                return await self._roundtrip(src, timeout=timeout)
            except (RuntimeError, asyncio.TimeoutError) as exc:
                # 超时/进程退出：杀干净下次重建，明确告知状态已丢
                await self._kill()
                return f"RuntimeError: REPL 已重启（{exc}；此前变量已丢失）"

    async def _roundtrip(self, src: str, timeout: float) -> str:
        assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
        payload = f"{src}\nprint('{self.SENTINEL}')\n"
        self._proc.stdin.write(payload.encode("utf-8", errors="replace"))
        await self._proc.stdin.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        lines: list[str] = []
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"执行超过 {timeout:g}s")
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise RuntimeError("REPL 进程已退出")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.strip() == self.SENTINEL:
                break
            if text.startswith(">>> ") or text.startswith("... ") or text in (">>>", "..."):
                continue  # 交互提示符行，不是输出
            lines.append(text)
        return "\n".join(lines)

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


class PythonArgs(BaseModel):
    code: str = Field(description="要执行的 Python 代码（变量跨调用保留）")


class PythonEvalTool(Tool):
    name = "python_eval"
    description = (
        "在持久 Python REPL 中执行代码，变量跨调用保留。"
        "已尝试预装 pwntools(pwn)/z3/gmpy2/pycryptodome(Crypto)/sympy（以实际环境为准）。"
        "crypto 题解方程、pwn 题构造 payload 都用它。"
    )
    accesses = [ToolAccess(resource="python", mode="w")]
    args_model = PythonArgs

    def __init__(self) -> None:
        self._ns: dict[str, Any] = {}
        self._console = code.InteractiveConsole(locals=self._ns)
        self._checked = False
        #: 主 solver REPL 命名空间是可变状态，加锁串行；worker REPL 状态按
        #: worker_id 分桶，避免 swarm 并发时互相污染。
        self._lock = asyncio.Lock()
        self._repl: _SpawnRepl | None = None  # docker 模式懒建
        self.preloaded: list[str] = []
        self._local_states: dict[str, _LocalReplState] = {}
        self._worker_repls: dict[str, _SpawnRepl] = {}
        self._docker_checked: dict[str, bool] = {}
        self._docker_preloaded: dict[str, list[str]] = {}

    @staticmethod
    def _scope(ctx: ToolContext) -> str:
        return ctx.worker_id or "__main__"

    def _local_state(self, scope: str) -> _LocalReplState | None:
        if scope == "__main__":
            return None
        state = self._local_states.get(scope)
        if state is None:
            state = _LocalReplState()
            self._local_states[scope] = state
        return state

    def _preload_local(self, ns: dict[str, Any] | None = None, preloaded: list[str] | None = None) -> None:
        target_ns = ns if ns is not None else self._ns
        target_preloaded = preloaded if preloaded is not None else self.preloaded
        for mod in PRELOAD:
            try:
                if mod not in target_preloaded:
                    target_ns[mod] = __import__(mod)
                    target_preloaded.append(mod)
            except ImportError:
                pass

    def _exec(self, src: str, console: code.InteractiveConsole | None = None) -> str:
        active_console = console if console is not None else self._console
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                incomplete = active_console.runsource(src)
                if incomplete:
                    print("SyntaxError: 代码不完整（可能缺少闭合括号/冒号块）")
            except Exception:
                import traceback

                traceback.print_exc()
        return buf.getvalue()

    def _docker_repl(self, scope: str, runtime: Any, cwd: Any, env: dict[str, str] | None) -> _SpawnRepl:
        if scope == "__main__":
            if self._repl is None:
                self._repl = _SpawnRepl(runtime, cwd=str(cwd) if cwd is not None else None, env=env)
            return self._repl
        repl = self._worker_repls.get(scope)
        if repl is None:
            repl = _SpawnRepl(runtime, cwd=str(cwd) if cwd is not None else None, env=env)
            self._worker_repls[scope] = repl
        return repl

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        runtime = getattr(ctx, "runtime", None)
        scope = self._scope(ctx)
        note = ""
        if runtime is not None and runtime.kind != "local":
            # docker / docker-persistent：容器内 REPL（镜像已装 exploit 包）
            repl = self._docker_repl(scope, runtime, getattr(ctx, "work_dir", None), ctx.execution_env())
            first = not self._docker_checked.get(scope, False)
            if first:
                avail = await repl.run(_PRELOAD_PROBE)
                self._docker_preloaded[scope] = [m for m in avail.split("|") if m.strip()]
                self._docker_checked[scope] = True
                self.preloaded = self._docker_preloaded[scope]
            output = await repl.run(a.code)
        else:
            state = self._local_state(scope)
            if state is None:
                first = not self._checked
                if not self.preloaded:
                    self._preload_local()
                async with self._lock:
                    output = await asyncio.to_thread(self._exec, a.code)
                self._checked = True
            else:
                first = not state.checked
                if not state.preloaded:
                    self._preload_local(state.ns, state.preloaded)
                async with state.lock:
                    output = await asyncio.to_thread(self._exec, a.code, state.console)
                state.checked = True
                self.preloaded = state.preloaded
        if first:
            preloaded = self.preloaded
            if runtime is not None and runtime.kind != "local":
                preloaded = self._docker_preloaded.get(scope, self.preloaded)
            elif scope != "__main__":
                state = self._local_states.get(scope)
                preloaded = state.preloaded if state is not None else self.preloaded
            note = f"[预装模块: {', '.join(preloaded) or '无'}]\n"
        view, handle, truncated = spill_text(ctx.workspace, output, 64 * 1024)
        return ToolResult(output=note + (view or "(无输出)"), handle=handle, data={"truncated": truncated})

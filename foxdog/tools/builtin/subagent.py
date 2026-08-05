"""子 agent 工具（DESIGN §6.2）：spawn_agent / wait_agent / list_agents。

spawn_agent(prompt, worker_id?)：启动子 agent——WorkerFactory 造独立 loop
（独立 history、registry 剥掉 spawn 系→深度上限 1），共享黑板/FlagGate/工作区/
权限链；asyncio task 跑单个 run_turn（worker 是单发的）。任一 worker 过闸 →
FlagGate.solved → 所有 loop 下一 step 自然停。
wait_agent(ids, timeout_s)：收各 worker 最后一条 assistant 文本（截断 2KB）。
"""
from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

from pydantic import BaseModel, Field

from ...llm.messages import system_message, user_message
from .. import Tool, ToolAccess, ToolContext, ToolResult

MAX_SUBAGENTS = 32  # DESIGN 上限

SUBAGENT_SYSTEM = """\
你是主 agent 派出的子 agent（worker），与主 agent 和其他 worker 共享黑板和 FlagGate。
纪律：
1. 专注完成派给你的任务，别发散；开工先读板（下方黑板摘要）。
2. 每确认一个事实就 post_fact 上板，每排除一个方向就 post_deadend 上板。
3. 拿到候选 flag 立刻 submit_flag——闸门全队共享，过闸即全员停止。
4. 保持步数精简，完成后用一句话汇报结论（成功的证据或失败的原因）。
5. 你有 worker 私有 cwd；shell、文件工具、python_eval 的相对路径默认在这里。
   Docker runtime 下 TMPDIR/TMP/TEMP 也指向私有 .tmp。临时文件用 $TMPDIR，不要硬编码 /tmp/foo。
   需要共享文件时显式写入 run 公共 work 区或 inputs 区。
"""


def last_assistant_text(history: list[dict[str, Any]]) -> str:
    """取历史里最后一条 assistant 文本（worker 汇报）。"""
    for m in reversed(history):
        if m.get("role") == "assistant" and m.get("content"):
            return str(m["content"])
    return ""


class SubAgent:
    def __init__(self, worker_id: str, prompt: str) -> None:
        self.worker_id = worker_id
        self.prompt = prompt
        self.started = time.time()
        self.task: asyncio.Task | None = None
        self.report: str = ""
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class SubAgentManager:
    def __init__(self, factory, board, max_subagents: int = MAX_SUBAGENTS) -> None:
        self.factory = factory
        self.board = board
        self.max_subagents = max_subagents
        self._agents: dict[str, SubAgent] = {}
        self._counter = itertools.count(1)
        self._gate_watcher: asyncio.Task | None = None

    def _ensure_gate_watcher(self) -> None:
        """父 agent 过闸 → 子在跑 agent 立即取消（0.2s 轮询）。

        预留：未来 expected_flags > 1（多 flag 题）时，取消条件要改成
        "已验证 flag 数凑够"再停，而不是第一个过闸就全停。
        """
        if self._gate_watcher is not None and not self._gate_watcher.done():
            return
        gate = getattr(self.factory, "flag_gate", None)
        if gate is None:
            return

        async def _watch() -> None:
            while not gate.solved:
                await asyncio.sleep(0.2)
            for agent in self._agents.values():
                if agent.task is not None and not agent.task.done():
                    agent.task.cancel()

        self._gate_watcher = asyncio.create_task(_watch())

    def spawn(self, prompt: str, worker_id: str | None = None) -> str:
        running = sum(1 for a in self._agents.values() if a.running)
        if running >= self.max_subagents:
            raise ValueError(f"子 agent 并发上限 {self.max_subagents}，先 wait_agent 收一批")
        wid = worker_id or f"agent-{next(self._counter)}"
        if wid in self._agents and self._agents[wid].running:
            raise ValueError(f"worker_id 冲突: {wid}")
        agent = SubAgent(wid, prompt)
        loop = self.factory.make_loop(wid)
        board_summary = self.board.summary() if self.board is not None else "（无黑板）"
        worker_cwd = loop.orchestrator.ctx.runtime.display_path(loop.orchestrator.ctx.work_dir)
        shared_work = loop.orchestrator.ctx.runtime.display_path(loop.orchestrator.ctx.workspace.work_dir)
        history = [
            system_message(
                f"{SUBAGENT_SYSTEM}\n\n"
                f"当前 worker 私有 cwd：{worker_cwd}\n"
                f"run 公共 work 区：{shared_work}\n\n"
                f"{board_summary}"
            ),
            user_message(f"子 agent 任务：{prompt}"),
        ]

        async def _run() -> None:
            try:
                await loop.run_turn(history, turn_no=1)
                agent.report = last_assistant_text(history)
            except asyncio.CancelledError:
                agent.error = "cancelled"
            except Exception as exc:  # noqa: BLE001 - 子 agent 异常不炸主流程
                agent.error = f"{type(exc).__name__}: {exc}"

        agent.task = asyncio.create_task(_run())
        self._agents[wid] = agent
        self._ensure_gate_watcher()
        return wid

    async def wait(self, ids: list[str], timeout_s: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout_s
        out: dict[str, str] = {}
        for wid in ids:
            agent = self._agents.get(wid)
            if agent is None:
                out[wid] = "（未知 worker）"
                continue
            remaining = deadline - time.monotonic()
            if agent.task is not None and not agent.task.done() and remaining > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(agent.task), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
            if agent.error:
                out[wid] = f"[error] {agent.error}"
            elif agent.running:
                out[wid] = "（仍在运行，稍后再 wait_agent）"
            else:
                out[wid] = agent.report[:2048] or "（无文本汇报）"
        return out

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "worker_id": a.worker_id,
                "running": a.running,
                "elapsed_s": round(time.time() - a.started, 1),
                "prompt": a.prompt[:60],
                "report": a.report[:80],
                "error": a.error,
            }
            for a in self._agents.values()
        ]

    async def stop_all(self) -> None:
        """Cancel and reap every child so no model stream survives its parent run."""
        tasks = [a.task for a in self._agents.values() if a.task is not None and not a.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class SpawnAgentArgs(BaseModel):
    prompt: str = Field(description="派给子 agent 的任务（自洽：含目标、成功标准）")
    worker_id: str | None = Field(default=None, description="自定义 worker 标识（缺省 agent-N）")


class SpawnAgentTool(Tool):
    name = "spawn_agent"
    description = (
        "扇出子 agent 并行干活（独立 loop 单 turn，共享工作区/黑板/FlagGate）。"
        "适合批量试 payload、并行分析多文件。wait_agent 收结果。"
    )
    accesses = [ToolAccess(resource="tasks", mode="w")]
    args_model = SpawnAgentArgs

    def __init__(self, manager: SubAgentManager) -> None:
        self.manager = manager

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            wid = self.manager.spawn(a.prompt, a.worker_id)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(output=f"子 agent {wid} 已启动。用 wait_agent(ids=[{wid!r}]) 收结果。")


class WaitAgentArgs(BaseModel):
    ids: list[str] = Field(description="要等待的 worker_id 列表")
    timeout_s: float = Field(default=60.0, description="总等待超时（秒）", ge=0, le=600)


class WaitAgentTool(Tool):
    name = "wait_agent"
    description = "等待子 agent 完成，返回各 worker 的最后汇报文本（截断 2KB）。"
    accesses = [ToolAccess(resource="tasks", mode="r")]
    args_model = WaitAgentArgs

    def __init__(self, manager: SubAgentManager) -> None:
        self.manager = manager

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        results = await self.manager.wait(a.ids, a.timeout_s)
        return ToolResult(output="\n\n".join(f"[{wid}] {text}" for wid, text in results.items()))


class ListAgentsTool(Tool):
    name = "list_agents"
    description = "列出全部子 agent 及状态。"
    accesses = [ToolAccess(resource="tasks", mode="r")]

    def __init__(self, manager: SubAgentManager) -> None:
        self.manager = manager

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rows = self.manager.list()
        if not rows:
            return ToolResult(output="(无子 agent)")
        lines = [
            f"{r['worker_id']} [{'running' if r['running'] else 'done'}] {r['elapsed_s']}s :: {r['prompt']}"
            + (f" ⇒ {r['report']}" if r["report"] else "")
            + (f" [error: {r['error']}]" if r["error"] else "")
            for r in rows
        ]
        return ToolResult(output="\n".join(lines))

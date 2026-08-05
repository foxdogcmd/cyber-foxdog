"""任务 3/4：planner 对简单题克制（prompt 条款）；flag 过闸立即终止在跑 worker。"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.store.workspace import Workspace
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.planner import PLANNER_INSTRUCTION, ReasonPlanner
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import Tool, ToolContext, ToolRegistry, ToolResult, register_builtin_tools
from foxdog.tools.builtin.subagent import SubAgentManager

FLAG = "flag{cancel_test_ok}"
READ_CMD = "Get-Content -Raw -LiteralPath '..\\..\\flag.txt'" if sys.platform == "win32" else "cat ../../flag.txt"


# ------------------------------------------------------------ planner 克制
def test_planner_prompt_has_restraint_clause():
    assert "不要为派而派" in PLANNER_INSTRUCTION
    assert "0-1 个 intent" in PLANNER_INSTRUCTION
    assert "直接写 exploit" in PLANNER_INSTRUCTION
    assert "只有证据显示复杂" in PLANNER_INSTRUCTION


# ------------------------------------------------------------ 取消原语
class SleepTool(Tool):
    name = "fake_sleep10"
    description = "stub: asyncio.sleep(60)（长睡防负载下自然结束；测试只看是否被取消）"
    accesses = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(60)
        return ToolResult(output="slept")


def _stack(tmp_path, provider, run_id="cancel-test"):
    ws = Workspace(base=tmp_path, run_id=run_id)
    (ws.work_dir / "flag.txt").write_text(f"目标输出: {FLAG}\n", encoding="utf-8")
    events: list = []
    registry = ToolRegistry()
    register_builtin_tools(registry)
    registry.register(SleepTool())
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws, background=background, emit=events.append,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l), flag_gate=gate,
    )
    return ws, events, registry, gate, background, ctx


async def test_coordinator_cancels_running_workers_on_solved(tmp_path):
    """worker-1 快读 flag 过闸；worker-2 在 10s sleep 中——断言 1s 内被取消（不等命令结束）。"""
    ws, events, registry, gate, background, ctx = _stack(tmp_path, None)
    routes = [
        ("规划器", [("text", '{"verdict":"explore","intents":[],"audit":[]}')]),
        ("拿flag", [
            ("tool", "shell_exec", {"command": READ_CMD}),
            ("tool", "submit_flag", {"flag": FLAG}),
            ("text", "flag 已提交"),
        ]),
        ("长跑方向", [("tool", "fake_sleep10", {}), ("text", "不该到这里")]),
    ]
    provider = FakeProvider(routes=routes, script=[("text", "x")])
    factory = WorkerFactory(provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    coordinator = Coordinator(
        board=ctx.facts, planner=ReasonPlanner(provider), factory=factory,
        emit=events.append, rollout=None, gate=gate, objective="取消测试",
        budget=Budget(), workers=2, race_minutes=0, tick=0.05, planner_cooldown=0.1,
        spawn_batch=2,
    )
    ctx.facts.create_intent("拿flag", "file", "local", "read-a", actor="test")
    ctx.facts.create_intent("长跑方向", "misc", "local", "sleep-a", actor="test")

    started = time.monotonic()
    solve_task = asyncio.create_task(coordinator.solve())
    # 等过闸
    deadline = time.monotonic() + 15
    while not gate.solved and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert gate.solved, "worker-1 没过闸"
    solved_at = time.monotonic()
    code = await solve_task
    elapsed = time.monotonic() - solved_at

    assert code == 0
    assert elapsed < 8.0, f"过闸后 {elapsed:.2f}s 才收尾（worker-2 没被及时取消——不取消会到 10s 量级）"
    assert time.monotonic() - started < 9.0, "总耗时异常（像是在等 sleep 10 结束）"
    # SwarmNote 有终止提示
    notes = [e.message for e in events if getattr(e, "kind", "") == "swarm_note"]
    assert any("终止" in n and "worker" in n for n in notes), notes
    intents = {i["id"]: i for i in ctx.facts.intents()}
    assert intents  # 全部收尾（过闸收尾 done）
    assert all(i["status"] == "done" for i in intents.values())
    ctx.facts.close()


async def test_subagent_manager_cancels_children_on_parent_solved(tmp_path):
    """父 loop solved 后，子在跑 agent 立即被取消。"""
    ws, events, registry, gate, background, ctx = _stack(tmp_path, None, run_id="cancel-sub")
    routes = [
        ("拿flag", [
            ("tool", "shell_exec", {"command": READ_CMD}),
            ("tool", "submit_flag", {"flag": FLAG}),
            ("text", "child-a done"),
        ]),
        ("长跑方向", [("tool", "fake_sleep10", {}), ("text", "不该到这里")]),
    ]
    provider = FakeProvider(routes=routes, script=[("text", "parent")])
    factory = WorkerFactory(provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    manager = SubAgentManager(factory, ctx.facts)

    manager.spawn("拿flag", worker_id="child-a")
    manager.spawn("长跑方向", worker_id="child-b")

    deadline = time.monotonic() + 15
    while not gate.solved and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert gate.solved
    solved_at = time.monotonic()
    # child-b 应在 1s 内被取消
    b = manager._agents["child-b"]
    deadline = time.monotonic() + 8
    while not b.task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - solved_at
    assert b.task.done(), "child-b 未被取消"
    assert b.error == "cancelled", f"child-b 是被取消（error={b.error}），不是自然结束"
    assert elapsed < 8.0, f"child-b 过闸后 {elapsed:.2f}s 才被取消——不取消会到 10s 量级"
    assert manager._agents["child-a"].error is None  # child-a 正常过闸收尾（solved 即停，无错误）
    ctx.facts.close()

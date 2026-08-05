"""worker 真并行（spawn_batch=3 一拍填满）+ 预算 None=无限 + 运行中动态调预算。"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget, BudgetState
from foxdog.core.goal import EXIT_BUDGET, GoalMode
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.planner import ReasonPlanner
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import Tool, ToolContext, ToolRegistry, ToolResult, register_builtin_tools

READ_CMD = "type flag.txt" if sys.platform == "win32" else "cat flag.txt"


class SleepTool(Tool):
    """测试工具：异步睡 0.3s（真并行断言用）。"""

    name = "fake_sleep"
    description = "stub"
    accesses = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(0.3)
        return ToolResult(output="slept")


def _stack(tmp_path, provider, run_id="parallel"):
    ws = Workspace(base=tmp_path, run_id=run_id)
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


async def test_workers_parallel_wallclock(tmp_path):
    """手动驱动主循环：等 3 worker 并行完成后 interrupt，量 wall clock。"""
    ws, events, registry, gate, background, ctx = _stack(tmp_path, None, run_id="parallel2")
    routes = [
        ("规划器", [("text", '{"verdict":"explore","intents":[],"audit":[]}')],),
        ("睡甲", [("tool", "fake_sleep", {}), ("text", "甲完成")]),
        ("睡乙", [("tool", "fake_sleep", {}), ("text", "乙完成")]),
        ("睡丙", [("tool", "fake_sleep", {}), ("text", "丙完成")]),
    ]
    provider = FakeProvider(routes=routes, script=[("text", "x")])
    factory = WorkerFactory(provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    coordinator = Coordinator(
        board=ctx.facts, planner=ReasonPlanner(provider), factory=factory,
        emit=events.append, rollout=None, gate=gate, objective="并行测试",
        budget=Budget(), workers=3, race_minutes=0, tick=0.05, planner_cooldown=0.1,
        spawn_batch=3,
    )
    for i, tag in enumerate(("睡甲", "睡乙", "睡丙")):
        ctx.facts.create_intent(f"{tag}（方向{i}）", f"cat{i}", f"surface{i}", f"tech{i}", actor="test")

    started = time.monotonic()
    solve_task = asyncio.create_task(coordinator.solve())
    # 等 3 个 intent 全部收尾
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        intents = ctx.facts.intents()
        if intents and all(i["status"] == "done" for i in intents):
            break
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - started
    coordinator.interrupt.set()
    code = await solve_task

    assert elapsed < 0.9, f"3×0.3s 并行应 <0.9s，实测 {elapsed:.2f}s（疑似串行）"
    intents = ctx.facts.intents()
    assert len(intents) == 3 and all(i["status"] == "done" for i in intents)
    ctx.facts.close()


def test_budget_none_means_unlimited():
    state = BudgetState(Budget())  # 全 None
    state.add_tokens(10**9)
    state.check()  # 不抛异常即无限
    assert "∞" in Budget().describe()


async def test_budget_change_takes_effect_live(tmp_path):
    """无限预算下开跑（不会被拦），运行中改成 0.01 分钟 → 立刻被拦 exit 2。"""
    ws = Workspace(base=tmp_path, run_id="budget-live")
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": "budget-live"})
    registry = ToolRegistry()
    register_builtin_tools(registry)
    gate = FlagGate(ws, flag_format=r"x")
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws, background=background, emit=events.append,
        fetch_output=lambda h, o, l: "", flag_gate=gate,
    )
    provider = FakeProvider(script=[("tool", "shell_exec", {"command": "echo hi"})], repeat=True)
    loop = AgentLoop(provider, registry, ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx),
                     events.append, rollout, gate, max_steps=100000)
    budget = Budget()  # 无限
    goal = GoalMode(loop, budget)
    history = [system_message("s"), user_message("go")]

    async def tighten():
        await asyncio.sleep(0.3)
        budget.max_minutes = 0.0001  # 运行中收紧：下一次 step 前 check 即生效

    started = time.monotonic()
    code, _ = await asyncio.gather(goal.solve(history), tighten())
    elapsed = time.monotonic() - started
    await background.stop_all()
    await rollout.close()
    assert code == EXIT_BUDGET
    assert elapsed < 30

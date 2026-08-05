"""step 级预算强制（M1 教训：--max-minutes 只在 turn 间检查，单 turn 内无限跑）。

FakeProvider(repeat=True) 无限返回 tool call，max_minutes=0.01（0.6s）时
必须在单 turn 内被 BudgetExceeded 拦下，solve 返回 exit 2。
"""
from __future__ import annotations

import time

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget, BudgetExceeded, BudgetState
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
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools


def _make_loop(ws: Workspace, provider: FakeProvider, events: list, rollout: RolloutWriter):
    registry = ToolRegistry()
    register_builtin_tools(registry)
    gate = FlagGate(ws, flag_format=r"flag\{.*\}")
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws,
        background=background,
        emit=events.append,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l),
        flag_gate=gate,
    )
    orchestrator = ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx)
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        orchestrator=orchestrator,
        emit=events.append,
        rollout=rollout,
        flag_gate=gate,
        max_steps=100000,  # 排除 max_steps 护栏先触发的可能
    )
    return loop, background


async def test_step_level_minutes_budget(tmp_path):
    ws = Workspace(base=tmp_path, run_id="budget-minutes")
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": ws.run_id})
    # 无限 tool call：没有 step 级预算检查的话永远跑不完一个 turn
    provider = FakeProvider(script=[("tool", "shell_exec", {"command": "echo hi"})], repeat=True)
    loop, background = _make_loop(ws, provider, events, rollout)

    goal = GoalMode(loop, Budget(max_minutes=0.01, max_turns=1000))
    history = [system_message("sys"), user_message("go")]
    started = time.monotonic()
    code = await goal.solve(history)
    elapsed = time.monotonic() - started
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_BUDGET
    assert elapsed < 30, f"step 级预算没拦住，跑了 {elapsed:.1f}s"
    kinds = [e.kind for e in events]
    assert "budget" in [getattr(e, "stop_reason", None) for e in events if e.kind == "turn_complete"]


async def test_step_level_tokens_budget(tmp_path):
    ws = Workspace(base=tmp_path, run_id="budget-tokens")
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": ws.run_id})
    provider = FakeProvider(script=[("tool", "shell_exec", {"command": "echo hi"})], repeat=True)
    loop, background = _make_loop(ws, provider, events, rollout)

    # FakeProvider 每 step 报 15 tokens；上限 40 → 第 3 个 step 前必被拦
    goal = GoalMode(loop, Budget(max_minutes=60, max_tokens=40, max_turns=1000))
    history = [system_message("sys"), user_message("go")]
    code = await goal.solve(history)
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_BUDGET
    assert provider.calls <= 4, f"token 预算没在 step 级拦住（调了 {provider.calls} 次）"


def test_budget_state_check():
    state = BudgetState(Budget(max_minutes=0.0, max_tokens=None))
    try:
        state.check()
    except BudgetExceeded as exc:
        assert exc.dimension == "minutes"
    else:
        raise AssertionError("max_minutes=0 应立即超限")

    state = BudgetState(Budget(max_minutes=60, max_tokens=10))
    state.add_tokens(10)
    try:
        state.check()
    except BudgetExceeded as exc:
        assert exc.dimension == "tokens"
    else:
        raise AssertionError("tokens 达上限应立即超限")

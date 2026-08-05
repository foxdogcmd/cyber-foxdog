"""race 遗产交接：race 无旗收场 → 黑板上板 race 发现、planner 上下文含"race 阶段已探明"。"""
from __future__ import annotations

import sys
import time

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget
from foxdog.core.goal import GoalMode
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.workspace import Workspace
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.planner import ReasonPlanner
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools


def _stack(tmp_path, run_id="race-legacy"):
    ws = Workspace(base=tmp_path, run_id=run_id)
    events: list = []
    registry = ToolRegistry()
    register_builtin_tools(registry)
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws, background=background, emit=events.append,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l), flag_gate=gate,
    )
    return ws, events, registry, gate, background, ctx


async def test_race_legacy_summarized_and_in_planner_context(tmp_path):
    """race 无旗收场：把各引擎 race 历史**摘要成结构化结论**上板（不再是半句思考碎片），
    并进 planner 上下文；planner 不从头再来。"""
    ws, events, registry, gate, background, ctx = _stack(tmp_path)
    # 主 solver：侦察一轮留下发现（post_fact），然后自然停（无 flag）
    main_provider = FakeProvider(script=[
        ("tool", "post_fact", {"fact": "8080 是 nginx 1.24"}),
        ("text", "race 阶段侦察到 nginx，路径探测暂无结果"),
    ])
    factory = WorkerFactory(main_provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    main_goal = GoalMode(factory.make_loop("solver"), Budget(max_minutes=0.02)  # race 快速铃响
    )
    main_history = [system_message("sys"), user_message("解题")]
    # 引擎 A：也留一条发现
    ea_provider = FakeProvider(script=[
        ("tool", "post_fact", {"fact": "/admin 403 但需要鉴权"}),
        ("text", "引擎 A 发现 admin 面板存在"),
    ])
    fa = WorkerFactory(ea_provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=5)
    goal_a = GoalMode(fa.make_loop("engine-a"), Budget())
    hist_a = [system_message("sys"), user_message("解题（引擎 A）")]
    goal_a.history_ref = hist_a

    # planner 用独立 provider：race 收尾时 _summarize_race 调它产出结构化交接
    planner_provider = FakeProvider(script=[
        ("text", "已探明：8080 端口是 nginx 1.24；/admin 面板存在但需鉴权\n待验证：admin 弱口令\n死路：无"),
        # 后续 planner.plan 调用返回 complete，让流程收尾
        ("text", '{"verdict":"complete","intents":[],"audit":[]}'),
    ])
    coordinator = Coordinator(
        board=ctx.facts, planner=ReasonPlanner(planner_provider), factory=factory,
        emit=events.append, rollout=None, gate=gate, objective="遗产测试",
        budget=Budget(max_minutes=0.02), workers=3, race_goal=main_goal, race_minutes=0.02,
        tick=0.05, planner_cooldown=0.1,
        race_engines=[("engine-a", lambda: goal_a.solve(hist_a), goal_a)],
    )
    solve_task = __import__("asyncio").create_task(coordinator.solve())
    code = await solve_task

    facts = [f["content"] for f in ctx.facts.facts()]
    actors_facts = [(f["content"], f["actor"]) for f in ctx.facts.facts()]
    # race worker 自己的 post_fact 仍在板上
    assert any("8080 是 nginx 1.24" in c for c in facts), facts
    assert any("/admin 403 但需要鉴权" in c for c in facts), facts
    # race 遗产（actor=race）上板：是结构化结论，不是半句思考碎片
    race_facts = [c for c, a in actors_facts if a == "race"]
    assert race_facts, actors_facts
    assert any("race 阶段结论" in c and "已探明" in c for c in race_facts), race_facts
    # 不再把"Let me..."/过渡句原样上板
    assert not any("路径探测暂无结果" in c for c in race_facts), race_facts
    # planner 上下文含结构化交接
    ctx_text = coordinator._planning_context()
    assert "race 阶段已探明" in ctx_text
    assert "nginx" in ctx_text
    # SwarmNote 提示遗产已总结上板
    notes = [e.message for e in events if getattr(e, "kind", "") == "swarm_note"]
    assert any("race 遗产已总结上板" in n for n in notes), notes
    await background.stop_all()
    ctx.facts.close()

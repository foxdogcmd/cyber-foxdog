"""事实板：post_fact/post_deadend 落 facts.jsonl；下一 turn 系统提示带摘要。"""
from __future__ import annotations

import sys

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget
from foxdog.core.goal import EXIT_SOLVED, GoalMode
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.core.prompts import build_system_prompt
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.swarm.facts import FactsBoard
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools
from foxdog.tools.builtin.facts import PostDeadEndTool, PostFactTool

FLAG = "flag{facts_board_test}"


def test_board_append_and_render(tmp_path):
    board = FactsBoard(tmp_path / "facts.jsonl")
    board.post_fact("8080 是 nginx 1.24", "abc123handle")
    board.post_deadend("直接写 .php 马", "waf_content 拦 <? 全部变体")
    text = board.render()
    assert "8080 是 nginx 1.24" in text and "abc123handle" in text
    assert "直接写 .php 马" in text and "waf_content" in text
    assert "已知事实" in text and "已排除方向" in text
    # 重读（模拟下一 turn）：摘要仍在
    board2 = FactsBoard(tmp_path / "facts.jsonl")
    assert len(board2.facts()) == 1 and len(board2.deadends()) == 1


async def test_post_tools(tmp_path):
    ws = Workspace(base=tmp_path, run_id="facts-tools")
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l),
    )
    res = await PostFactTool().run({"fact": "靶机有 .git 泄露", "evidence_handle": "h1"}, ctx)
    assert res.ok
    res = await PostDeadEndTool().run({"direction": "目录爆破", "reason": "common.txt 全 404"}, ctx)
    assert res.ok
    assert (ws.dir / "blackboard.db").is_file()  # M3 起事实板落 SQLite 黑板
    assert "靶机有 .git 泄露" in ctx.facts.render()
    assert "目录爆破" in ctx.facts.render()


async def test_facts_injected_next_turn_system_prompt(tmp_path):
    """post 后下一 turn 开始，系统提示包含事实/死路摘要。"""
    ws = Workspace(base=tmp_path, run_id="facts-turn")
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": ws.run_id})
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
    (ws.work_dir / "target_flag.txt").write_text(FLAG, encoding="utf-8")
    read_flag = (
        "Get-Content -Raw -LiteralPath 'target_flag.txt'"
        if sys.platform == "win32"
        else "cat target_flag.txt"
    )
    provider = FakeProvider(
        script=[
            ("tool", "post_fact", {"fact": "8080 是 nginx"}),
            ("tool", "post_deadend", {"direction": "直接爆破后台", "reason": "全部 403"}),
            ("text", "先记录，停下"),  # turn 1 自然结束
            ("tool", "shell_exec", {"command": read_flag}),  # turn 2
            ("tool", "submit_flag", {"flag": FLAG}),
            ("text", "done"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        orchestrator=orchestrator,
        emit=events.append,
        rollout=rollout,
        flag_gate=gate,
        max_steps=10,
    )

    def refresh_system(history, turn_no):
        history[0] = system_message(build_system_prompt(board=ctx.facts))

    goal = GoalMode(loop, Budget(max_minutes=5), on_turn_start=refresh_system)
    history = [system_message(build_system_prompt(board=ctx.facts)), user_message("解题")]
    code = await goal.solve(history)
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_SOLVED
    # turn 2 开始时 refresh 过：history[0] 含 post 的事实与死路
    sysmsg = history[0]["content"]
    assert "8080 是 nginx" in sysmsg
    assert "直接爆破后台" in sysmsg and "全部 403" in sysmsg
    assert "已知事实" in sysmsg and "已排除方向" in sysmsg

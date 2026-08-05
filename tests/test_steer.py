"""steer 中途指挥：队列注入 → 下一 turn 的 history 出现 steer user message + rollout 记录。"""
from __future__ import annotations

import json
import sys

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget
from foxdog.core.goal import EXIT_SOLVED, GoalMode
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.core.steer import SteerQueue
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools

FLAG = "flag{steer_test_ok}"
READ_CMD = "type flag.txt" if sys.platform == "win32" else "cat flag.txt"


def test_steer_queue_basic():
    q = SteerQueue()
    q.put("  先试 XX  ")
    q.put("")  # 空行忽略
    q.put("再试 YY")
    assert q.drain() == ["先试 XX", "再试 YY"]
    assert q.drain() == []  # 取空


async def test_steer_injected_next_turn(tmp_path):
    ws = Workspace(base=tmp_path, run_id="steer-test")
    (ws.work_dir / "flag.txt").write_text(f"目标输出: {FLAG}\n", encoding="utf-8")
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": ws.run_id})
    registry = ToolRegistry()
    register_builtin_tools(registry)
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws,
        background=background,
        emit=events.append,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l),
        flag_gate=gate,
    )
    orchestrator = ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx)
    provider = FakeProvider(
        script=[
            ("text", "第一轮没思路，先停"),  # turn 1 completed → 自动续跑
            ("tool", "shell_exec", {"command": READ_CMD}),  # turn 2（steer 已注入）
            ("tool", "submit_flag", {"flag": FLAG}),
            ("text", "done"),
        ]
    )
    loop = AgentLoop(provider, registry, orchestrator, events.append, rollout, gate, max_steps=10)
    steer = SteerQueue()
    goal = GoalMode(loop, Budget(max_minutes=5), steer=steer)
    history = [system_message("sys"), user_message("解题")]

    # turn 1 进行中（第一次 LLM 调用返回后）放入 steer
    steer.put("先试读 flag.txt 这个方向")
    code = await goal.solve(history)
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_SOLVED
    # steer 作为 user message 出现在 history（turn 2 开始时注入）
    steer_msgs = [
        m for m in history
        if m.get("role") == "user" and "操作员 steer 指令" in str(m.get("content"))
    ]
    assert len(steer_msgs) == 1 and "先试读 flag.txt 这个方向" in steer_msgs[0]["content"]
    # rollout 有 steer 记录
    records = [json.loads(l) for l in (ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()]
    steers = [r for r in records if r.get("type") == "steer"]
    assert len(steers) == 1 and steers[0]["text"] == "先试读 flag.txt 这个方向"
    ctx.facts.close()

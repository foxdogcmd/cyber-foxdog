"""多 flag（任务 4 修正规格）：FlagGate 累积计数、按预期数收口终止 worker、writeup flags。"""
from __future__ import annotations

import asyncio
import json
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
from foxdog.store.writeup import generate_writeup
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.planner import ReasonPlanner
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import Tool, ToolContext, ToolRegistry, ToolResult, register_builtin_tools

FLAG1 = "flag{multi_first_ok}"
FLAG2 = "flag{multi_second_ok}"
READ1 = "Get-Content -Raw -LiteralPath '..\\..\\flag1.txt'" if sys.platform == "win32" else "cat ../../flag1.txt"
READ2 = "Get-Content -Raw -LiteralPath '..\\..\\flag2.txt'" if sys.platform == "win32" else "cat ../../flag2.txt"


# ------------------------------------------------------------ FlagGate 多 flag
def test_gate_multi_flag_accumulation(tmp_path):
    ws = Workspace(base=tmp_path, run_id="multi-gate")
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}", expected_flags=2)
    # 溯源证据：两面 flag 都只能在"输出"里出现（自我注入时序规则）
    ws.cas_store(f"out1 {FLAG1}\nout2 {FLAG2}\n".encode())

    r1 = gate.submit(FLAG1)
    assert r1.ok and "第 1/2 面" in r1.reason
    assert not gate.solved  # 第一面过闸不收工
    assert gate.flags == [FLAG1]

    r_dup = gate.submit(FLAG1)  # 重复提交：ok 但忽略
    assert r_dup.ok and "忽略重复提交" in r_dup.reason
    assert gate.flags == [FLAG1]

    r2 = gate.submit(FLAG2)
    assert r2.ok and "第 2/2 面" in r2.reason
    assert gate.solved  # 凑齐才 solved
    assert gate.flag == FLAG2

    r3 = gate.submit("flag{third_extra}")  # 集齐后新提交忽略（格式不对也会先被 solved 拦）
    assert r3.ok and "已集齐" in r3.reason

    winner = json.loads(ws.winner_path.read_text(encoding="utf-8"))
    assert winner["flags"] == [FLAG1, FLAG2] and winner["solved"] is True
    assert winner["flag"] == FLAG2  # 兼容旧读取方：最后一面


def test_gate_single_flag_default_unchanged(tmp_path):
    """默认 expected_flags=1：行为与旧版完全一致（first-valid-wins）。"""
    ws = Workspace(base=tmp_path, run_id="single-gate")
    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}")
    ws.cas_store(b"out flag{only_one}\n")
    r = gate.submit("flag{only_one}")
    assert r.ok and gate.solved
    r2 = gate.submit("flag{another}")
    assert r2.ok and "已集齐" in r2.reason


# ------------------------------------------------------------ coordinator 按预期数收口
class SleepTool(Tool):
    name = "fake_sleep"
    description = "stub"
    accesses = []

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(self.seconds)
        return ToolResult(output="slept")


async def test_coordinator_multi_flag_no_cancel_until_all(tmp_path):
    """expected=2：第一面过闸不 cancel（worker 继续跑）；第二面后 1s 内全部取消。"""
    ws = Workspace(base=tmp_path, run_id="multi-coord")
    (ws.work_dir / "flag1.txt").write_text(f"out {FLAG1}\n", encoding="utf-8")
    (ws.work_dir / "flag2.txt").write_text(f"out {FLAG2}\n", encoding="utf-8")
    events: list = []
    registry = ToolRegistry()
    register_builtin_tools(registry)
    registry.register(SleepTool(0.6))   # fake_sleep：拿第二面前的间隔
    registry.register(SleepTool(30.0))  # 复用同名注册会被覆盖——改用独立名
    registry._tools.pop("fake_sleep")
    registry.register(type("S1", (SleepTool,), {"name": "sleep_short"})(2.0))
    registry.register(type("S2", (SleepTool,), {"name": "sleep_long"})(30.0))

    gate = FlagGate(ws, flag_format=r"flag\{[^{}]*\}", expected_flags=2)
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws, background=background, emit=events.append,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l), flag_gate=gate,
    )
    routes = [
        ("规划器", [("text", '{"verdict":"explore","intents":[],"audit":[]}')]),
        ("拿第一面", [("tool", "shell_exec", {"command": READ1}), ("tool", "submit_flag", {"flag": FLAG1}), ("text", "ok")]),
        ("拿第二面", [("tool", "sleep_short", {}), ("tool", "shell_exec", {"command": READ2}), ("tool", "submit_flag", {"flag": FLAG2}), ("text", "ok")]),
        ("长跑方向", [("tool", "sleep_long", {}), ("text", "不该到这里")]),
    ]
    provider = FakeProvider(routes=routes, script=[("text", "x")])
    factory = WorkerFactory(provider, registry, PermissionChain(mode="yolo"), ctx, events.append, None, gate, max_steps=6)
    coordinator = Coordinator(
        board=ctx.facts, planner=ReasonPlanner(provider), factory=factory,
        emit=events.append, rollout=None, gate=gate, objective="多 flag 测试",
        budget=Budget(), workers=3, race_minutes=0, tick=0.05, planner_cooldown=0.1,
        spawn_batch=3,
    )
    ctx.facts.create_intent("拿第一面", "file", "local", "read-1", actor="test")
    ctx.facts.create_intent("拿第二面", "file", "local", "read-2", actor="test")
    ctx.facts.create_intent("长跑方向", "misc", "local", "sleep-x", actor="test")

    solve_task = asyncio.create_task(coordinator.solve())
    # 等第一面过闸
    deadline = time.monotonic() + 15
    while len(gate.flags) < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert len(gate.flags) == 1 and not gate.solved
    first_at = time.monotonic()
    # 第一面后：长跑 worker 不应被立即取消（等第二面期间它仍在跑）
    await asyncio.sleep(0.5)
    running = [wid for wid, t in coordinator._worker_tasks.items() if not t.done()]
    assert running, "第一面后所有 worker 都没了（被误杀）"
    # 等第二面 + 全量取消
    while not gate.solved and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert gate.solved and len(gate.flags) == 2
    solved_at = time.monotonic()
    code = await solve_task
    assert code == 0
    assert time.monotonic() - solved_at < 2.0, "凑齐后未在 2s 内全部取消"  # 负载下留余量；不取消会到 30s 量级
    assert time.monotonic() - first_at < 9.0, "像是在等 sleep_long(30s) 自然结束"

    notes = [e.message for e in events if getattr(e, "kind", "") == "swarm_note"]
    assert any("第 1/2 面 flag 过闸，继续" in n for n in notes), notes
    assert any("已全部到手" in n and "终止" in n for n in notes), notes
    ctx.facts.close()


# ------------------------------------------------------------ writeup flags
def test_writeup_flags_array(tmp_path):
    run = tmp_path / "run-x"
    run.mkdir()
    (run / "rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "run_id": "run-x", "objective": "多 flag 题", "ts": time.time()}) + "\n",
        encoding="utf-8",
    )
    (run / "winner.json").write_text(
        json.dumps({"flag": FLAG2, "flags": [FLAG1, FLAG2], "solved": True, "reason": "ok"}),
        encoding="utf-8",
    )
    out = generate_writeup(run)
    assert out is not None
    text = out.read_text(encoding="utf-8")
    assert FLAG1 in text and FLAG2 in text and "已集齐" in text

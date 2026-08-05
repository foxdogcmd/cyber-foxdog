"""swarm 端到端：planner 产 intent → worker 认领 → 读 flag → 过闸 → 全员停 → exit 0。"""
from __future__ import annotations

import json
import sys

from foxdog.core import context as ctx_mod
from foxdog.core.budget import Budget
from foxdog.core.goal import EXIT_SOLVED
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.swarm.coordinator import Coordinator
from foxdog.swarm.planner import ReasonPlanner
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools

FLAG = "flag{swarm_e2e_ok}"
READ_CMD = "type flag.txt" if sys.platform == "win32" else "cat flag.txt"
PLAN_JSON = json.dumps(
    {
        "verdict": "explore",
        "intents": [
            {
                "goal": "读工作区 flag.txt 拿到 flag 并 submit_flag",
                "category": "file",
                "surface": "local",
                "technique": "read",
            }
        ],
        "audit": [],
    },
    ensure_ascii=False,
)


async def test_swarm_e2e(tmp_path):
    ws = Workspace(base=tmp_path, run_id="swarm-e2e")
    # 预置"目标侧"flag 文件：flag 只能出现在工具输出里（FlagGate 污染扫描）
    (ws.work_dir / "flag.txt").write_text(f"目标输出: {FLAG}\n", encoding="utf-8")
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": ws.run_id, "objective": "e2e"})

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
    provider = FakeProvider(
        routes=[
            # planner 角色：返回 1 个 intent（goal=读 flag）
            ("规划器", [("text", PLAN_JSON)]),
            # worker 角色：cat 预置 flag 文件 → submit_flag
            ("领取任务 intent", [
                ("tool", "shell_exec", {"command": READ_CMD}),
                ("tool", "submit_flag", {"flag": FLAG}),
                ("text", "flag 已提交"),
            ]),
        ],
        script=[("text", "主 agent 在 swarm 模式不应被调用")],
    )
    factory = WorkerFactory(provider, registry, PermissionChain(mode="yolo"), ctx, events.append, rollout, gate, max_steps=10)
    coordinator = Coordinator(
        board=ctx.facts,
        planner=ReasonPlanner(provider),
        factory=factory,
        emit=events.append,
        rollout=rollout,
        gate=gate,
        objective="读 flag",
        budget=Budget(max_minutes=2),
        workers=3,
        race_minutes=0,  # 跳过 race，直接验 swarm 主循环
        tick=0.1,
        planner_cooldown=0.2,
    )
    code = await coordinator.solve()
    await background.stop_all()
    await rollout.close()

    # 1. exit 0 + flag 正确
    assert code == EXIT_SOLVED
    assert gate.solved and gate.flag == FLAG

    # 2. 黑板：intent 被 planner 创建、被 worker 认领并收尾 done
    intents = ctx.facts.intents()
    assert len(intents) == 1
    intent = intents[0]
    assert intent["status"] == "done"
    assert intent["claimed_by"] == "worker-1"
    assert intent["goal"] == "读工作区 flag.txt 拿到 flag 并 submit_flag"

    # 3. 事件带 worker 前缀
    worker_events = [e for e in events if getattr(e, "worker", None) == "worker-1"]
    kinds = {e.kind for e in worker_events}
    assert "tool_call_begin" in kinds and "swarm_note" in kinds

    # 4. rollout 有 swarm_plan 与 worker 起止记录
    records = [json.loads(l) for l in (ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds_r = {r.get("kind") for r in records if r.get("type") == "event"}
    assert "swarm_plan" in kinds_r and "worker_start" in kinds_r
    ctx.facts.close()

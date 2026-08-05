"""子 agent：spawn 2 个并行 worker 收齐汇报；父 solved 共享；深度上限 1。"""
from __future__ import annotations

import asyncio
import sys

from foxdog.core import context as ctx_mod
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.swarm.facts import FactsBoard
from foxdog.swarm.worker import WorkerFactory
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools
from foxdog.tools.builtin.subagent import ListAgentsTool, SpawnAgentTool, SubAgentManager, WaitAgentTool

READ_CMD = "type flag.txt" if sys.platform == "win32" else "cat flag.txt"


def _make_stack(tmp_path, provider, run_id="subagent-test"):
    ws = Workspace(base=tmp_path, run_id=run_id)
    events: list = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
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
    permission = PermissionChain(mode="yolo")
    factory = WorkerFactory(provider, registry, permission, ctx, events.append, None, gate, max_steps=10)
    manager = SubAgentManager(factory, ctx.facts)
    registry.register(SpawnAgentTool(manager))
    registry.register(WaitAgentTool(manager))
    registry.register(ListAgentsTool(manager))
    orchestrator = ToolOrchestrator(registry, permission, ctx)
    loop = AgentLoop(provider, registry, orchestrator, events.append, None, gate, max_steps=10)
    return ws, events, rollout, gate, background, factory, manager, loop


async def test_spawn_two_and_wait(tmp_path):
    provider = FakeProvider(
        routes=[
            ("分析甲文件", [("tool", "shell_exec", {"command": "echo alpha-1"}), ("text", "甲分析完了 alpha")]),
            ("分析乙文件", [("tool", "shell_exec", {"command": "echo beta-2"}), ("text", "乙分析完了 beta")]),
        ],
        script=[
            ("tool", "spawn_agent", {"prompt": "分析甲文件", "worker_id": "a1"}),
            ("tool", "spawn_agent", {"prompt": "分析乙文件", "worker_id": "a2"}),
            ("tool", "wait_agent", {"ids": ["a1", "a2"], "timeout_s": 15}),
            ("text", "收齐了"),
        ],
    )
    ws, events, rollout, gate, background, factory, manager, loop = _make_stack(tmp_path, provider)
    history = [system_message("sys"), user_message("解题")]
    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()

    assert result.stop_reason == "completed"
    # wait_agent 的工具结果里收齐两个子 agent 的汇报
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    wait_msg = next(m for m in tool_msgs if "[a1]" in m["content"])
    assert "甲分析完了 alpha" in wait_msg["content"]
    assert "乙分析完了 beta" in wait_msg["content"]
    # 子 agent 事件带 worker 前缀
    worker_events = [e for e in events if getattr(e, "worker", None) in ("a1", "a2")]
    assert any(e.kind == "tool_call_begin" for e in worker_events)
    # 深度上限：子 agent registry 无 spawn 系工具
    child_loop = factory.make_loop("x")
    assert child_loop.registry.get("spawn_agent") is None
    assert child_loop.registry.get("shell_exec") is not None


async def test_child_solved_stops_parent(tmp_path):
    flag = "flag{subagent_test_ok}"
    provider = FakeProvider(
        routes=[
            ("拿 flag", [
                ("tool", "shell_exec", {"command": READ_CMD}),
                ("tool", "submit_flag", {"flag": flag}),
                ("text", "子 agent 拿到 flag 了"),
            ]),
        ],
        script=[
            ("tool", "spawn_agent", {"prompt": "拿 flag"}),
            ("tool", "wait_agent", {"ids": ["agent-1"], "timeout_s": 15}),
            ("text", "这段不该被用到"),  # solved 后 loop 不再调 provider
        ],
    )
    ws, events, rollout, gate, background, factory, manager, loop = _make_stack(
        tmp_path, provider, run_id="subagent-solved"
    )
    (ws.work_dir / "flag.txt").write_text(f"目标输出: {flag}\n", encoding="utf-8")
    history = [system_message("sys"), user_message("解题")]
    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()

    assert gate.solved and gate.flag == flag
    assert result.stop_reason == "solved"  # 父 loop 下一 step 前看到共享闸门已开


async def test_stop_all_cancels_inflight_subagent_stream(tmp_path):
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def stream_chat(
            self, _messages, _tools, _on_delta, *, require_tool=False, on_reasoning_delta=None
        ):
            del require_tool, on_reasoning_delta
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    provider = BlockingProvider()
    _ws, _events, _rollout, _gate, background, _factory, manager, _loop = _make_stack(
        tmp_path, provider, run_id="subagent-cancel"
    )
    manager.spawn("慢分析", "slow-child")
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    await asyncio.wait_for(manager.stop_all(), timeout=0.5)

    assert provider.cancelled.is_set()
    assert manager.list()[0]["error"] == "cancelled"
    assert not manager.list()[0]["running"]
    await background.stop_all()

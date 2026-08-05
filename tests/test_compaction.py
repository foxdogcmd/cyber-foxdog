"""上下文压缩：微压缩归档占位、全量压缩重建历史、rollout CompactionRecord。"""
from __future__ import annotations

import json

from foxdog.core import context as ctx_mod
from foxdog.core.compaction import CompactionManager, estimate_tokens
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import assistant_to_openai, system_message, tool_result_message, user_message, AssistantMessage
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools
from foxdog.llm.messages import ToolCall


def _step(history, call_id: str, output: str):
    """造一步 assistant(tool_call) + tool 结果。"""
    msg = AssistantMessage(
        content="",
        tool_calls=[ToolCall(id=call_id, name="shell_exec", arguments='{"command":"x"}')],
    )
    history.append(assistant_to_openai(msg))
    history.append(tool_result_message(call_id, output))


def test_micro_compact_replaces_old_large_outputs():
    cm = CompactionManager(FakeProvider(), window_tokens=10**9)
    history = [system_message("sys"), user_message("go")]
    archived = {}
    big = "X" * 2000
    for i in range(8):
        cid = f"c{i}"
        _step(history, cid, big if i < 7 else "small")
        archived[cid] = f"handle{i}"
    replaced = cm.micro_compact(history, archived)
    # 8 步里只有第 1 步（其后 7 个 step > keep_steps=6）且 >1KB 的被替换
    assert replaced == 1
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    assert tool_msgs[0]["content"].startswith("[已归档 handle=handle0")
    assert big not in tool_msgs[0]["content"]
    for m in tool_msgs[1:7]:
        assert m["content"] == big  # 最近 6 步内不动
    # 幂等：再跑一遍不重复替换
    assert cm.micro_compact(history, archived) == 0


async def test_full_compact_rebuilds_history(tmp_path):
    summary_text = "目标 10.0.0.1:8080 nginx；已验证事实：.git 泄露；已排除：直接爆破；当前假设：源码里有 key；已写文件：probe.py；flag 状态：无候选"
    compact_provider = FakeProvider(script=[("text", summary_text)])
    cm = CompactionManager(
        compact_provider,
        window_tokens=100,  # 85% = 85 tokens = 340 字符，极易触发
        state_renderer=lambda: "当前 todo 计划：\n[x] 侦察\n\n已知事实：8080 nginx",
    )
    records: list[dict] = []
    history = [system_message("sys-prompt"), user_message("题目" + "长" * 500)]
    for i in range(3):
        _step(history, f"c{i}", "Y" * 500)

    rec = await cm.maybe_full_compact(history, records.append)
    assert rec is not None and rec.kind == "full"
    # 压缩后首条是 system，保留要素都在
    assert history[0]["role"] == "system" and history[0]["content"] == "sys-prompt"
    body = history[1]["content"]
    assert "目标 10.0.0.1:8080" in body  # 摘要（保留要素）
    assert "当前 todo 计划" in body  # 重渲染状态
    assert len(history) == 2
    # CompactionRecord 已记录
    assert records and records[0]["type"] == "compaction" and records[0]["kind"] == "full"
    assert records[0]["before_tokens"] > records[0]["after_tokens"]


async def test_loop_triggers_full_compaction(tmp_path):
    """端到端：loop 里塞超长历史，下一步前触发全量压缩，rollout 有 CompactionRecord。"""
    ws = Workspace(base=tmp_path, run_id="compact-loop")
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
    solver = FakeProvider(
        script=[
            ("tool", "shell_exec", {"command": "echo " + "A" * 5000}),
            ("text", "分析完毕"),
        ]
    )
    compact_provider = FakeProvider(script=[("text", "压缩摘要：目标是 echo 大输出")])
    cm = CompactionManager(
        compact_provider,
        window_tokens=1000,  # 阈值 850 tokens = 3400 字符；echo 5000 字后必触发
        state_renderer=lambda: "(state)",
    )
    loop = AgentLoop(
        provider=solver,
        registry=registry,
        orchestrator=orchestrator,
        emit=events.append,
        rollout=rollout,
        flag_gate=gate,
        max_steps=10,
        compaction=cm,
    )
    history = [system_message("sys"), user_message("解题")]
    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "completed"
    # 压缩后首条仍是 system，摘要进了历史
    assert history[0]["role"] == "system"
    assert any("压缩摘要" in (m.get("content") or "") for m in history)
    # rollout 里有 CompactionRecord
    records = [json.loads(l) for l in (ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()]
    comp = [r for r in records if r.get("type") == "compaction" and r.get("kind") == "full"]
    assert comp, "rollout 缺 full CompactionRecord"
    assert comp[0]["summary"].startswith("压缩摘要")
    assert any(
        event.kind == "model_request_started" and event.worker == "compact" for event in events
    )
    assert any(
        event.kind == "model_request_complete" and event.worker == "compact" for event in events
    )


def test_estimate_tokens():
    history = [{"role": "user", "content": "a" * 400}]
    assert estimate_tokens(history) == 100

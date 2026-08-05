"""端到端：FakeProvider 驱动 loop 跑通「分析→调工具→拿 flag」。

脚本：预置目标侧 flag 文件（模拟 flag 由目标系统产出）→ 第 1 步 shell_exec
读取该文件（命令本身不含 flag，否则被 FlagGate 污染扫描拒收）→ 第 2 步
submit_flag → loop 判定 solved。
验证：事件序列、rollout 落盘、FlagGate 通过、winner.json、stop_reason=solved。
"""
from __future__ import annotations

import asyncio
import json
import sys

import anthropic
import httpx

from foxdog.core.budget import Budget
from foxdog.core.goal import EXIT_BUDGET, EXIT_ERROR, EXIT_SOLVED, GoalMode
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.core import context as ctx_mod
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import AssistantMessage, ToolCall, system_message, user_message
from foxdog.protocol.types import Event
from foxdog.store.rollout import RolloutWriter
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools

FLAG = "flag{test_123}"


class SequenceProvider:
    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)
        self.require_tool: list[bool] = []

    async def stream_chat(
        self, messages, tools, on_delta, *, require_tool=False, on_reasoning_delta=None
    ):
        self.require_tool.append(require_tool)
        response = self.responses.pop(0)
        if response.reasoning_content and on_reasoning_delta is not None:
            on_reasoning_delta(response.reasoning_content)
        if response.content:
            on_delta(response.content)
        return response


class AnthropicBadRequestThenSuccessProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self, messages, tools, on_delta, *, require_tool=False, on_reasoning_delta=None
    ):
        self.calls += 1
        if self.calls == 1:
            response = httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages"),
            )
            raise anthropic.BadRequestError("invalid tool adjacency", response=response, body={})
        on_delta("重建后恢复")
        return AssistantMessage(content="重建后恢复", finish_reason="stop")


async def _make_test_loop(tmp_path, run_id: str, provider, *, max_steps: int = 10):
    ws = Workspace(base=tmp_path, run_id=run_id)
    events: list[Event] = []
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
    loop = AgentLoop(provider, registry, orchestrator, events.append, rollout, gate, max_steps=max_steps)
    return ws, events, rollout, background, loop


async def test_loop_solves_with_fake_provider(tmp_path):
    ws = Workspace(base=tmp_path, run_id="loop-test")
    events: list[Event] = []
    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start({"run_id": ws.run_id, "cwd": str(tmp_path), "objective": "test", "model": "fake"})

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
    # 预置"目标侧"flag 文件：flag 只能由读文件产生，模拟真实解题的产出路径
    (ws.work_dir / "target_flag.txt").write_text(f"congrats {FLAG}\n", encoding="utf-8")
    read_cmd = "type target_flag.txt" if sys.platform == "win32" else "cat target_flag.txt"
    provider = FakeProvider(
        script=[
            ("tool", "shell_exec", {"command": read_cmd}),
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
    history = [system_message("你是 CTF solver"), user_message("解题")]
    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()
    await rollout.close()

    # 1. stop_reason 与 FlagGate 状态
    assert result.stop_reason == "solved", result
    assert gate.solved and gate.flag == FLAG
    winner = ws.read_winner()
    assert winner and winner["flag"] == FLAG

    # 2. 事件序列
    kinds = [e.kind for e in events]
    assert kinds[0] == "turn_started"
    begin_names = [e.name for e in events if e.kind == "tool_call_begin"]
    end_names = [e.name for e in events if e.kind == "tool_call_end"]
    assert begin_names == ["shell_exec", "submit_flag"]
    assert end_names == ["shell_exec", "submit_flag"]
    flag_events = [e for e in events if e.kind == "flag_verified"]
    assert len(flag_events) == 1 and flag_events[0].ok and flag_events[0].flag == FLAG
    assert kinds[-1] == "turn_complete"
    assert events[-1].stop_reason == "solved"  # type: ignore[attr-defined]

    # 3. rollout 落盘
    lines = (ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(l) for l in lines]
    assert records[0]["type"] == "session_meta"
    types = [r["type"] for r in records[1:]]
    assert "tool_call" in types and "tool_result" in types
    tool_results = [r for r in records if r["type"] == "tool_result"]
    assert any(FLAG in r["content"] for r in tool_results)  # echo 输出归档

    # 4. 历史回填：assistant + tool 消息邻接
    roles = [m["role"] for m in history]
    assert roles[:2] == ["system", "user"]
    assert "tool" in roles


async def test_reasoning_only_truncation_recovers_inside_same_turn(tmp_path):
    provider = SequenceProvider(
        [
            AssistantMessage(
                reasoning_content="思考耗尽输出额度",
                finish_reason="length",
                usage={"total_tokens": 4096},
            ),
            AssistantMessage(
                tool_calls=[ToolCall(id="recover-1", name="shell_exec", arguments='{"command":"Write-Output recovered"}')],
                finish_reason="tool_calls",
            ),
            AssistantMessage(content="已完成一个实际动作", finish_reason="stop"),
        ]
    )
    ws, events, rollout, background, loop = await _make_test_loop(tmp_path, "truncated-recovery", provider)
    history = [system_message("sys"), user_message("解题")]

    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "completed" and result.steps == 1
    assert provider.require_tool == [False, True, False]
    assert sum(event.kind == "turn_started" for event in events) == 1
    assert sum(event.kind == "turn_complete" for event in events) == 1
    starts = [event for event in events if event.kind == "model_request_started"]
    completes = [event for event in events if event.kind == "model_request_complete"]
    reasoning = [event.delta for event in events if event.kind == "agent_reasoning_delta"]
    assert len(starts) == len(completes) == 3
    assert [event.request_id for event in starts] == [event.request_id for event in completes]
    assert reasoning == ["思考耗尽输出额度"]
    assert completes[0].reasoning_chars == len("思考耗尽输出额度")
    assert all(event.duration_ms >= 0 for event in completes)
    assert not any(
        msg.get("role") == "assistant" and msg.get("content") is None and not msg.get("tool_calls")
        for msg in history
    )
    records = [json.loads(line) for line in (ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()]
    recovery = next(record for record in records if record.get("type") == "model_recovery")
    assert recovery["reason"] == "truncated"


async def test_truncated_partial_tool_call_is_discarded_before_recovery(tmp_path):
    provider = SequenceProvider(
        [
            AssistantMessage(
                tool_calls=[ToolCall(
                    id="partial",
                    name="shell_exec",
                    arguments='{"command":"Set-Content -LiteralPath partial-should-not-run.txt -Value bad"}',
                )],
                finish_reason="length",
            ),
            AssistantMessage(
                tool_calls=[ToolCall(
                    id="recovered",
                    name="shell_exec",
                    arguments='{"command":"Write-Output recovered"}',
                )],
                finish_reason="tool_calls",
            ),
            AssistantMessage(content="实际动作已完成", finish_reason="stop"),
        ]
    )
    ws, events, rollout, background, loop = await _make_test_loop(tmp_path, "partial-tool", provider)
    history = [system_message("sys"), user_message("解题")]

    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "completed" and result.steps == 1
    assert not (ws.work_dir / "partial-should-not-run.txt").exists()
    assert provider.require_tool == [False, True, False]
    assert [event.name for event in events if event.kind == "tool_call_begin"] == ["shell_exec"]


async def test_filtered_response_never_executes_accumulated_tool_call(tmp_path):
    provider = SequenceProvider([
        AssistantMessage(
            tool_calls=[ToolCall(
                id="filtered",
                name="shell_exec",
                arguments='{"command":"Set-Content -LiteralPath filtered-should-not-run.txt -Value bad"}',
            )],
            finish_reason="content_filter",
        )
    ])
    ws, events, rollout, background, loop = await _make_test_loop(tmp_path, "filtered-tool", provider)

    result = await loop.run_turn([system_message("sys"), user_message("解题")], turn_no=1)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "error"
    assert not (ws.work_dir / "filtered-should-not-run.txt").exists()
    assert not any(event.kind == "tool_call_begin" for event in events)


async def test_required_tool_contract_retries_prose_without_polluting_history(tmp_path, monkeypatch):
    provider = SequenceProvider(
        [
            AssistantMessage(content="先总结一下", finish_reason="stop"),
            AssistantMessage(content="仍然只总结", finish_reason="stop"),
            AssistantMessage(
                tool_calls=[ToolCall(
                    id="required",
                    name="shell_exec",
                    arguments='{"command":"Write-Output action"}',
                )],
                finish_reason="tool_calls",
            ),
            AssistantMessage(content="动作完成", finish_reason="stop"),
        ]
    )
    _ws, _events, rollout, background, loop = await _make_test_loop(tmp_path, "required-tool", provider)
    history = [system_message("sys"), user_message("继续解题")]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("foxdog.core.loop.asyncio.sleep", no_sleep)
    result = await loop.run_turn(history, turn_no=2, require_first_tool=True)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "completed" and result.steps == 1
    assert provider.require_tool == [True, True, True, False]
    assert all(msg.get("content") not in {"先总结一下", "仍然只总结"} for msg in history)


async def test_required_tool_contract_has_bounded_failure(tmp_path, monkeypatch):
    provider = SequenceProvider([AssistantMessage(content=f"总结 {i}", finish_reason="stop") for i in range(3)])
    _ws, events, rollout, background, loop = await _make_test_loop(tmp_path, "required-tool-failure", provider)
    history = [system_message("sys"), user_message("继续解题")]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("foxdog.core.loop.asyncio.sleep", no_sleep)
    result = await loop.run_turn(history, turn_no=2, require_first_tool=True)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "error"
    assert provider.require_tool == [True, True, True]
    assert history == [system_message("sys"), user_message("继续解题")]
    assert any(event.kind == "error" and "忽略强制工具调用" in event.message for event in events)


async def test_empty_model_response_retries_then_fails_without_history_pollution(tmp_path, monkeypatch):
    provider = SequenceProvider([AssistantMessage(finish_reason="stop") for _ in range(3)])
    _ws, events, rollout, background, loop = await _make_test_loop(tmp_path, "empty-response", provider)
    history = [system_message("sys"), user_message("解题")]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("foxdog.core.loop.asyncio.sleep", no_sleep)
    result = await loop.run_turn(history, turn_no=1)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "error"
    assert len(provider.require_tool) == 3
    assert history == [system_message("sys"), user_message("解题")]
    assert any(event.kind == "error" and "空响应" in event.message for event in events)


async def test_anthropic_bad_request_uses_same_history_rebuild_path(tmp_path):
    provider = AnthropicBadRequestThenSuccessProvider()
    _ws, _events, rollout, background, loop = await _make_test_loop(tmp_path, "anthropic-rebuild", provider)

    result = await loop.run_turn([system_message("sys"), user_message("解题")], turn_no=1)
    await background.stop_all()
    await rollout.close()

    assert result.stop_reason == "completed"
    assert provider.calls == 2


async def test_goal_continues_after_per_turn_step_cap(tmp_path):
    read_cmd = (
        "Get-Content -Raw -LiteralPath 'target_flag.txt'"
        if sys.platform == "win32"
        else "cat target_flag.txt"
    )
    provider = FakeProvider(
        script=[
            ("tool", "shell_exec", {"command": read_cmd}),
            ("tool", "submit_flag", {"flag": FLAG}),
        ]
    )
    ws, events, rollout, background, loop = await _make_test_loop(
        tmp_path, "step-cap-continuation", provider, max_steps=1
    )
    (ws.work_dir / "target_flag.txt").write_text(FLAG, encoding="utf-8")
    history = [system_message("sys"), user_message("解题")]

    code = await GoalMode(loop, Budget(max_minutes=5)).solve(history)
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_SOLVED
    reasons = [event.stop_reason for event in events if event.kind == "turn_complete"]
    assert reasons == ["max_steps", "solved"]


async def test_goal_stops_after_repeated_no_progress_turns(tmp_path):
    provider = SequenceProvider(
        [AssistantMessage(reasoning_content="仍在思考", finish_reason="length") for _ in range(9)]
    )
    _ws, events, rollout, background, loop = await _make_test_loop(tmp_path, "stalled-goal", provider)
    history = [system_message("sys"), user_message("解题")]

    goal = GoalMode(loop, Budget(max_minutes=5))
    code = await goal.solve(history)
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_ERROR and goal.last_stop_reason == "stalled"
    assert any(event.kind == "error" and "没有工具进展" in event.message for event in events)


async def test_goal_records_max_turn_budget_reason(tmp_path):
    provider = FakeProvider(script=[("text", "本轮自然暂停")])
    _ws, _events, rollout, background, loop = await _make_test_loop(tmp_path, "turn-budget", provider)
    goal = GoalMode(loop, Budget(max_turns=1))

    code = await goal.solve([system_message("sys"), user_message("解题")])
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_BUDGET
    assert goal.last_stop_reason == "max_turns"


async def test_goal_interrupt_at_turn_boundary_does_not_append_continuation(tmp_path):
    interrupt = asyncio.Event()

    class BoundaryInterruptProvider:
        async def stream_chat(
            self, _messages, _tools, on_delta, *, require_tool=False, on_reasoning_delta=None
        ):
            del require_tool, on_reasoning_delta
            on_delta("本轮回答刚完成")
            interrupt.set()
            return AssistantMessage(content="本轮回答刚完成", finish_reason="stop")

    provider = BoundaryInterruptProvider()
    _ws, _events, rollout, background, loop = await _make_test_loop(
        tmp_path, "boundary-interrupt", provider
    )
    loop.interrupt = interrupt
    history = [system_message("sys"), user_message("解题")]
    goal = GoalMode(loop, Budget())

    code = await goal.solve(history)
    await background.stop_all()
    await rollout.close()

    assert code == EXIT_ERROR
    assert goal.last_stop_reason == "interrupted"
    assert not any(
        message.get("role") == "user" and "目标续跑 turn" in (message.get("content") or "")
        for message in history
    )

"""Agent 主循环（借鉴 kimi-code loop + codex turn.rs）：无状态 turn/step。

step：capture_step_context（消息历史快照+工具表）→ 流式调 LLM（发 AgentMessageDelta）
→ 收 tool calls → scheduler 执行（orchestrator 发 ToolCallBegin/End）→ 结果回填。

护栏：max_steps（默认 200）；每 step 结束检查 solved 与 Interrupt；
provider 返回 400（tool_use 邻接类错误）时严格重建消息重发一次。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import anthropic
import openai
from pydantic import BaseModel, Field

from ..gate.flag_gate import FlagGate
from ..llm.client import ChatProvider
from ..llm.messages import AssistantMessage, assistant_to_openai, tool_result_message, user_message
from ..protocol.types import (
    AgentMessageDelta,
    AgentReasoningDelta,
    BackgroundTaskComplete,
    Error,
    Event,
    ModelRequestComplete,
    ModelRequestStarted,
    SwarmNote,
    TurnComplete,
    TurnStarted,
)
from ..store.rollout import RolloutWriter
from ..tools import Tool, ToolRegistry
from .budget import BudgetExceeded, BudgetState
from .orchestrator import ToolOrchestrator
from .scheduler import run_tool_calls

if TYPE_CHECKING:
    from .compaction import CompactionManager


class TurnResult(BaseModel):
    stop_reason: str  # completed | truncated | empty | solved | max_steps | interrupted | error
    steps: int = 0
    usage: dict[str, int] = Field(default_factory=dict)
    message: str = ""  # stop_reason=error 时的错误信息


_TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "truncated"}
_FILTERED_FINISH_REASONS = {"content_filter", "filtered", "refusal"}
_EMPTY_RESPONSE_RETRIES = 2
_TRUNCATION_RECOVERIES = 2
_REQUIRED_TOOL_RETRIES = 2


def _merge_usage(total: dict[str, int], delta: dict[str, Any]) -> dict[str, int]:
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        if isinstance(delta.get(key), int):
            total[key] = total.get(key, 0) + delta[key]
    details = delta.get("completion_tokens_details")
    if hasattr(details, "model_dump"):
        details = details.model_dump()
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
        total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + details["reasoning_tokens"]
    return total


def _rebuild_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """400 降级重建：丢弃没有 tool 结果配对的 tool_calls（及其 assistant 消息），
    丢弃没有归属的 tool 消息，保证 tool_use/tool_result 邻接合法。"""
    out: list[dict[str, Any]] = []
    valid_tool_ids: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            ids = [tc.get("id") for tc in msg["tool_calls"]]
            # 该 assistant 之后必须紧跟全部对应 tool 消息才算合法；此处先记账
            valid_tool_ids.update(i for i in ids if i)
            out.append(msg)
        elif role == "tool":
            if msg.get("tool_call_id") in valid_tool_ids:
                out.append(msg)
            # 孤儿 tool 消息丢弃
        else:
            out.append(msg)
    # 反向检查：assistant 的 tool_calls 若缺结果，降级为纯文本
    answered: set[str] = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    fixed: list[dict[str, Any]] = []
    for msg in out:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            if all(tc.get("id") in answered for tc in msg["tool_calls"]):
                fixed.append(msg)
            else:
                note = msg.get("content") or "[工具调用被降级移除]"
                fixed.append({"role": "assistant", "content": note})
        else:
            fixed.append(msg)
    return fixed


class AgentLoop:
    def __init__(
        self,
        provider: ChatProvider,
        registry: ToolRegistry,
        orchestrator: ToolOrchestrator,
        emit: Callable[[Event], None],
        rollout: RolloutWriter | None = None,
        flag_gate: FlagGate | None = None,
        max_steps: int = 200,
        interrupt: asyncio.Event | None = None,
        budget_state: BudgetState | None = None,
        compaction: "CompactionManager | None" = None,
        worker: str | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.orchestrator = orchestrator
        self.emit = emit
        self.rollout = rollout
        self.flag_gate = flag_gate
        self.max_steps = max_steps
        self.interrupt = interrupt or asyncio.Event()
        self.budget_state = budget_state  # GoalMode.solve 时注入（step 级预算强制）
        self.compaction = compaction
        self.worker = worker  # 子 agent/worker 标识：rollout 记录带此前缀
        self._model_request_seq = 0
        #: tool_call_id → CAS 句柄（orchestrator 对每个工具输出归档；微压缩引用）
        self.archived_handles: dict[str, str] = {}

    def _record(self, record: dict[str, Any]) -> None:
        if self.rollout:
            if self.worker:
                record.setdefault("worker", self.worker)
            self.rollout.write(record)

    @property
    def solved(self) -> bool:
        return self.flag_gate is not None and self.flag_gate.solved

    def _drain_background_completions(self, history: list[dict[str, Any]]) -> None:
        """Archive detached command results and inject each completion into agent context once."""
        ctx = self.orchestrator.ctx
        for task in ctx.background.drain_completed():
            result = task.result
            exit_code = result.exit_code if result is not None else None
            timed_out = bool(result and (result.timed_out or result.idle_timed_out))
            ok = bool(result and result.error is None and exit_code == 0 and not timed_out and not task.cancelled)
            if task.cancelled:
                status = "cancelled"
            elif result is None:
                status = "unknown"
            elif result.error:
                status = f"error: {result.error}"
            elif result.idle_timed_out:
                status = f"idle-timeout, exit={exit_code}"
            elif result.timed_out:
                status = f"timeout, exit={exit_code}"
            else:
                status = f"exit={exit_code}"

            output = task.tail(4000)
            handle = result.handle if result is not None else None
            evidence = ""
            if result is not None:
                evidence = result.output
                if handle and ctx.workspace is not None and ctx.workspace.cas_exists(handle):
                    evidence = ctx.workspace.cas_fetch_text(handle)
            if evidence and ctx.workspace is not None:
                if ctx.flag_gate is not None:
                    ctx.flag_gate.output_scan(evidence)
                handle = handle or ctx.workspace.cas_store(evidence.encode("utf-8", errors="replace"))

            notice = (
                f"[后台任务完成通知]\ntask_id: {task.task_id}\ncommand: {task.command}\n"
                f"status: {status}\noutput:\n{output or '(no output)'}"
            )
            if handle:
                notice += f"\n[evidence_handle={handle}]"
            history.append(user_message(notice))
            self.emit(
                BackgroundTaskComplete(
                    task_id=task.task_id,
                    command=task.command,
                    ok=ok,
                    exit_code=exit_code,
                    output_preview=output[-1000:],
                    handle=handle,
                )
            )
            self._record(
                {
                    "type": "event",
                    "kind": "background_task_complete",
                    "task_id": task.task_id,
                    "command": task.command,
                    "ok": ok,
                    "status": status,
                    "handle": handle,
                }
            )

    async def run_turn(
        self,
        history: list[dict[str, Any]],
        turn_no: int,
        *,
        require_first_tool: bool = False,
    ) -> TurnResult:
        """跑到 agent 自然停（无 tool call）/ solved / 护栏触发为止。"""
        self.emit(TurnStarted(turn=turn_no))
        usage: dict[str, int] = {}
        steps = 0
        stop_reason = "completed"
        error_msg = ""
        empty_retries = 0
        truncation_recoveries = 0
        required_tool_retries = 0
        require_tool = require_first_tool

        while steps < self.max_steps:
            if self.interrupt.is_set():
                stop_reason = "interrupted"
                break
            if self.solved:
                stop_reason = "solved"
                break

            self._drain_background_completions(history)

            # step 级预算强制：超限抛 BudgetExceeded，GoalMode 捕获 → exit 2
            if self.budget_state is not None:
                try:
                    self.budget_state.check()
                except BudgetExceeded as exc:
                    self._record({"type": "event", "kind": "budget_exceeded", "detail": str(exc)})
                    self.emit(TurnComplete(turn=turn_no, stop_reason="budget", usage=usage))
                    raise

            # 上下文压缩：微压缩旧长输出；逼近窗口上限时全量压缩
            if self.compaction is not None:
                compact_request_id: str | None = None
                compact_started = 0.0
                compact_reasoning_chars = 0
                compact_content_chars = 0

                def on_compact_start() -> None:
                    nonlocal compact_request_id, compact_started
                    compact_request_id = f"compact:{turn_no}:{self._model_request_seq}"
                    self._model_request_seq += 1
                    compact_started = time.monotonic()
                    self.emit(
                        ModelRequestStarted(
                            request_id=compact_request_id,
                            turn=turn_no,
                            step=steps + 1,
                            worker="compact",
                        )
                    )

                def on_compact_delta(text: str) -> None:
                    nonlocal compact_content_chars
                    compact_content_chars += len(text)

                def on_compact_reasoning(text: str) -> None:
                    nonlocal compact_reasoning_chars
                    compact_reasoning_chars += len(text)
                    if compact_request_id is not None and not self.interrupt.is_set():
                        self.emit(
                            AgentReasoningDelta(
                                request_id=compact_request_id,
                                delta=text,
                                worker="compact",
                            )
                        )

                try:
                    await self.compaction.step(
                        history,
                        self.archived_handles,
                        self._record,
                        on_full_start=on_compact_start,
                        on_delta=on_compact_delta,
                        on_reasoning_delta=on_compact_reasoning,
                    )
                except asyncio.CancelledError:
                    if compact_request_id is not None:
                        self.emit(
                            ModelRequestComplete(
                                request_id=compact_request_id,
                                duration_ms=round((time.monotonic() - compact_started) * 1000),
                                ok=False,
                                error="cancelled",
                                reasoning_chars=compact_reasoning_chars,
                                content_chars=compact_content_chars,
                                worker="compact",
                            )
                        )
                    raise
                except Exception as exc:
                    if compact_request_id is not None:
                        self.emit(
                            ModelRequestComplete(
                                request_id=compact_request_id,
                                duration_ms=round((time.monotonic() - compact_started) * 1000),
                                ok=False,
                                error=f"{type(exc).__name__}: {exc}",
                                reasoning_chars=compact_reasoning_chars,
                                content_chars=compact_content_chars,
                                worker="compact",
                            )
                        )
                    raise
                if compact_request_id is not None:
                    self.emit(
                        ModelRequestComplete(
                            request_id=compact_request_id,
                            duration_ms=round((time.monotonic() - compact_started) * 1000),
                            reasoning_chars=compact_reasoning_chars,
                            content_chars=compact_content_chars,
                            worker="compact",
                        )
                    )

            # capture_step_context：本步快照（消息+工具表），配置变更不串味
            snapshot = list(history)
            tools_schema = self.registry.openai_tools()

            request_id = f"{turn_no}:{self._model_request_seq}"
            self._model_request_seq += 1
            model_started = time.monotonic()
            self.emit(ModelRequestStarted(request_id=request_id, turn=turn_no, step=steps + 1))
            self._record(
                {
                    "type": "event",
                    "kind": "model_request_started",
                    "request_id": request_id,
                    "turn": turn_no,
                    "step": steps + 1,
                }
            )
            try:
                requested_tool = require_tool
                msg = await self._stream_with_rebuild(
                    snapshot,
                    tools_schema,
                    history,
                    request_id=request_id,
                    require_tool=require_tool,
                )
                require_tool = False
            except asyncio.CancelledError:
                duration_ms = round((time.monotonic() - model_started) * 1000)
                self.emit(
                    ModelRequestComplete(
                        request_id=request_id,
                        duration_ms=duration_ms,
                        ok=False,
                        error="cancelled",
                    )
                )
                raise
            except Exception as exc:  # noqa: BLE001
                duration_ms = round((time.monotonic() - model_started) * 1000)
                self.emit(
                    ModelRequestComplete(
                        request_id=request_id,
                        duration_ms=duration_ms,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                stop_reason = "error"
                error_msg = f"{type(exc).__name__}: {exc}"
                self.emit(Error(message=error_msg))
                break
            duration_ms = round((time.monotonic() - model_started) * 1000)
            completion_tokens = msg.usage.get("completion_tokens")
            if not isinstance(completion_tokens, int):
                completion_tokens = None
            self.emit(
                ModelRequestComplete(
                    request_id=request_id,
                    duration_ms=duration_ms,
                    finish_reason=msg.finish_reason,
                    reasoning_chars=len(msg.reasoning_content or ""),
                    content_chars=len(msg.content),
                    completion_tokens=completion_tokens,
                )
            )
            self._record(
                {
                    "type": "event",
                    "kind": "model_request_complete",
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "finish_reason": msg.finish_reason,
                    "reasoning_chars": len(msg.reasoning_content or ""),
                    "content_chars": len(msg.content),
                    "completion_tokens": completion_tokens,
                }
            )
            _merge_usage(usage, msg.usage)
            if self.budget_state is not None:
                tokens = msg.usage.get("total_tokens")
                if isinstance(tokens, int):
                    self.budget_state.add_tokens(tokens)

            finish_reason = (msg.finish_reason or "").lower()
            has_text = bool(msg.content.strip())
            self._record(
                {
                    "type": "model_response",
                    "finish_reason": msg.finish_reason,
                    "content_chars": len(msg.content),
                    "reasoning_chars": len(msg.reasoning_content or ""),
                    "tool_calls": len(msg.tool_calls),
                    "usage": msg.usage,
                }
            )

            # Terminal diagnostics take precedence over accumulated tool deltas. A
            # length-limited stream may contain only half of a tool's JSON arguments;
            # executing that partial call would turn a provider truncation into a side effect.
            if finish_reason in _FILTERED_FINISH_REASONS:
                stop_reason = "error"
                discarded = f"，已丢弃 {len(msg.tool_calls)} 个工具调用" if msg.tool_calls else ""
                error_msg = f"模型响应被过滤（finish_reason={msg.finish_reason}{discarded}）"
                self.emit(Error(message=error_msg))
                break

            truncated = finish_reason in _TRUNCATED_FINISH_REASONS
            if truncated:
                empty_retries = 0
                if truncation_recoveries < _TRUNCATION_RECOVERIES:
                    truncation_recoveries += 1
                    history.append(
                        user_message(
                            "[系统恢复指令] 上一响应在产生完整、可执行的动作前达到输出上限。"
                            "上一响应中的文本和工具参数均视为不完整，禁止执行；不要重做长篇分析。"
                            "基于此前已确认的证据选择一个有界的下一步，现在必须调用一个工具推进解题。"
                        )
                    )
                    require_tool = True
                    self.emit(SwarmNote(message="模型输出达到上限，已丢弃不完整动作并在同一 turn 恢复"))
                    self._record(
                        {
                            "type": "model_recovery",
                            "reason": "truncated",
                            "attempt": truncation_recoveries,
                            "discarded_tool_calls": len(msg.tool_calls),
                        }
                    )
                    continue
                stop_reason = "truncated"
                error_msg = f"模型连续 {_TRUNCATION_RECOVERIES + 1} 次在产生完整动作前达到输出上限"
                break

            empty = not msg.tool_calls and not has_text
            if empty:
                # Kimi-style bounded same-step retry: a successful HTTP response with
                # no visible text or tool action is not a completed agent turn.
                if empty_retries < _EMPTY_RESPONSE_RETRIES:
                    empty_retries += 1
                    require_tool = requested_tool
                    if empty_retries == 1:
                        self.emit(SwarmNote(message="模型返回空响应，正在同一步重试"))
                    await asyncio.sleep(0.25 * (2 ** (empty_retries - 1)))
                    continue
                stop_reason = "error"
                error_msg = (
                    f"模型连续 {_EMPTY_RESPONSE_RETRIES + 1} 次返回空响应"
                    f"（finish_reason={msg.finish_reason or 'missing'}），已停止避免空转"
                )
                self.emit(Error(message=error_msg))
                break

            empty_retries = 0
            if requested_tool and not msg.tool_calls:
                # Some OpenAI-compatible proxies silently ignore tool_choice. Treat a
                # prose-only response as a contract violation, not a completed goal turn.
                if required_tool_retries < _REQUIRED_TOOL_RETRIES:
                    required_tool_retries += 1
                    require_tool = True
                    self.emit(SwarmNote(message="模型未按要求调用工具，正在同一步恢复执行"))
                    self._record(
                        {
                            "type": "model_recovery",
                            "reason": "required_tool_missing",
                            "attempt": required_tool_retries,
                        }
                    )
                    await asyncio.sleep(0.25 * (2 ** (required_tool_retries - 1)))
                    continue
                stop_reason = "error"
                error_msg = (
                    f"模型连续 {_REQUIRED_TOOL_RETRIES + 1} 次忽略强制工具调用，"
                    "已停止当前路径避免总结空转"
                )
                self.emit(Error(message=error_msg))
                break

            required_tool_retries = 0
            assistant_msg = assistant_to_openai(msg)
            # Do not persist thinking-only/null responses. DeepSeek does not need
            # reasoning replay between user turns unless a tool call was produced.
            if has_text or msg.tool_calls:
                history.append(assistant_msg)
            self._record({"type": "message", "message": assistant_msg})

            if not msg.tool_calls:
                stop_reason = "completed"
                break

            items: list[tuple[Any, Tool]] = []
            for call in msg.tool_calls:
                tool = self.registry.get(call.name)
                if tool is None:
                    # 未知工具：让 orchestrator 生成错误结果占位，保持顺序
                    items.append((call, None))  # type: ignore[arg-type]
                else:
                    items.append((call, tool))
            self._record(
                {
                    "type": "tool_call",
                    "calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in msg.tool_calls],
                }
            )

            results = await run_tool_calls(items, self.orchestrator)

            for call, result in zip(msg.tool_calls, results):
                content = result.render()
                history.append(tool_result_message(call.id, content))
                cas_handle = result.data.get("cas_handle")
                if isinstance(cas_handle, str):
                    self.archived_handles[call.id] = cas_handle
                self._record(
                    {
                        "type": "tool_result",
                        "call_id": call.id,
                        "name": call.name,
                        "ok": result.ok,
                        "content": content,
                        "handle": result.handle,
                    }
                )
            steps += 1
            truncation_recoveries = 0
            if self.solved:
                stop_reason = "solved"
                break
        else:
            stop_reason = "max_steps"

        self.emit(TurnComplete(turn=turn_no, stop_reason=stop_reason, usage=usage))
        self._record(
            {
                "type": "event",
                "kind": "turn_complete",
                "turn": turn_no,
                "stop_reason": stop_reason,
                "usage": usage,
            }
        )
        return TurnResult(stop_reason=stop_reason, steps=steps, usage=usage, message=error_msg)

    async def _stream_with_rebuild(
        self,
        snapshot: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]],
        history: list[dict[str, Any]],
        *,
        request_id: str,
        require_tool: bool = False,
    ) -> AssistantMessage:
        """流式调用；400（tool_use 邻接类）时严格重建消息重发一次。"""

        def on_delta(text: str) -> None:
            if not self.interrupt.is_set():
                self.emit(AgentMessageDelta(delta=text, request_id=request_id))

        def on_reasoning_delta(text: str) -> None:
            if not self.interrupt.is_set():
                self.emit(AgentReasoningDelta(request_id=request_id, delta=text))

        try:
            return await self.provider.stream_chat(
                snapshot,
                tools_schema,
                on_delta,
                require_tool=require_tool,
                on_reasoning_delta=on_reasoning_delta,
            )
        except (openai.BadRequestError, anthropic.BadRequestError):
            rebuilt = _rebuild_messages(snapshot)
            history[:] = rebuilt  # 后续 step 沿用降级投影
            return await self.provider.stream_chat(
                list(rebuilt),
                tools_schema,
                on_delta,
                require_tool=require_tool,
                on_reasoning_delta=on_reasoning_delta,
            )

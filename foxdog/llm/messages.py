"""消息类型与流式 tool_call 重组。

消息在 OpenAI wire 格式（dict）与本模块类型之间互转。
流式响应中 tool_calls 以增量块到达，必须按 index 对齐拼接：
id/name 只在首个块出现，arguments 逐块追加。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str = ""  # JSON 字符串


class AssistantMessage(BaseModel):
    """一步流式响应的最终产物。"""

    content: str = ""
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    # Kimi Preserved Thinking requires the complete assistant message to be
    # replayed on later turns, even when that response did not call a tool.
    # Other providers keep the historical behavior unless they opt in.
    preserve_reasoning_content: bool = False
    # Anthropic signed thinking/redacted_thinking blocks must be replayed
    # byte-for-byte across a tool-use round trip.  This is internal state;
    # assistant_to_openai exposes it under a private history key only when
    # there are tool calls, and OpenAI-compatible clients strip that key.
    anthropic_thinking_blocks: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = None


def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def assistant_to_openai(msg: AssistantMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
    if msg.reasoning_content is not None and (msg.tool_calls or msg.preserve_reasoning_content):
        # DeepSeek replays reasoning across tool steps; Kimi may opt into
        # Preserved Thinking for every assistant turn.
        out["reasoning_content"] = msg.reasoning_content
        if msg.reasoning_signature is not None:
            out["reasoning_signature"] = msg.reasoning_signature
    if msg.anthropic_thinking_blocks and msg.tool_calls:
        # Private history extension consumed only by to_anthropic().
        out["_anthropic_thinking_blocks"] = [dict(block) for block in msg.anthropic_thinking_blocks]
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in msg.tool_calls
        ]
    return out


def tool_result_message(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class ToolCallAccumulator:
    """按 index 对齐重组流式 tool_calls 增量。"""

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, str]] = {}

    def feed(self, delta: Any) -> None:
        """delta: openai 流式 chunk 里的单个 tool_call 增量（有 .index）。"""
        idx = int(getattr(delta, "index", 0) or 0)
        slot = self._slots.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if getattr(delta, "id", None):
            slot["id"] += delta.id
        fn = getattr(delta, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["name"] += fn.name
            if getattr(fn, "arguments", None):
                slot["arguments"] += fn.arguments

    def finish(self) -> list[ToolCall]:
        return [
            ToolCall(
                id=self._slots[i]["id"] or f"call_{i}",
                name=self._slots[i]["name"],
                arguments=self._slots[i]["arguments"],
            )
            for i in sorted(self._slots)
        ]

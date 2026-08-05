"""OpenAI Responses API 客户端（与 LLMClient 同 ChatProvider 接口）。

用于支持 protocol="responses" 的模型端点（OpenAI o-series 等）。
消息转换：OpenAI Chat 格式 → Responses input；流式事件 → on_delta/on_reasoning_delta。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable

import openai

from .client import ModelEndpoint, OnDelta, _is_retryable
from .messages import AssistantMessage, ToolCall, ToolCallAccumulator


def _to_responses_input(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """OpenAI chat messages → (instructions, responses input items)."""
    instructions: str | None = None
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if content:
                instructions = str(content) if instructions is None else f"{instructions}\n{content}"
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": str(content or ""),
            })
            continue
        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            reasoning = msg.get("reasoning_content")
            if reasoning and (tool_calls or msg.get("preserve_reasoning_content")):
                items.append({"type": "reasoning", "content": str(reasoning)})
            if content:
                items.append({"type": "message", "role": "assistant", "content": str(content)})
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", ""),
                    })
            continue
        # user or unknown
        items.append({"type": "message", "role": role or "user", "content": str(content or "")})
    return instructions, items


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI function tools → Responses function tools."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function":
            fn = tool.get("function", {})
            out.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
    return out


class ResponsesClient:
    """OpenAI Responses API 流式客户端。"""

    def __init__(
        self,
        endpoint: ModelEndpoint,
        max_retries: int = 4,
        backoff_base: float = 1.0,
    ) -> None:
        self._endpoint = endpoint
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def _client(self) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            base_url=self._endpoint.base_url,
            api_key=self._endpoint.resolved_key() or "EMPTY",
            max_retries=0,
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        *,
        require_tool: bool = False,
        on_reasoning_delta: OnDelta | None = None,
    ) -> AssistantMessage:
        last_exc: BaseException | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._stream_once(
                    messages, tools, on_delta,
                    require_tool=require_tool,
                    on_reasoning_delta=on_reasoning_delta,
                )
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError):
                    raise
                last_exc = exc
                if not _is_retryable(exc):
                    raise
                await asyncio.sleep(self.backoff_base * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        *,
        require_tool: bool = False,
        on_reasoning_delta: OnDelta | None = None,
    ) -> AssistantMessage:
        ep = self._endpoint
        instructions, input_items = _to_responses_input(messages)
        kwargs: dict[str, Any] = {
            "model": ep.model,
            "input": input_items,
            "stream": True,
        }
        if instructions:
            kwargs["instructions"] = instructions
        converted_tools = _convert_tools(tools)
        if converted_tools:
            kwargs["tools"] = converted_tools
            if require_tool:
                kwargs["tool_choice"] = "required"
        if ep.max_tokens:
            kwargs["max_output_tokens"] = ep.max_tokens
        if ep.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": ep.reasoning_effort}

        stream = await self._client().responses.create(**kwargs)

        parts: list[str] = []
        reasoning_parts: list[str] = []
        saw_reasoning = False
        tool_calls: list[ToolCall] = []
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        try:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    delta_text = getattr(event, "delta", "")
                    if delta_text:
                        parts.append(delta_text)
                        on_delta(delta_text)
                elif etype == "response.reasoning.delta":
                    delta_text = getattr(event, "delta", "")
                    if delta_text:
                        saw_reasoning = True
                        reasoning_parts.append(delta_text)
                        if on_reasoning_delta is not None:
                            on_reasoning_delta(delta_text)
                elif etype == "response.function_call_arguments.delta":
                    pass  # accumulated in the complete event
                elif etype == "response.function_call_arguments.done":
                    call_id = getattr(event, "item_id", "") or f"call_{len(tool_calls)}"
                    name = getattr(event, "name", "") or ""
                    arguments = getattr(event, "arguments", "")
                    tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
                elif etype == "response.completed":
                    resp = getattr(event, "response", None)
                    if resp is not None:
                        u = getattr(resp, "usage", None)
                        if u is not None:
                            raw = u.model_dump() if hasattr(u, "model_dump") else dict(u)
                            usage = raw
                        status = getattr(resp, "status", "")
                        if status == "completed":
                            finish_reason = "stop"
                        elif status:
                            finish_reason = status
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                maybe = close()
                if inspect.isawaitable(maybe):
                    await maybe
        return AssistantMessage(
            content="".join(parts),
            reasoning_content="".join(reasoning_parts) if saw_reasoning else None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )

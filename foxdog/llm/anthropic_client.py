"""Anthropic Messages API 客户端（与 LLMClient 同 ChatProvider 接口）。

- 消息转换：OpenAI wire 格式 → Anthropic。system 抽顶层；tool 消息 → user 角色
  tool_result 块（连续的合并进同一 user 消息）；assistant tool_calls → tool_use 块；
  连续同角色消息合并（Anthropic 要求角色交替）。
- 工具 schema：{type:function,...} → {name, description, input_schema}。
- 流式：text delta → on_delta，thinking delta → on_reasoning_delta；tool_use 由最终消息的完整 input 落成 ToolCall；
  usage 映射 prompt/completion/total。
- 重试与主备故障转移策略对齐 LLMClient（429/5xx/连接错误指数退避）。
- 属性访问宽松（getattr），测试可用 SimpleNamespace stub 替代 SDK 类型。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import anthropic

from .client import ModelEndpoint, OnDelta, is_deepseek_endpoint
from .messages import AssistantMessage, ToolCall


DEEPSEEK_DEFAULT_MAX_TOKENS = 32_768
ANTHROPIC_DEFAULT_MAX_TOKENS = 4_096


# ------------------------------------------------------------ 消息转换（纯函数，可单测）
def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _append_block(out: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    """追加 content 块；连续同角色合并（Anthropic 要求 user/assistant 交替）。"""
    if out and out[-1]["role"] == role:
        out[-1]["content"].append(block)
    else:
        out.append({"role": role, "content": [block]})


def _block_value(block: Any, key: str, default: Any = None) -> Any:
    """Read an Anthropic content block from SDK models, dicts, or test stubs."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _thinking_block_dict(block: Any) -> dict[str, Any]:
    """Materialize a signed thinking block without dropping provider fields."""
    if isinstance(block, dict):
        return dict(block)
    if hasattr(block, "model_dump"):
        return dict(block.model_dump(exclude_none=True))
    try:
        return {key: value for key, value in vars(block).items() if value is not None}
    except TypeError:
        btype = _block_value(block, "type")
        if btype == "thinking":
            return {
                key: value
                for key in ("type", "thinking", "signature")
                if (value := _block_value(block, key)) is not None
            }
        return {
            key: value
            for key in ("type", "data")
            if (value := _block_value(block, key)) is not None
        }


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """OpenAI 消息列表 → (system 文本, Anthropic messages)。"""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            reasoning = m.get("reasoning_content")
            thinking_blocks = m.get("_anthropic_thinking_blocks") or []
            if tool_calls and isinstance(thinking_blocks, list) and thinking_blocks:
                for block in thinking_blocks:
                    if isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}:
                        _append_block(out, "assistant", dict(block))
            elif reasoning and tool_calls:
                thinking: dict[str, Any] = {"type": "thinking", "thinking": str(reasoning)}
                signature = m.get("reasoning_signature")
                if signature:
                    thinking["signature"] = str(signature)
                _append_block(out, "assistant", thinking)
            if content:
                _append_block(out, "assistant", _text_block(str(content)))
            for tc in tool_calls:
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    inp = json.loads(raw)
                except json.JSONDecodeError:
                    inp = {"_raw": raw}
                if not isinstance(inp, dict):
                    inp = {"_raw": raw}
                _append_block(
                    out,
                    "assistant",
                    {"type": "tool_use", "id": tc.get("id") or "", "name": fn.get("name") or "", "input": inp},
                )
        elif role == "tool":
            # OpenAI tool 消息 → user 角色 tool_result 块；连续的自动合并（邻接合法）
            _append_block(
                out,
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": str(content or ""),
                },
            )
        elif role == "user":
            if content:
                _append_block(out, "user", _text_block(str(content)))
    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


def tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 工具 schema → Anthropic {name, description, input_schema}。"""
    out: list[dict[str, Any]] = []
    for t in tools:
        fn = t.get("function") or {}
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def from_anthropic(final: Any) -> AssistantMessage:
    """Anthropic 最终消息 → AssistantMessage（宽松属性访问，stub 友好）。"""
    parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_signature: str | None = None
    thinking_blocks: list[dict[str, Any]] = []
    calls: list[ToolCall] = []
    for block in getattr(final, "content", None) or []:
        btype = _block_value(block, "type")
        if btype == "text":
            parts.append(_block_value(block, "text", ""))
        elif btype == "thinking":
            thinking_blocks.append(_thinking_block_dict(block))
            reasoning_parts.append(_block_value(block, "thinking", "") or "")
            signature = _block_value(block, "signature")
            if signature:
                reasoning_signature = str(signature)
        elif btype == "redacted_thinking":
            thinking_blocks.append(_thinking_block_dict(block))
        elif btype == "tool_use":
            inp = _block_value(block, "input")
            calls.append(
                ToolCall(
                    id=_block_value(block, "id", "") or f"call_{len(calls)}",
                    name=_block_value(block, "name", "") or "",
                    arguments=json.dumps(inp if isinstance(inp, dict) else {}, ensure_ascii=False),
                )
            )
    usage: dict[str, Any] = {}
    u = getattr(final, "usage", None)
    if u is not None:
        inp_tok = getattr(u, "input_tokens", 0) or 0
        out_tok = getattr(u, "output_tokens", 0) or 0
        usage = {"prompt_tokens": inp_tok, "completion_tokens": out_tok, "total_tokens": inp_tok + out_tok}
    raw_stop = getattr(final, "stop_reason", None)
    finish_reason = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "refusal": "content_filter",
    }.get(raw_stop, raw_stop)
    return AssistantMessage(
        content="".join(parts),
        reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
        reasoning_signature=reasoning_signature,
        anthropic_thinking_blocks=thinking_blocks,
        tool_calls=calls,
        usage=usage,
        finish_reason=finish_reason,
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


class AnthropicClient:
    """Anthropic 协议 chat 客户端（重试 + 主备切换与 LLMClient 同策略）。"""

    def __init__(
        self,
        primary: ModelEndpoint,
        backup: ModelEndpoint | None = None,
        max_retries: int = 4,
        failover_after: int = 2,
        backoff_base: float = 1.0,
        client_factory: Callable[[ModelEndpoint], Any] | None = None,  # 测试注入 stub
    ) -> None:
        self._endpoints = [e for e in (primary, backup) if e is not None]
        self._current = 0
        self._consecutive_failures = 0
        self.max_retries = max_retries
        self.failover_after = failover_after
        self.backoff_base = backoff_base
        self._client_factory = client_factory

    def _client(self, ep: ModelEndpoint) -> Any:
        if self._client_factory is not None:
            return self._client_factory(ep)
        kwargs: dict[str, Any] = {"api_key": ep.resolved_key() or "EMPTY", "max_retries": 0}
        if ep.base_url:
            kwargs["base_url"] = ep.base_url
        return anthropic.AsyncAnthropic(**kwargs)

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
            ep = self._endpoints[self._current]
            try:
                result = await self._stream_once(
                    ep,
                    messages,
                    tools,
                    on_delta,
                    require_tool=require_tool,
                    on_reasoning_delta=on_reasoning_delta,
                )
                self._consecutive_failures = 0
                return result
            except BaseException as exc:  # noqa: BLE001 - 分类后重抛
                if isinstance(exc, asyncio.CancelledError):
                    raise
                last_exc = exc
                if not _is_retryable(exc):
                    raise
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failover_after and len(self._endpoints) > 1:
                    self._current = (self._current + 1) % len(self._endpoints)
                    self._consecutive_failures = 0
                await asyncio.sleep(self.backoff_base * (2**attempt))
        assert last_exc is not None
        raise last_exc

    async def _stream_once(
        self,
        ep: ModelEndpoint,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        *,
        require_tool: bool = False,
        on_reasoning_delta: OnDelta | None = None,
    ) -> AssistantMessage:
        system, msgs = to_anthropic(messages)
        deepseek = is_deepseek_endpoint(ep)
        kwargs: dict[str, Any] = {
            "model": ep.model,
            "max_tokens": ep.max_tokens or (
                DEEPSEEK_DEFAULT_MAX_TOKENS if deepseek else ANTHROPIC_DEFAULT_MAX_TOKENS
            ),
            "messages": msgs,
        }
        if not deepseek or ep.thinking == "disabled":
            kwargs["temperature"] = ep.temperature
        if deepseek and ep.thinking is not None:
            kwargs["thinking"] = {"type": ep.thinking}
        if deepseek and ep.thinking != "disabled" and ep.reasoning_effort is not None:
            kwargs["output_config"] = {"effort": ep.reasoning_effort}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools_to_anthropic(tools)
            if require_tool:
                kwargs["tool_choice"] = {"type": "any"}
        client = self._client(ep)
        async with client.messages.stream(**kwargs) as stream:
            if hasattr(stream, "__aiter__"):
                async for event in stream:
                    delta = event.get("delta") if isinstance(event, dict) else getattr(event, "delta", None)
                    delta_type = (
                        delta.get("type") if isinstance(delta, dict) else getattr(delta, "type", None)
                    )
                    if delta_type == "text_delta":
                        text = delta.get("text", "") if isinstance(delta, dict) else getattr(delta, "text", "")
                        if text:
                            on_delta(text)
                    elif delta_type == "thinking_delta":
                        thinking = (
                            delta.get("thinking", "")
                            if isinstance(delta, dict)
                            else getattr(delta, "thinking", "")
                        )
                        if thinking and on_reasoning_delta is not None:
                            on_reasoning_delta(thinking)
            else:
                # 兼容仅实现 text_stream 的轻量 provider / 测试桩。
                async for text in stream.text_stream:
                    on_delta(text)
            final = await stream.get_final_message()
        return from_anthropic(final)

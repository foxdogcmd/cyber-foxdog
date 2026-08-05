"""OpenAI 兼容异步流式客户端：指数退避重试 + 主备故障转移。

- 任意 OpenAI 兼容 base_url（DeepSeek / 月之暗面 / 本地 vLLM 等）。
- 429/5xx/连接错误：指数退避重试，最多 max_retries 次。
- 主端点连续失败 failover_after 次 → 切到备用端点（借鉴 LeoAI 故障转移）。
- FakeProvider：无网络冒烟/测试用脚本化 provider，接口与 LLMClient 一致。
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import os
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlparse

import openai
from pydantic import BaseModel

from .messages import AssistantMessage, ToolCall, ToolCallAccumulator

OnDelta = Callable[[str], None]

KIMI_DEFAULT_MAX_TOKENS = 32_768


class ChatProvider(Protocol):
    """core/loop 只依赖这个接口，不关心背后是真实 API 还是 fake。"""

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        *,
        require_tool: bool = False,
        on_reasoning_delta: OnDelta | None = None,
    ) -> AssistantMessage: ...


class ModelEndpoint(BaseModel):
    """一个模型端点配置。"""

    provider: str = "openai-compatible"
    protocol: str = "openai"  # openai | anthropic（自建端点两种协议都支持）
    base_url: str | None = None
    model: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.6
    max_tokens: int | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    def resolved_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return os.environ.get("FOXDOG_API_KEY", "")


def is_deepseek_endpoint(ep: ModelEndpoint) -> bool:
    """Recognize both the official endpoint and DeepSeek models behind a proxy."""
    host = (urlparse(ep.base_url or "").hostname or "").lower()
    return host == "api.deepseek.com" or ep.model.lower().startswith("deepseek-")


def is_kimi_endpoint(ep: ModelEndpoint) -> bool:
    """Recognize Kimi's official APIs and common Kimi model IDs behind proxies."""
    host = (urlparse(ep.base_url or "").hostname or "").lower()
    model = ep.model.lower()
    return host in {"api.moonshot.cn", "api.kimi.com"} or model.startswith(("kimi-", "k3"))


def kimi_model_family(ep: ModelEndpoint) -> str | None:
    """Return the modern Kimi family whose request constraints we know."""
    model = ep.model.lower()
    if model.startswith("kimi-k3") or model.startswith("k3"):
        return "k3"
    if model.startswith("kimi-k2.7"):
        return "k2.7"
    if model.startswith("kimi-k2.6"):
        return "k2.6"
    return None


def _kimi_k3_effort(effort: str | None) -> str | None:
    """Normalize the shared effort vocabulary to K3's low/high/max wire values."""
    if effort in {"low", "high", "max"}:
        return effort
    if effort == "medium":
        return "high"
    if effort == "xhigh":
        return "max"
    return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return False


class LLMClient:
    """主备双端点的流式 chat 客户端。"""

    def __init__(
        self,
        primary: ModelEndpoint,
        backup: ModelEndpoint | None = None,
        max_retries: int = 4,
        failover_after: int = 2,
        backoff_base: float = 1.0,
    ) -> None:
        self._endpoints = [e for e in (primary, backup) if e is not None]
        self._current = 0
        self._consecutive_failures = 0
        self.max_retries = max_retries
        self.failover_after = failover_after
        self.backoff_base = backoff_base

    def _client(self, ep: ModelEndpoint) -> openai.AsyncOpenAI:
        # Retry policy lives in LLMClient; disable the SDK's hidden inner retries.
        return openai.AsyncOpenAI(
            base_url=ep.base_url,
            api_key=ep.resolved_key() or "EMPTY",
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
        deepseek = is_deepseek_endpoint(ep)
        kimi = is_kimi_endpoint(ep)
        kimi_family = kimi_model_family(ep) if kimi else None
        stripped = {"_anthropic_thinking_blocks", "reasoning_signature"}
        if not deepseek and not kimi:
            stripped.add("reasoning_content")
        wire_messages = [
            {key: value for key, value in message.items() if key not in stripped}
            for message in messages
        ]
        kwargs: dict[str, Any] = {
            "model": ep.model,
            "messages": wire_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # DeepSeek thinking and modern Kimi models reject or ignore custom
        # sampling values. Kimi's legacy moonshot-v1 models retain temperature.
        if deepseek and ep.thinking == "disabled":
            kwargs["temperature"] = ep.temperature
        elif not deepseek and kimi_family is None:
            kwargs["temperature"] = ep.temperature
        if deepseek and ep.thinking != "disabled" and ep.reasoning_effort is not None:
            kwargs["reasoning_effort"] = ep.reasoning_effort
        if deepseek and ep.thinking is not None:
            kwargs["extra_body"] = {"thinking": {"type": ep.thinking}}
        if kimi_family == "k3":
            effort = _kimi_k3_effort(ep.reasoning_effort)
            if effort is not None:
                kwargs["reasoning_effort"] = effort
        elif kimi_family == "k2.6":
            if ep.thinking == "disabled":
                thinking = {"type": "disabled"}
            else:
                thinking = {"type": "enabled", "keep": "all"}
            kwargs["extra_body"] = {"thinking": thinking}
        # K2.7 Code always enables Preserved Thinking and rejects custom
        # thinking modes, so intentionally omit the field for that family.
        if tools:
            kwargs["tools"] = tools
            if require_tool and kimi_family not in {"k2.6", "k2.7"}:
                kwargs["tool_choice"] = "required"
        if ep.max_tokens:
            kwargs["max_tokens"] = ep.max_tokens
        elif kimi_family is not None:
            kwargs["max_tokens"] = KIMI_DEFAULT_MAX_TOKENS
        stream = await self._client(ep).chat.completions.create(**kwargs)

        acc = ToolCallAccumulator()
        parts: list[str] = []
        reasoning_parts: list[str] = []
        saw_reasoning = False
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        try:
            async for chunk in stream:
                if chunk.choices:
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    if delta and delta.content:
                        parts.append(delta.content)
                        on_delta(delta.content)
                    reasoning = getattr(delta, "reasoning_content", None) if delta else None
                    if reasoning is not None:
                        saw_reasoning = True
                        reasoning_parts.append(reasoning)
                        if reasoning and on_reasoning_delta is not None:
                            on_reasoning_delta(reasoning)
                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            acc.feed(tc)
                if getattr(chunk, "usage", None):
                    raw_usage = chunk.usage
                    usage = raw_usage.model_dump() if hasattr(raw_usage, "model_dump") else dict(raw_usage)
        finally:
            # Reading to EOF closes OpenAI's AsyncStream automatically. Cancellation
            # can stop before EOF, so close explicitly to abort the HTTP response now.
            close = getattr(stream, "close", None)
            if callable(close):
                maybe_awaitable = close()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
        return AssistantMessage(
            content="".join(parts),
            reasoning_content="".join(reasoning_parts) if saw_reasoning else None,
            preserve_reasoning_content=kimi and saw_reasoning,
            tool_calls=acc.finish(),
            usage=usage,
            finish_reason=finish_reason,
        )


# ---------------------------------------------------------------------------
# FakeProvider：脚本化 provider，无网络冒烟与单测用。
# 脚本元素：("text", 内容) 或 ("tool", 工具名, 参数dict)
# 默认脚本：读预置目标侧 flag 文件 → submit_flag → 结束（配合 FlagGate 全链路冒烟）。
# routes：多角色脚本——[(marker, script)]，按"全部 user 角色消息拼接文本"里首个
# 命中的 marker 选该角色脚本（swarm：planner/worker/主 agent 各走各的剧本）。


class FakeProvider:
    def __init__(
        self,
        script: list[tuple[str, Any]] | None = None,
        repeat: bool = False,
        routes: list[tuple[str, list[tuple[str, Any]]]] | None = None,
    ) -> None:
        if script is None and not routes:
            flag = os.environ.get("FOXDOG_FAKE_FLAG", "flag{smoke_test_flag_123}")
            # 读预置的目标侧 flag 文件（cli 在 provider=fake 时写入工作区），
            # 不能用 echo——命令里出现 flag 会被 FlagGate 污染扫描拒收
            # Keep the smoke script portable across a Windows host and a Linux
            # Docker execution runtime. Both supported environments expose
            # `python`; shell-specific Get-Content/cat would select the host OS.
            read_cmd = (
                'python -c "from pathlib import Path; '
                "print(Path('fake_target_flag.txt').read_text(encoding='utf-8'), end='')\""
            )
            script = [
                ("tool", "shell_exec", {"command": read_cmd}),
                ("tool", "submit_flag", {"flag": flag}),
                ("text", "flag 已验证，解题完成。"),
            ]
        self._script = list(script or [])
        self._original = list(script or [])
        self._repeat = repeat  # True 时脚本耗尽后从头再来（预算/压缩测试用）
        self._routes = [(m, list(s)) for m, s in (routes or [])]
        self._counter = itertools.count()
        self.calls: int = 0

    def _pick_step(self, messages: list[dict[str, Any]]) -> tuple[str, Any]:
        if self._routes:
            # 用 user 消息做路由：worker 任务书/planner 指令都在 user 角色里；
            # 主 agent 历史里的 spawn 参数是 assistant/tool 角色，不会误命中
            user_text = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
            for marker, script in self._routes:
                if marker in user_text:
                    return script.pop(0) if script else ("text", "（fake route 脚本耗尽）")
            # 无命中 → 走默认脚本（主 agent 角色）
        if not self._script and self._repeat:
            self._script = list(self._original)
        return self._script.pop(0) if self._script else ("text", "（fake 脚本耗尽，结束）")

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        *,
        require_tool: bool = False,
        on_reasoning_delta: OnDelta | None = None,
    ) -> AssistantMessage:
        self.calls += 1
        step = self._pick_step(messages)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if step[0] == "tool":
            n = next(self._counter)
            tc = ToolCall(id=f"fake_call_{n}", name=step[1], arguments=json.dumps(step[2], ensure_ascii=False))
            return AssistantMessage(content="", tool_calls=[tc], usage=usage)
        text = str(step[1])
        on_delta(text)
        return AssistantMessage(content=text, tool_calls=[], usage=usage)

"""DeepSeek-specific OpenAI-compatible protocol behavior."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from foxdog.config import ConfigStore, load_config
from foxdog.core.loop import _merge_usage
from foxdog.core.orchestrator import _event_arguments
from foxdog.interactive import SETTING_SYSTEM
from foxdog.llm import client as client_module
from foxdog.llm.client import LLMClient, ModelEndpoint, is_deepseek_endpoint
from foxdog.llm.messages import AssistantMessage, assistant_to_openai
from foxdog.tools.builtin import config_edit
from foxdog.tools.builtin.config_edit import ListDeepSeekModelsTool, SecretVault, SetModelTool


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._iter = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Completions:
    def __init__(self, chunks, recorded):
        self.chunks = chunks
        self.recorded = recorded

    async def create(self, **kwargs):
        self.recorded.update(kwargs)
        return _Stream(self.chunks)


async def test_openai_stream_is_closed_when_request_is_cancelled(monkeypatch):
    class BlockingStream:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed.set()

    stream = BlockingStream()

    class Completions:
        async def create(self, **_kwargs):
            return stream

    ep = ModelEndpoint(base_url="https://api.deepseek.com", model="deepseek-v4-flash")
    client = LLMClient(ep)
    stub = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(client, "_client", lambda _ep: stub)

    task = asyncio.create_task(client._stream_once(ep, [], [], lambda _text: None))
    await asyncio.wait_for(stream.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed.is_set()


def _chunk(*, content=None, reasoning=None, tool_calls=None, usage=None, finish_reason=None):
    choices = []
    if content is not None or reasoning is not None or tool_calls is not None or finish_reason is not None:
        choices = [SimpleNamespace(
            delta=SimpleNamespace(
                content=content,
                reasoning_content=reasoning,
                tool_calls=tool_calls,
            ),
            finish_reason=finish_reason,
        )]
    return SimpleNamespace(choices=choices, usage=usage)


async def test_deepseek_stream_preserves_reasoning_for_tool_followup(monkeypatch):
    recorded = {}
    usage = SimpleNamespace(model_dump=lambda: {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 24},
    })
    tool_delta = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="shell_exec", arguments='{"command":"id"}'),
    )
    chunks = [
        _chunk(reasoning="先分析"),
        _chunk(reasoning="，再执行"),
        _chunk(tool_calls=[tool_delta]),
        _chunk(finish_reason="tool_calls"),
        _chunk(usage=usage),
    ]
    ep = ModelEndpoint(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        reasoning_effort="high",
        thinking="enabled",
    )
    client = LLMClient(ep)
    stub = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(chunks, recorded)))
    monkeypatch.setattr(client, "_client", lambda _ep: stub)

    streamed_reasoning: list[str] = []
    msg = await client._stream_once(
        ep,
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "上一轮推理",
                "_anthropic_thinking_blocks": [
                    {"type": "thinking", "thinking": "不得进入 OpenAI wire", "signature": "sig"}
                ],
            },
            {"role": "user", "content": "检查目标"},
        ],
        [{"type": "function", "function": {"name": "shell_exec", "parameters": {"type": "object"}}}],
        lambda _text: None,
        require_tool=True,
        on_reasoning_delta=streamed_reasoning.append,
    )

    assert msg.reasoning_content == "先分析，再执行"
    assert streamed_reasoning == ["先分析", "，再执行"]
    assert msg.tool_calls[0].name == "shell_exec"
    assert recorded["reasoning_effort"] == "high"
    assert recorded["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "tool_choice" not in recorded
    assert "temperature" not in recorded
    assert "_anthropic_thinking_blocks" not in recorded["messages"][0]
    assert recorded["messages"][0]["reasoning_content"] == "上一轮推理"
    assert msg.finish_reason == "tool_calls"
    followup = assistant_to_openai(msg)
    assert followup["reasoning_content"] == "先分析，再执行"
    assert followup["tool_calls"][0]["id"] == "call_1"


async def test_deepseek_disabled_thinking_can_require_tool(monkeypatch):
    recorded = {}
    chunks = [_chunk(content="ok")]
    ep = ModelEndpoint(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking="disabled",
    )
    client = LLMClient(ep)
    stub = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(chunks, recorded)))
    monkeypatch.setattr(client, "_client", lambda _ep: stub)

    await client._stream_once(
        ep,
        [{"role": "user", "content": "检查目标"}],
        [{"type": "function", "function": {"name": "shell_exec", "parameters": {"type": "object"}}}],
        lambda _text: None,
        require_tool=True,
    )

    assert recorded["tool_choice"] == "required"


async def test_deepseek_default_thinking_omits_required_tool_choice(monkeypatch):
    recorded = {}
    chunks = [_chunk(content="ok")]
    ep = ModelEndpoint(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
    client = LLMClient(ep)
    stub = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(chunks, recorded)))
    monkeypatch.setattr(client, "_client", lambda _ep: stub)

    await client._stream_once(
        ep,
        [{"role": "user", "content": "检查目标"}],
        [{"type": "function", "function": {"name": "shell_exec", "parameters": {"type": "object"}}}],
        lambda _text: None,
        require_tool=True,
    )

    assert "tool_choice" not in recorded


async def test_non_deepseek_keeps_temperature_and_strips_reasoning_extension(monkeypatch):
    recorded = {}
    chunks = [_chunk(content="ok")]
    ep = ModelEndpoint(base_url="https://api.openai.com/v1", model="gpt-test", temperature=0.2)
    client = LLMClient(ep)
    stub = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(chunks, recorded)))
    monkeypatch.setattr(client, "_client", lambda _ep: stub)

    await client._stream_once(
        ep,
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "provider extension",
                "_anthropic_thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}],
            },
            {"role": "user", "content": "continue"},
        ],
        [],
        lambda _text: None,
    )

    assert recorded["temperature"] == 0.2
    assert "reasoning_content" not in recorded["messages"][0]
    assert "_anthropic_thinking_blocks" not in recorded["messages"][0]


def test_deepseek_detection_and_extended_usage_merge():
    assert is_deepseek_endpoint(ModelEndpoint(base_url="https://api.deepseek.com", model="custom"))
    assert is_deepseek_endpoint(ModelEndpoint(base_url="https://proxy.invalid/v1", model="deepseek-v4-pro"))
    assert not is_deepseek_endpoint(ModelEndpoint(base_url="https://api.openai.com/v1", model="gpt-test"))

    total = {}
    _merge_usage(total, {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 24},
    })
    assert total == {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
        "reasoning_tokens": 24,
    }


def test_assistant_reasoning_is_optional():
    assert "reasoning_content" not in assistant_to_openai(AssistantMessage(content="done"))


def test_openai_sdk_inner_retries_are_disabled(monkeypatch):
    recorded = {}
    sentinel = object()

    def factory(**kwargs):
        recorded.update(kwargs)
        return sentinel

    monkeypatch.setattr(client_module.openai, "AsyncOpenAI", factory)
    ep = ModelEndpoint(base_url="https://api.deepseek.com", model="deepseek-v4-flash", api_key="secret")
    assert LLMClient(ep)._client(ep) is sentinel
    assert recorded["max_retries"] == 0


async def test_setting_ai_can_persist_complete_deepseek_config(tmp_path, monkeypatch):
    assert "https://api.deepseek.com" in SETTING_SYSTEM
    assert "list_deepseek_models" in SETTING_SYSTEM
    assert "xhigh/max" in SETTING_SYSTEM
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    result = await SetModelTool(store).run(
        {
            "role": "solver",
            "protocol": "openai",
            "base_url": "https://api.deepseek.com",
            "api_key": "test-only",
            "model": "deepseek-v4-flash",
            "thinking": "enabled",
            "reasoning_effort": "max",
        },
        None,
    )
    assert result.ok
    monkeypatch.setattr("foxdog.config.USER_CONFIG", path)
    monkeypatch.setattr("foxdog.config.PROJECT_CONFIG", tmp_path / "missing.toml")
    loaded = load_config()
    assert loaded.solver.thinking == "enabled"
    assert loaded.solver.reasoning_effort == "max"


async def test_setting_ai_fetches_official_deepseek_model_list(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    store = ConfigStore(path)
    store.set_model(
        "solver",
        protocol="openai",
        base_url="https://api.deepseek.com",
        api_key="test-only",
        model="deepseek-v4-flash",
    )
    recorded = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"object": "list", "data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}

    class Client:
        def __init__(self, **kwargs):
            recorded["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            recorded["url"] = url
            recorded["request"] = kwargs
            return Response()

    monkeypatch.setattr(config_edit.httpx, "AsyncClient", Client)
    result = await ListDeepSeekModelsTool(store).run({}, None)
    assert result.ok
    assert recorded["url"] == "https://api.deepseek.com/models"
    assert recorded["request"]["headers"] == {"Authorization": "Bearer test-only"}
    assert "deepseek-v4-flash" in result.output and "deepseek-v4-pro" in result.output


def test_setting_model_key_is_redacted_from_tui_events():
    raw = '{"role":"solver","api_key":"secret-value","model":"deepseek-v4-flash"}'
    redacted = _event_arguments("set_model", raw)
    assert "secret-value" not in redacted
    assert json.loads(redacted)["api_key"] == "***"
    assert _event_arguments("shell_exec", raw) == raw
def test_secret_vault_uses_opaque_handles_and_resolves_only_on_host():
    vault = SecretVault()
    handle = vault.put("sk-super-secret-value")
    assert handle.startswith("secret://setting/")
    assert "super-secret" not in handle
    assert vault.resolve(handle) == "sk-super-secret-value"
    assert vault.resolve("plain") == "plain"

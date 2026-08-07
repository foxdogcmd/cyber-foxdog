"""Anthropic 客户端：消息/工具 schema 转换（正反向）、流式 stub、重试策略。"""
from __future__ import annotations

from types import SimpleNamespace

from foxdog.llm.anthropic_client import (
    DEEPSEEK_DEFAULT_MAX_TOKENS,
    AnthropicClient,
    from_anthropic,
    to_anthropic,
    tools_to_anthropic,
)
from foxdog.llm.client import ModelEndpoint
from foxdog.llm.messages import assistant_to_openai, system_message, tool_result_message, user_message, AssistantMessage, ToolCall


def test_to_anthropic_system_extraction():
    system, msgs = to_anthropic([system_message("sys提示"), user_message("题目")])
    assert system == "sys提示"
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "题目"}]}]


def test_to_anthropic_tool_flow_grouping():
    """assistant tool_calls → tool_use 块；连续 tool 消息合并进同一 user 消息（邻接合法）。"""
    history = [
        system_message("s"),
        user_message("u"),
        assistant_to_openai(AssistantMessage(content="", tool_calls=[
            ToolCall(id="c1", name="shell_exec", arguments='{"command":"id"}'),
            ToolCall(id="c2", name="shell_exec", arguments='{"command":"pwd"}'),
        ])),
        tool_result_message("c1", "out1"),
        tool_result_message("c2", "out2"),
        user_message("继续"),
        user_message("steer补充"),
    ]
    system, msgs = to_anthropic(history)
    # user / assistant(tool_use×2) / user(tool_result×2 + text×2，连续同角色合并)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    a = msgs[1]["content"]
    assert [b["type"] for b in a] == ["tool_use", "tool_use"]
    assert a[0]["id"] == "c1" and a[0]["input"] == {"command": "id"}
    u = msgs[2]["content"]
    assert [b["type"] for b in u] == ["tool_result", "tool_result", "text", "text"]
    assert u[0]["tool_use_id"] == "c1" and u[1]["content"] == "out2"
    # 连续 user 文本合并进同一消息
    assert [b.get("text") for b in u[2:]] == ["继续", "steer补充"]


def test_tools_to_anthropic():
    tools = [{"type": "function", "function": {"name": "n", "description": "d", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}}]
    out = tools_to_anthropic(tools)
    assert out == [{"name": "n", "description": "d", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}]


def test_from_anthropic():
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="分析结果"),
            SimpleNamespace(type="tool_use", id="t1", name="shell_exec", input={"command": "ls"}),
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=7),
        stop_reason="tool_use",
    )
    msg = from_anthropic(final)
    assert msg.content == "分析结果"
    assert msg.tool_calls[0].name == "shell_exec"
    assert '"command": "ls"' in msg.tool_calls[0].arguments
    assert msg.usage == {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
    assert msg.finish_reason == "tool_calls"


def test_from_anthropic_preserves_thinking_for_tool_roundtrip():
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="先分析再执行", signature="sig-1"),
            SimpleNamespace(type="tool_use", id="t1", name="shell_exec", input={"command": "id"}),
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        stop_reason="tool_use",
    )
    msg = from_anthropic(final)
    assert msg.reasoning_content == "先分析再执行"
    assert msg.reasoning_signature == "sig-1"
    assert msg.finish_reason == "tool_calls"

    wire = assistant_to_openai(msg)
    _system, converted = to_anthropic([user_message("go"), wire, tool_result_message("t1", "ok")])
    blocks = converted[1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "先分析再执行", "signature": "sig-1"}
    assert blocks[1]["type"] == "tool_use"


def test_from_anthropic_preserves_all_signed_and_redacted_blocks_in_order():
    final = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "第一段", "signature": "sig-1"},
            SimpleNamespace(type="redacted_thinking", data="opaque-data"),
            SimpleNamespace(type="thinking", thinking="第二段", signature="sig-2"),
            SimpleNamespace(type="text", text="准备执行"),
            SimpleNamespace(type="tool_use", id="t1", name="shell_exec", input={"command": "id"}),
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        stop_reason="tool_use",
    )
    msg = from_anthropic(final)
    expected = [
        {"type": "thinking", "thinking": "第一段", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "opaque-data"},
        {"type": "thinking", "thinking": "第二段", "signature": "sig-2"},
    ]
    assert msg.reasoning_content == "第一段第二段"
    assert msg.reasoning_signature == "sig-2"
    assert msg.anthropic_thinking_blocks == expected

    history_message = assistant_to_openai(msg)
    assert history_message["_anthropic_thinking_blocks"] == expected
    _system, converted = to_anthropic(
        [user_message("go"), history_message, tool_result_message("t1", "ok")]
    )
    blocks = converted[1]["content"]
    assert blocks[:3] == expected
    assert [block["type"] for block in blocks] == [
        "thinking",
        "redacted_thinking",
        "thinking",
        "text",
        "tool_use",
    ]


def test_thinking_blocks_without_tool_call_are_not_saved_to_history():
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="分析完成", signature="sig-final"),
            SimpleNamespace(type="redacted_thinking", data="opaque-final"),
            SimpleNamespace(type="text", text="最终答案"),
        ],
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
        stop_reason="end_turn",
    )
    msg = from_anthropic(final)
    assert len(msg.anthropic_thinking_blocks) == 2
    history_message = assistant_to_openai(msg)
    assert "_anthropic_thinking_blocks" not in history_message
    assert "reasoning_content" not in history_message


class _FakeStream:
    def __init__(self, final, deltas):
        self._final = final
        self._deltas = deltas

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, final, deltas, recorded):
        self._final = final
        self._deltas = deltas
        self._recorded = recorded

    def stream(self, **kwargs):
        self._recorded.update(kwargs)
        return _FakeStream(self._final, self._deltas)


class _FakeEventStream(_FakeStream):
    def __init__(self, final, events):
        super().__init__(final, [])
        self._events = events

    def __aiter__(self):
        self._event_iter = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._event_iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeEventMessages:
    def __init__(self, final, events):
        self._final = final
        self._events = events

    def stream(self, **_kwargs):
        return _FakeEventStream(self._final, self._events)


async def test_stream_chat_with_stub():
    recorded: dict = {}
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="完成")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )
    client = AnthropicClient(
        ModelEndpoint(protocol="anthropic", model="claude-test", api_key="k"),
        client_factory=lambda ep: SimpleNamespace(messages=_FakeMessages(final, ["完", "成"], recorded)),
    )
    deltas: list[str] = []
    msg = await client.stream_chat(
        [system_message("sys"), user_message("hi")],
        [{"type": "function", "function": {"name": "t", "description": "", "parameters": {"type": "object"}}}],
        deltas.append,
    )
    assert deltas == ["完", "成"]
    assert msg.content == "完成" and msg.usage["total_tokens"] == 5
    # system 抽顶层、工具 schema 已转换
    assert recorded["system"] == "sys"
    assert recorded["tools"][0]["name"] == "t" and "input_schema" in recorded["tools"][0]
    assert recorded["max_tokens"] == 4096  # Anthropic 必填缺省


async def test_stream_chat_emits_anthropic_thinking_deltas():
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="先分析再执行", signature="sig"),
            SimpleNamespace(type="text", text="完成"),
        ],
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
        stop_reason="end_turn",
    )
    events = [
        SimpleNamespace(delta=SimpleNamespace(type="thinking_delta", thinking="先分析")),
        SimpleNamespace(delta=SimpleNamespace(type="thinking_delta", thinking="再执行")),
        SimpleNamespace(delta=SimpleNamespace(type="text_delta", text="完成")),
    ]
    client = AnthropicClient(
        ModelEndpoint(protocol="anthropic", model="deepseek-test", api_key="k"),
        client_factory=lambda _ep: SimpleNamespace(messages=_FakeEventMessages(final, events)),
    )
    reasoning: list[str] = []
    text: list[str] = []
    msg = await client.stream_chat(
        [user_message("hi")],
        [],
        text.append,
        on_reasoning_delta=reasoning.append,
    )

    assert reasoning == ["先分析", "再执行"]
    assert text == ["完成"]
    assert msg.reasoning_content == "先分析再执行"


async def test_deepseek_anthropic_thinking_omits_required_tool_choice():
    recorded: dict = {}
    final = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="shell_exec", input={"command": "id"})],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        stop_reason="tool_use",
    )
    endpoint = ModelEndpoint(
        protocol="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        thinking="enabled",
        reasoning_effort="xhigh",
        api_key="k",
    )
    client = AnthropicClient(
        endpoint,
        client_factory=lambda ep: SimpleNamespace(messages=_FakeMessages(final, [], recorded)),
    )
    await client.stream_chat(
        [user_message("继续解题")],
        [{"type": "function", "function": {"name": "shell_exec", "description": "", "parameters": {"type": "object"}}}],
        lambda _text: None,
        require_tool=True,
    )
    assert recorded["max_tokens"] == DEEPSEEK_DEFAULT_MAX_TOKENS
    assert recorded["thinking"] == {"type": "enabled"}
    assert recorded["output_config"] == {"effort": "xhigh"}
    assert "tool_choice" not in recorded
    assert "temperature" not in recorded


async def test_deepseek_anthropic_disabled_thinking_omits_output_config():
    recorded: dict = {}
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="完成")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        stop_reason="end_turn",
    )
    endpoint = ModelEndpoint(
        protocol="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        thinking="disabled",
        reasoning_effort="max",
        temperature=0.3,
        api_key="k",
    )
    client = AnthropicClient(
        endpoint,
        client_factory=lambda ep: SimpleNamespace(messages=_FakeMessages(final, [], recorded)),
    )
    await client.stream_chat([user_message("直接回答")], [], lambda _text: None)

    assert recorded["thinking"] == {"type": "disabled"}
    assert recorded["temperature"] == 0.3
    assert "output_config" not in recorded


async def test_deepseek_anthropic_disabled_thinking_can_require_tool():
    recorded: dict = {}
    final = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="shell_exec", input={"command": "id"})],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        stop_reason="tool_use",
    )
    endpoint = ModelEndpoint(
        protocol="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        thinking="disabled",
        api_key="k",
    )
    client = AnthropicClient(
        endpoint,
        client_factory=lambda ep: SimpleNamespace(messages=_FakeMessages(final, [], recorded)),
    )
    await client.stream_chat(
        [user_message("继续解题")],
        [{"type": "function", "function": {"name": "shell_exec", "description": "", "parameters": {"type": "object"}}}],
        lambda _text: None,
        require_tool=True,
    )

    assert recorded["tool_choice"] == {"type": "any"}


async def test_deepseek_anthropic_default_thinking_omits_required_tool_choice():
    recorded: dict = {}
    final = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="shell_exec", input={"command": "id"})],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        stop_reason="tool_use",
    )
    endpoint = ModelEndpoint(
        protocol="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        api_key="k",
    )
    client = AnthropicClient(
        endpoint,
        client_factory=lambda ep: SimpleNamespace(messages=_FakeMessages(final, [], recorded)),
    )
    await client.stream_chat(
        [user_message("继续解题")],
        [{"type": "function", "function": {"name": "shell_exec", "description": "", "parameters": {"type": "object"}}}],
        lambda _text: None,
        require_tool=True,
    )

    assert "tool_choice" not in recorded

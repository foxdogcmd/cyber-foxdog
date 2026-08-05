"""OpenAI Responses API 客户端：消息转换（message/function_call/function_call_output）与流式事件。"""
from __future__ import annotations

from types import SimpleNamespace

from foxdog.llm.client import ModelEndpoint
from foxdog.llm.messages import assistant_to_openai, system_message, tool_result_message, user_message, AssistantMessage, ToolCall
from foxdog.llm.responses_client import ResponsesClient, _convert_tools, _to_responses_input


def test_to_responses_input_conversion():
    history = [
        system_message("sys提示"),
        user_message("题目"),
        assistant_to_openai(AssistantMessage(
            content="看一下",
            tool_calls=[ToolCall(id="c1", name="shell_exec", arguments='{"command":"id"}')],
        )),
        tool_result_message("c1", "uid=0"),
        user_message("继续"),
    ]
    instructions, items = _to_responses_input(history)
    assert instructions == "sys提示"
    types = [i["type"] for i in items]
    assert types == ["message", "message", "function_call", "function_call_output", "message"]
    fc = items[2]
    assert fc["call_id"] == "c1" and fc["name"] == "shell_exec" and fc["arguments"] == '{"command":"id"}'
    out = items[3]
    assert out["call_id"] == "c1" and out["output"] == "uid=0"
    assert items[4]["role"] == "user"


def test_convert_tools():
    tools = [{"type": "function", "function": {"name": "n", "description": "d", "parameters": {"type": "object"}}}]
    assert _convert_tools(tools) == [{"type": "function", "name": "n", "description": "d", "parameters": {"type": "object"}}]


class _FakeStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()


class _FakeResponses:
    def __init__(self, events, recorded):
        self._events = events
        self._recorded = recorded

    async def create(self, **kwargs):
        self._recorded.update(kwargs)
        return _FakeStream(self._events)


async def test_stream_chat_deltas_toolcall_usage():
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="分析"),
        SimpleNamespace(type="response.output_text.delta", delta="完成"),
        SimpleNamespace(
            type="response.function_call_arguments.done",
            item_id="fc_1", name="shell_exec", arguments='{"command":"ls"}',
        ),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            ),
        ),
    ]
    recorded: dict = {}
    client = ResponsesClient(ModelEndpoint(protocol="responses", model="o-test", api_key="k", base_url="https://x/v1"))
    client._client = lambda: SimpleNamespace(responses=_FakeResponses(events, recorded))  # type: ignore[method-assign]

    deltas: list[str] = []
    msg = await client.stream_chat(
        [system_message("s"), user_message("hi")],
        [{"type": "function", "function": {"name": "shell_exec", "description": "", "parameters": {"type": "object"}}}],
        deltas.append,
    )
    assert deltas == ["分析", "完成"]
    assert msg.content == "分析完成"
    assert msg.tool_calls[0].id == "fc_1" and msg.tool_calls[0].name == "shell_exec"
    assert msg.usage["total_tokens"] == 15
    assert msg.finish_reason == "stop"
    # instructions 提取 + tools 转换进请求
    assert recorded["instructions"] == "s"
    assert recorded["tools"][0]["name"] == "shell_exec"
    assert recorded["stream"] is True

"""协议层：Op / Event 类型集中定义（借鉴 codex protocol crate）。

core 只发 Event，不知道任何前端存在；TUI/headless/--json 都只是消费者。
审批也是协议事件：core 发 ApprovalRequest 挂起，前端回 ApprovalResponse。
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- Op
# 用户/系统输入


class UserInput(BaseModel):
    kind: Literal["user_input"] = "user_input"
    text: str


class Interrupt(BaseModel):
    kind: Literal["interrupt"] = "interrupt"


class ApprovalResponse(BaseModel):
    kind: Literal["approval_response"] = "approval_response"
    request_id: str
    approved: bool
    remember: bool = False  # 记住本次审批（缓存，不再重复询问）


Op = Union[UserInput, Interrupt, ApprovalResponse]

# ---------------------------------------------------------------- Event
# agent 输出


class TurnStarted(BaseModel):
    kind: Literal["turn_started"] = "turn_started"
    turn: int
    worker: str | None = None  # swarm/子 agent 场景的事件来源（None=主 agent）


class ModelRequestStarted(BaseModel):
    kind: Literal["model_request_started"] = "model_request_started"
    request_id: str
    turn: int
    step: int
    worker: str | None = None


class AgentReasoningDelta(BaseModel):
    kind: Literal["agent_reasoning_delta"] = "agent_reasoning_delta"
    request_id: str
    delta: str
    worker: str | None = None


class ModelRequestComplete(BaseModel):
    kind: Literal["model_request_complete"] = "model_request_complete"
    request_id: str
    duration_ms: int
    ok: bool = True
    finish_reason: str | None = None
    reasoning_chars: int = 0
    content_chars: int = 0
    completion_tokens: int | None = None
    error: str | None = None
    worker: str | None = None


class AgentMessageDelta(BaseModel):
    kind: Literal["agent_message_delta"] = "agent_message_delta"
    delta: str
    request_id: str | None = None
    worker: str | None = None


class ToolCallBegin(BaseModel):
    kind: Literal["tool_call_begin"] = "tool_call_begin"
    call_id: str
    name: str
    arguments: str = ""
    worker: str | None = None


class ToolCallEnd(BaseModel):
    kind: Literal["tool_call_end"] = "tool_call_end"
    call_id: str
    name: str
    ok: bool
    output_preview: str = ""
    error: str | None = None
    duration_ms: int = 0
    handle: str | None = None  # CAS 归档句柄（TUI 折叠行详情用）
    worker: str | None = None


class BackgroundTaskComplete(BaseModel):
    kind: Literal["background_task_complete"] = "background_task_complete"
    task_id: str
    command: str
    ok: bool
    exit_code: int | None = None
    output_preview: str = ""
    handle: str | None = None
    worker: str | None = None


class FlagVerified(BaseModel):
    kind: Literal["flag_verified"] = "flag_verified"
    flag: str
    ok: bool
    reason: str = ""
    worker: str | None = None


class ApprovalRequest(BaseModel):
    kind: Literal["approval_request"] = "approval_request"
    request_id: str
    tool: str
    arguments: str = ""
    reason: str = ""


class TurnComplete(BaseModel):
    kind: Literal["turn_complete"] = "turn_complete"
    turn: int
    stop_reason: str  # completed | truncated | solved | max_steps | interrupted | error
    usage: dict = Field(default_factory=dict)
    worker: str | None = None


class Error(BaseModel):
    kind: Literal["error"] = "error"
    message: str
    worker: str | None = None


class SwarmNote(BaseModel):
    """swarm 协调循环的系统通告（派 intent / 软暂停 / worker 起止）。"""

    kind: Literal["swarm_note"] = "swarm_note"
    message: str
    worker: str | None = None


Event = Union[
    TurnStarted,
    ModelRequestStarted,
    AgentReasoningDelta,
    ModelRequestComplete,
    AgentMessageDelta,
    ToolCallBegin,
    ToolCallEnd,
    BackgroundTaskComplete,
    FlagVerified,
    ApprovalRequest,
    TurnComplete,
    Error,
    SwarmNote,
]

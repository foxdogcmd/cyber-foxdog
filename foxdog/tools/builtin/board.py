"""黑板工具：list_board / claim_intent / complete_intent（主 agent 与 worker 都可用）。

worker 由 coordinator 分配 intent；claim_intent 工具主要给 agent 主动认领的场景。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult


class ListBoardTool(Tool):
    name = "list_board"
    description = "查看黑板摘要：控制指令、已知事实、已排除方向、全部 intent 状态。"
    accesses = [ToolAccess(resource="state", mode="r")]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(output=ctx.facts.summary())


class ClaimIntentArgs(BaseModel):
    worker_id: str = Field(default="main", description="认领者标识")


class ClaimIntentTool(Tool):
    name = "claim_intent"
    description = "原子认领黑板上最老的 open intent（WON 返回任务，LOST 说明没货或被抢）。"
    accesses = [ToolAccess(resource="state", mode="w")]
    args_model = ClaimIntentArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        intent = ctx.facts.claim_intent(a.worker_id)
        if intent is None:
            return ToolResult(ok=False, error="LOST：没有可认领的 open intent")
        return ToolResult(
            output=f"WON {intent['id']}：{intent['goal']}",
            data={"intent": intent},
        )


class CompleteIntentArgs(BaseModel):
    intent_id: str = Field(description="要收尾的 intent id")
    status: Literal["done", "barren"] = Field(description="done=完成有产出 / barren=试尽了没货")
    result: str = Field(description="结论（产出的证据，或判贫瘠的原因）")
    actor: str = Field(default="main", description="操作者标识")


class CompleteIntentTool(Tool):
    name = "complete_intent"
    description = "收尾你认领的 intent：done=完成（result 写结论），barren=死方向（result 写原因）。"
    accesses = [ToolAccess(resource="state", mode="w")]
    args_model = CompleteIntentArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        ok = ctx.facts.complete_intent(a.intent_id, a.status, a.result, actor=a.actor)
        if not ok:
            return ToolResult(ok=False, error=f"intent {a.intent_id} 不在 open/claimed 状态（可能已被收尾）")
        return ToolResult(output=f"intent {a.intent_id} 已标记 {a.status}")

"""submit_flag：候选 flag 过 FlagGate 三重校验（反幻觉核心，见 gate/flag_gate.py）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...protocol.types import FlagVerified
from .. import Tool, ToolAccess, ToolContext, ToolResult


class SubmitFlagArgs(BaseModel):
    flag: str = Field(description="候选 flag（必须逐字出现在真实工具输出里）")
    evidence: str | None = Field(
        default=None, description="溯源证据：CAS 句柄（fetch_output 返回的 handle）"
    )


class SubmitFlagTool(Tool):
    name = "submit_flag"
    description = (
        "提交候选 flag。必须通过 FlagGate 三重校验（格式/非占位符/溯源）才算数，"
        "flag 必须逐字出现在真实工具输出归档里，只在回复里说不算。"
    )
    accesses = [ToolAccess(resource="gate", mode="w")]
    args_model = SubmitFlagArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if ctx.flag_gate is None:
            return ToolResult(ok=False, error="FlagGate 未配置")
        result = ctx.flag_gate.submit(a.flag, evidence=a.evidence)
        ctx.emit(FlagVerified(flag=result.flag, ok=result.ok, reason=result.reason))
        return ToolResult(ok=result.ok, output=result.reason, error=None if result.ok else result.reason)

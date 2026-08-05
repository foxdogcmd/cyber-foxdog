"""post_fact / post_deadend：轻量事实板工具（写死路和写事实一样有价值）。

追加写 run 目录 facts.jsonl；每 turn 开始摘要（各最近 15 条）注入系统提示
"已知事实/已排除方向"区块（见 swarm/facts.py 与 core/prompts.py）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult


class PostFactArgs(BaseModel):
    fact: str = Field(description="已验证的事实（含端口/服务/凭据/路径等关键信息）")
    evidence_handle: str | None = Field(default=None, description="证据 CAS 句柄（工具输出归档 handle）")


class PostFactTool(Tool):
    name = "post_fact"
    description = "记录一条已验证事实到事实板（facts.jsonl），下一 turn 起注入系统提示，全队可见。"
    accesses = [ToolAccess(resource="state", mode="w")]
    args_model = PostFactArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        ctx.facts.post_fact(a.fact, a.evidence_handle)
        return ToolResult(output=f"事实已记录: {a.fact}")


class PostDeadEndArgs(BaseModel):
    direction: str = Field(description="已排除的方向（什么面、什么手法）")
    reason: str = Field(description="排除原因（观察到了什么）")


class PostDeadEndTool(Tool):
    name = "post_deadend"
    description = "记录一条已排除方向（死路）到事实板，防重复劳动——写死路和写事实一样有价值。"
    accesses = [ToolAccess(resource="state", mode="w")]
    args_model = PostDeadEndArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        ctx.facts.post_deadend(a.direction, a.reason)
        return ToolResult(output=f"死路已记录: {a.direction}")

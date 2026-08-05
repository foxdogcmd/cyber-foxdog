"""activate_skill：把 SKILL.md 全文注入上下文（作为 tool result 返回）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..skills.loader import SkillRegistry
from ..tools import Tool, ToolAccess, ToolContext, ToolResult


class ActivateSkillArgs(BaseModel):
    name: str = Field(description="技能名（系统提示技能目录里列出的）")


class ActivateSkillTool(Tool):
    name = "activate_skill"
    description = "加载指定技能的完整手册（SKILL.md 全文）到上下文。场景匹配时先加载再动手。"
    accesses = [ToolAccess(resource="meta", mode="r")]
    args_model = ActivateSkillArgs

    def __init__(self, skills: SkillRegistry) -> None:
        self.skills = skills

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        ok, text = self.skills.activate(a.name)
        return ToolResult(ok=ok, output=text if ok else "", error=None if ok else text)

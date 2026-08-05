"""todo_list：计划管理工具（整体替换式，渲染回上下文防失忆）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "done"] = "pending"


class TodoArgs(BaseModel):
    items: list[TodoItem] = Field(description="完整的新计划（整体替换旧计划）")


class TodoListTool(Tool):
    name = "todo_list"
    description = "管理解题计划：整体替换 todo 列表。开工先列计划，进展及时更新。"
    accesses = [ToolAccess(resource="state", mode="w")]
    args_model = TodoArgs

    def __init__(self) -> None:
        self.items: list[TodoItem] = []

    def render(self) -> str:
        if not self.items:
            return "(空计划)"
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        return "\n".join(f"{mark[i.status]} {i.content}" for i in self.items)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        self.items = a.items
        return ToolResult(output=self.render())

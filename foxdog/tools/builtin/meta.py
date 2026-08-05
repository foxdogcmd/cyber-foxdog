"""元工具：fetch_output（CAS 归档取回）、task_list/task_output/task_stop（后台任务）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult


class FetchOutputArgs(BaseModel):
    handle: str = Field(description="CAS 句柄（截断输出里给出的 handle）")
    offset: int = Field(default=0, description="字节偏移")
    limit: int = Field(default=4000, description="最多取回字节数", le=65536)


class FetchOutputTool(Tool):
    name = "fetch_output"
    description = "按句柄取回被截断的工具输出全文片段（offset/limit 分页）。"
    accesses = [ToolAccess(resource="meta", mode="r")]
    args_model = FetchOutputArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        text = ctx.fetch_output(a.handle, a.offset, a.limit)
        ok = not text.startswith("error:")
        return ToolResult(ok=ok, output=text, error=None if ok else text)


class TaskListTool(Tool):
    name = "task_list"
    description = "列出全部后台任务及状态。"
    accesses = [ToolAccess(resource="tasks", mode="r")]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rows = ctx.background.list()
        if not rows:
            return ToolResult(output="(无后台任务)")
        lines = [
            f"{r['task_id']} [{'running' if r['running'] else 'done'}] exit={r['exit_code']} {r['elapsed_s']}s :: {r['command']}"
            for r in rows
        ]
        return ToolResult(output="\n".join(lines))


class TaskOutputArgs(BaseModel):
    task_id: str
    tail: int = Field(default=4000, description="取输出尾部多少字符")


class TaskOutputTool(Tool):
    name = "task_output"
    description = "查看后台任务的输出（ring buffer 尾部）。"
    accesses = [ToolAccess(resource="tasks", mode="r")]
    args_model = TaskOutputArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        text = ctx.background.output(a.task_id, tail=a.tail)
        if text is None:
            return ToolResult(ok=False, error=f"未知任务 {a.task_id}")
        return ToolResult(output=text or "(暂无输出)")


class TaskStopArgs(BaseModel):
    task_id: str


class TaskStopTool(Tool):
    name = "task_stop"
    description = "终止后台任务（kill 进程树）。"
    accesses = [ToolAccess(resource="tasks", mode="w")]
    args_model = TaskStopArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        ok = await ctx.background.stop(a.task_id)
        return ToolResult(ok=ok, output=f"已停止 {a.task_id}" if ok else "", error=None if ok else f"未知任务 {a.task_id}")

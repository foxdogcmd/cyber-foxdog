"""shell_session：持久交互 shell 会话（交互式 nc / gdb / ftp 靠它）。

action=open 启动持久 shell（Windows PowerShell 7，POSIX bash -i / sh -i），
write 写 stdin（自动补换行），read 非阻塞读新增输出（wait_ms 可等收集），
close 终止进程树。输出超阈值走 CAS spill。状态跨命令保持（cd、变量都留）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ...core.context import spill_text
from .. import Tool, ToolAccess, ToolContext, ToolResult

READ_SPILL = 32 * 1024  # read 输出进上下文阈值


class ShellSessionArgs(BaseModel):
    action: Literal["open", "write", "read", "close"] = Field(description="会话操作")
    session_id: str = Field(description="会话标识（自定义，如 nc1 / gdb1）")
    data: str | None = Field(default=None, description="write 时写入 stdin 的内容（自动补换行）")
    wait_ms: int = Field(default=500, description="read/write 后等待收集输出的毫秒数，0 为纯非阻塞", ge=0, le=60_000)


class ShellSessionTool(Tool):
    name = "shell_session"
    description = (
        "持久交互 shell 会话：open 开新会话（Windows PowerShell 7 / POSIX sh|bash -i，状态跨命令保持），"
        "write 写 stdin（自动补换行），read 读新增输出（wait_ms 收集），close 关闭。"
        "交互式 nc、gdb、ftp、ssh 用它；一次性命令用 shell_exec。"
    )
    accesses = [ToolAccess(resource="shell", mode="w")]
    args_model = ShellSessionArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            if a.action == "open":
                sess = await ctx.sessions.open(a.session_id, cwd=str(ctx.workspace.work_dir), runtime=ctx.runtime)
                out = await ctx.sessions.read(a.session_id, wait_ms=max(a.wait_ms, 300))
                return ToolResult(
                    output=f"会话 {a.session_id} 已建立（pid={sess.proc.pid}）。活跃会话: {ctx.sessions.ids()}\n{out}".rstrip()
                )
            if a.action == "write":
                if a.data is None:
                    return ToolResult(ok=False, error="write 需要 data 参数")
                await ctx.sessions.write(a.session_id, a.data)
                out = await ctx.sessions.read(a.session_id, wait_ms=a.wait_ms)
                return self._render(ctx, out)
            if a.action == "read":
                out = await ctx.sessions.read(a.session_id, wait_ms=a.wait_ms)
                return self._render(ctx, out)
            # close
            tail = await ctx.sessions.close(a.session_id)
            return ToolResult(output=f"会话 {a.session_id} 已关闭。\n{tail}".rstrip())
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))

    def _render(self, ctx: ToolContext, out: str) -> ToolResult:
        if not out:
            return ToolResult(output="(暂无新输出)")
        view, handle, _ = spill_text(ctx.workspace, out, READ_SPILL)
        return ToolResult(output=view, handle=handle)

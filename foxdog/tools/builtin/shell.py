"""shell_exec：一次性执行命令，支持后台分流。

- 默认同步执行（前台宽限期 promote_after，默认 120s）。
- 宽限期内未结束 → 自动转后台任务，返回 task_id，用 task_output 轮询。
- 模型也可直接 background=true 指定后台执行。
- TUI 可用 Ctrl+B 提前解除前台等待，子进程继续运行，完成结果自动通知 agent。
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from ...executor.process import DEFAULT_TOTAL_TIMEOUT
from .. import Tool, ToolAccess, ToolContext, ToolResult


class ShellArgs(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    background: bool = Field(default=False, description="是否直接放后台执行")
    timeout: int | None = Field(default=None, description="总时限（秒），默认 1800")


class ShellExecTool(Tool):
    name = "shell_exec"
    description = (
        "在当前执行环境运行 shell 命令（local 为 POSIX sh/Windows PowerShell 7，Docker 为 POSIX sh）。"
        "stdout/stderr 合并返回。"
        "Windows 上的计算、编码、payload、解析或文件处理优先 Python：短逻辑直接 python_eval/Python -c，"
        "需要复用、调试或多步执行时再写 .py；PowerShell 主要用于短原生命令、目录查看和进程启动。"
        "长任务（扫描/爆破）用 background=true 转后台，再 task_output 轮询。"
    )
    accesses = [ToolAccess(resource="shell", mode="w")]
    args_model = ShellArgs

    def __init__(self, promote_after: float = 120.0) -> None:
        self.promote_after = promote_after

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        total = float(a.timeout) if a.timeout else DEFAULT_TOTAL_TIMEOUT

        async def runner(on_output):
            return await ctx.runtime.run_shell(
                a.command,
                cwd=ctx.work_dir,
                env=ctx.execution_env(),
                total_timeout=total,
                cas_store=ctx.workspace.cas_store,
                on_output=on_output,
            )

        bt = ctx.background.start(a.command, runner)
        if a.background:
            bt.detached = True
            return ToolResult(output=f"已转入后台任务 {bt.task_id}，用 task_output 查看进度、task_stop 终止。")

        bt.foreground_waiting = True
        done_wait = asyncio.create_task(bt.done.wait())
        detach_wait = asyncio.create_task(bt.detach_requested.wait())
        try:
            finished, _ = await asyncio.wait(
                {done_wait, detach_wait},
                timeout=self.promote_after,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            bt.foreground_waiting = False
            for waiter in (done_wait, detach_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(done_wait, detach_wait, return_exceptions=True)

        if done_wait not in finished:
            bt.detached = True
            reason = "已按 Ctrl+B 转入后台" if detach_wait in finished else f"运行超过 {int(self.promote_after)}s，已自动转后台"
            return ToolResult(
                output=f"命令{reason}任务 {bt.task_id}，"
                "用 task_output 查看进度、task_stop 终止。"
            )

        res = bt.result
        assert res is not None
        if res.error:
            return ToolResult(ok=False, error=res.error)
        note = ""
        if res.idle_timed_out:
            note = "[无输出空闲超时，已 kill]\n"
        elif res.timed_out:
            note = "[总时限超时，已 kill]\n"
        return ToolResult(
            ok=res.exit_code == 0 and not (res.timed_out or res.idle_timed_out),
            output=f"{note}exit_code={res.exit_code}\n{res.output}",
            handle=res.handle,
            data={"task_id": bt.task_id, "exit_code": res.exit_code},
        )

"""copilot 工具（/setting 全局副驾的指挥能力，解题中也可用）。

host 是 ChatApp（鸭子类型）：读 current_run/steer/apply_budget/active coordinator。
配置类工具在 config_edit.py；这里是指挥类：get_status / steer_team /
kill_worker / suppress_intent / set_budget。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult


class _HostTool(Tool):
    accesses = [ToolAccess(resource="state", mode="w")]

    def __init__(self, host: Any) -> None:
        self.host = host  # ChatApp（鸭子类型，避免循环导入）


class GetStatusTool(_HostTool):
    name = "get_status"
    description = "查看当前状态：任务/run、worker 列表与各自 intent、token 用量、预算。"
    accesses = [ToolAccess(resource="state", mode="r")]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        run = self.host.current_run
        if run is None:
            return ToolResult(output="当前空闲（无运行中任务）。")
        lines = [f"run_id: {run.ws.run_id}", f"题目: {run.objective[:60]}"]
        lines.append(f"预算: {run.budget.describe()}")
        state = run.loop.budget_state
        if run.coordinator is not None and run.coordinator.factory.budget_state is not None:
            state = run.coordinator.factory.budget_state
        lines.append(f"tokens: {state.total_tokens if state else 0}")
        lines.append(f"flag: {run.gate.flag or '未拿到'}")
        if run.coordinator is not None:
            active = run.coordinator.active_workers()
            lines.append(f"在跑 worker（{len(active)}）：")
            lines += [f"  {wid} → {iid}" for wid, iid in active] or ["  （无）"]
        else:
            lines.append("单 agent 模式（无 swarm worker）")
        return ToolResult(output="\n".join(lines))


class SteerTeamArgs(BaseModel):
    text: str = Field(description="操作员指令（方向调整/优先做什么）")


class SteerTeamTool(_HostTool):
    name = "steer_team"
    description = "指挥全体：指令上黑板控制通道（planner/worker 可见）+ 入主 steer 队列。"
    args_model = SteerTeamArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        self.host.steer.put(a.text)
        run = self.host.current_run
        if run is not None and run.ctx.facts is not None:
            run.ctx.facts.post_directive(f"操作员指令：{a.text}", actor="operator", priority="critical")
            return ToolResult(output=f"指令已上黑板并入队：{a.text}")
        return ToolResult(output=f"当前无任务，指令已入主 steer 队列（下一任务生效）：{a.text}")


class KillWorkerArgs(BaseModel):
    worker_id: str = Field(description="要终止的 worker（get_status 里看 id）")


class KillWorkerTool(_HostTool):
    name = "kill_worker"
    description = "终止指定 swarm worker（task 取消 + intent 置 barren）。"
    args_model = KillWorkerArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        run = self.host.current_run
        if run is None or run.coordinator is None:
            return ToolResult(ok=False, error="当前无 swarm 任务（单 agent 的主任务用 Esc 中断）")
        if await run.coordinator.cancel_worker(a.worker_id):
            return ToolResult(output=f"已终止 {a.worker_id}")
        return ToolResult(ok=False, error=f"未找到在跑的 worker {a.worker_id}")


class SuppressIntentArgs(BaseModel):
    intent_id: str
    reason: str = Field(description="压制原因（为什么这方向别再做）")


class SuppressIntentTool(_HostTool):
    name = "suppress_intent"
    description = "把 intent 置 barren（这个方向别再派；claimed 的也会被压住，worker 收尾不覆盖）。"
    args_model = SuppressIntentArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        run = self.host.current_run
        board = run.ctx.facts if run is not None else None
        if board is None:
            return ToolResult(ok=False, error="当前无任务黑板")
        if board.complete_intent(a.intent_id, "barren", a.reason, actor="operator"):
            return ToolResult(output=f"intent {a.intent_id} 已置 barren：{a.reason}")
        return ToolResult(ok=False, error=f"intent {a.intent_id} 不在 open/claimed 状态")


class SetBudgetArgs(BaseModel):
    minutes: float | None = Field(default=None, description="时间预算（分钟）")
    tokens: int | None = Field(default=None, description="token 预算")
    turns: int | None = Field(default=None, description="轮数预算")
    off: bool = Field(default=False, description="true=全部取消限制（无限）")


class SetBudgetTool(_HostTool):
    name = "set_budget"
    description = "动态调整预算（运行中立即生效）；off=true 取消全部限制。"
    args_model = SetBudgetArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        self.host.apply_budget(minutes=a.minutes, tokens=a.tokens, turns=a.turns, off=a.off)
        budget = self.host.session.cfg.budget if self.host.session else None
        return ToolResult(output=f"预算已调整：{budget.describe() if budget else ''}（运行中任务立即生效）")


def register_copilot_tools(registry, host: Any) -> None:
    for tool in (GetStatusTool(host), SteerTeamTool(host), KillWorkerTool(host), SuppressIntentTool(host), SetBudgetTool(host)):
        registry.register(tool)

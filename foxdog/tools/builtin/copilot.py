"""copilot 工具（/setting 全局副驾的会话/指挥能力，解题中也可用）。

host 是 ChatApp（鸭子类型）：读 current_run/steer/apply_budget/active coordinator。
配置落盘工具在 config_edit.py；这里是 TUI 会话态能力：get_status / steer_team /
kill_worker / suppress_intent / set_budget / list_runs / resume_run / fetch_challenge /
set_task_options。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...store.rollout import find_run_summary, list_run_summaries
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
        factory = getattr(run.coordinator, "factory", None) if run.coordinator is not None else None
        if getattr(factory, "budget_state", None) is not None:
            state = factory.budget_state
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


class SetTaskOptionsArgs(BaseModel):
    target: str | None = Field(default=None, description="下一题靶机 URL/IP；传空字符串或 clear_target=true 可清空")
    flag_format: str | None = Field(default=None, description="下一题 flag 正则；传空字符串或 clear_flag_format=true 可清空")
    workers: int | None = Field(default=None, ge=1, description="下一题 swarm worker 上限")
    clear_target: bool = Field(default=False, description="true=清空下一题 target")
    clear_flag_format: bool = Field(default=False, description="true=清空下一题 flag_format")


class SetTaskOptionsTool(_HostTool):
    name = "set_task_options"
    description = "设置当前 TUI 会话的下一题选项：target、flag_format、worker 上限；不落盘。"
    args_model = SetTaskOptionsArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        changes: list[str] = []
        if a.clear_target or a.target is not None:
            self.host.opt_target = None if a.clear_target else (a.target or None)
            changes.append(f"target={self.host.opt_target or '（未设置）'}")
        if a.clear_flag_format or a.flag_format is not None:
            self.host.opt_format = None if a.clear_flag_format else (a.flag_format or None)
            changes.append(f"flag_format={self.host.opt_format or '（未设置）'}")
        if a.workers is not None:
            self.host.opt_workers = int(a.workers)
            changes.append(f"workers={self.host.opt_workers}")
        if changes:
            setattr(self.host, "_session_task_options_dirty", True)
        try:
            self.host._sync_board_sidebar()
            self.host._refresh_status()
        except Exception:
            pass
        return ToolResult(output="下一题选项已更新：" + ("；".join(changes) if changes else "无变化"))


class ListRunsArgs(BaseModel):
    limit: int = Field(default=15, ge=1, le=50, description="最多返回多少条历史 run")


class ListRunsTool(_HostTool):
    name = "list_runs"
    description = "列出可续跑的历史 run（最新在上），用于替代 /runs。"
    accesses = [ToolAccess(resource="state", mode="r")]
    args_model = ListRunsArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        items = list(reversed(list_run_summaries(self.host._workspace, limit=a.limit)))
        if not items:
            return ToolResult(output="还没有任何历史 run。", data={"runs": []})
        lines = ["历史 run（最新在上）："]
        data: list[dict[str, Any]] = []
        for item in items:
            status = item.flag or ("solved" if item.solved else "unsolved")
            objective = item.objective[:60] or "（未记录目标）"
            lines.append(f"- {item.run_id}  {status}  {objective}")
            data.append(
                {
                    "run_id": item.run_id,
                    "objective": item.objective,
                    "target": item.target,
                    "flag_format": item.flag_format,
                    "solved": item.solved,
                    "flag": item.flag,
                    "workers": item.workers,
                }
            )
        return ToolResult(output="\n".join(lines), data={"runs": data})


class ResumeRunArgs(BaseModel):
    run_id: str | None = Field(default=None, description="要续跑的 run_id；省略则续最近一个")


class ResumeRunTool(_HostTool):
    name = "resume_run"
    description = "续跑一个历史 run；省略 run_id 时续最近一个，用于替代 /resume。"
    args_model = ResumeRunArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        run_id = (a.run_id or "").strip()
        if getattr(self.host, "busy", False):
            return ToolResult(ok=False, error="当前有任务在跑，先中断或等完成后再续跑。")
        if self.host.session is None or not self.host._provider_ready():
            return ToolResult(ok=False, error="resume 依赖模型，请先配置模型。")
        item = find_run_summary(self.host._workspace, run_id or None)
        if item is None:
            hint = f"：{run_id}" if run_id else ""
            return ToolResult(ok=False, error=f"找不到可续跑的 run{hint}。")
        if not item.objective:
            return ToolResult(ok=False, error=f"{item.run_id} 未记录目标，无法续跑。")
        self.host._resume_run(item.run_id)
        return ToolResult(output=f"已开始续跑 {item.run_id}：{item.objective[:80]}")


class FetchChallengeArgs(BaseModel):
    identifier: str = Field(description="题目 id 或 URL；配置 platform.fetch_url 时会按 id 拉取")


class FetchChallengeTool(_HostTool):
    name = "fetch_challenge"
    description = "拉取题目描述到输入框；支持平台 fetch_url 或直接 URL，用于替代 /fetch。"
    accesses = [ToolAccess(resource="state", mode="w"), ToolAccess(resource="network", mode="r")]
    args_model = FetchChallengeArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        identifier = a.identifier.strip()
        if not identifier:
            return ToolResult(ok=False, error="identifier 不能为空。")
        if self.host.session is None:
            return ToolResult(ok=False, error="会话未初始化，请先配置模型。")
        ok = await self.host._fetch_task(identifier)
        if not ok:
            return ToolResult(ok=False, error="题目拉取失败，详情见主时间线。")
        return ToolResult(output=f"题目已拉取到输入框：{identifier}")


def register_copilot_tools(registry, host: Any) -> None:
    for tool in (
        GetStatusTool(host),
        SteerTeamTool(host),
        KillWorkerTool(host),
        SuppressIntentTool(host),
        SetBudgetTool(host),
        SetTaskOptionsTool(host),
        ListRunsTool(host),
        ResumeRunTool(host),
        FetchChallengeTool(host),
    ):
        registry.register(tool)

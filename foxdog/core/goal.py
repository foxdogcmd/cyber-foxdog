"""GoalMode：solve 目标模式（借鉴 kimi-code agent/goal）。

循环：while not solved and not budget_exceeded: run_turn()。
agent 一轮自然停下（无 tool call 且未 solved）时注入"继续解题"提示自动续跑。
三维预算：max_minutes / max_tokens（step 级强制，见 core/budget.py）、max_turns（turn 间检查）。
max_steps 是单 turn 分片上限，达到后开启 continuation turn，不等同总预算耗尽。
每 turn 开始：on_turn_start 钩子（刷新系统提示）+ drain steer 队列注入操作员指令。

返回 exit code：0=flag 验证通过，2=预算耗尽，3=异常/中断，4=正常完成但无 flag。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..llm.messages import user_message
from ..protocol.types import Error, SwarmNote
from .budget import Budget, BudgetExceeded, BudgetState
from .loop import AgentLoop
from .steer import SteerQueue

__all__ = ["Budget", "BudgetExceeded", "BudgetState", "GoalMode",
           "EXIT_SOLVED", "EXIT_BUDGET", "EXIT_ERROR", "EXIT_COMPLETE"]

EXIT_SOLVED = 0
EXIT_BUDGET = 2
EXIT_ERROR = 3
EXIT_COMPLETE = 4
MAX_NO_PROGRESS_TURNS = 3


class GoalMode:
    def __init__(
        self,
        loop: AgentLoop,
        budget: Budget,
        on_turn_start: Callable[[list[dict[str, Any]], int], None] | None = None,
        steer: SteerQueue | None = None,
    ) -> None:
        self.loop = loop
        self.budget = budget
        self.on_turn_start = on_turn_start
        self.steer = steer
        self.last_stop_reason: str | None = None

    def _continue_prompt(self, turns: int, stop_reason: str, no_progress_turns: int) -> str:
        if stop_reason == "truncated":
            reason = "上一轮输出达到模型上限，未能产生完整动作"
        elif stop_reason == "max_steps":
            reason = "上一轮达到单 turn 步数上限，任务本身仍在继续"
        else:
            reason = "上一轮自然暂停，但 flag 尚未验证"
        stalled = (
            f" 已连续 {no_progress_turns} 轮没有工具进展，必须立即换一个具体方向。"
            if no_progress_turns > 1
            else ""
        )
        return (
            f"[目标续跑 turn {turns + 1}] {reason}。{stalled}"
            "保持原题目、真实目标地址和现有证据不变；回顾 todo_list、已知事实与已排除方向，"
            "本轮只选择一个有界且能产生新证据的动作并立即调用工具。"
            "不要重复阶段总结；若已有候选 flag，立刻 submit_flag。"
            "\n\n⚠ 如果你上一轮发现了重要线索但没有 post_fact 记录，本轮第一步必须先 post_fact 记录这些发现，再继续推进。"
        )

    def _drain_steer(self, history: list[dict[str, Any]]) -> None:
        """每 turn 开始把操作员指令作为 user message 注入（rollout 记 steer 类型）。"""
        if self.steer is None:
            return
        for text in self.steer.drain():
            history.append(user_message(f"[操作员 steer 指令] {text}"))
            self.loop._record({"type": "steer", "text": text})
            self.loop.emit(SwarmNote(message=f"steer 注入：{text}"))

    async def _wait_for_background(self, state: BudgetState) -> None:
        """Avoid model polling when a naturally completed turn is only waiting on detached work."""
        background = self.loop.orchestrator.ctx.background
        if not background.has_running_detached() or background.has_completed_detached():
            return
        self.loop.emit(SwarmNote(message="当前无其他动作，等待后台任务完成（可继续 steer 或 Esc 中断）"))
        while background.has_running_detached() and not background.has_completed_detached():
            if self.loop.interrupt.is_set() or (self.steer is not None and self.steer.has_pending()):
                return
            state.check()
            await asyncio.sleep(0.1)

    async def solve(self, history: list[dict[str, Any]]) -> int:
        state = BudgetState(self.budget)
        self.loop.budget_state = state  # 注入：loop 每个 step 前强制检查
        turns = 0
        no_progress_turns = 0
        self.last_stop_reason = None

        while not self.loop.solved:
            if self.loop.interrupt.is_set():
                self.last_stop_reason = "interrupted"
                return EXIT_ERROR
            if self.budget.max_turns is not None and turns >= self.budget.max_turns:
                self.last_stop_reason = "max_turns"
                return EXIT_BUDGET
            if self.on_turn_start is not None:
                self.on_turn_start(history, turns + 1)
            self._drain_steer(history)

            try:
                result = await self.loop.run_turn(
                    history,
                    turn_no=turns + 1,
                    require_first_tool=turns > 0,
                )
            except BudgetExceeded:
                self.last_stop_reason = "budget"
                return EXIT_BUDGET
            turns += 1
            self.last_stop_reason = result.stop_reason

            if self.loop.solved:
                return EXIT_SOLVED
            # The interrupt may arrive after the model completed but before the
            # continuation turn is appended. Never turn a stop request into one
            # more automatic model request.
            if self.loop.interrupt.is_set():
                self.last_stop_reason = "interrupted"
                return EXIT_ERROR
            if result.stop_reason in ("error", "interrupted"):
                return EXIT_ERROR
            no_progress_turns = no_progress_turns + 1 if result.steps == 0 else 0
            if no_progress_turns >= MAX_NO_PROGRESS_TURNS:
                self.last_stop_reason = "stalled"
                self.loop.emit(
                    Error(message=f"连续 {no_progress_turns} 个 turn 没有工具进展，已停止当前路径避免空转")
                )
                return EXIT_ERROR
            # completed/truncated/max_steps：开启且仅开启下一 continuation turn。
            history.append(
                user_message(self._continue_prompt(turns, result.stop_reason, no_progress_turns))
            )
            try:
                await self._wait_for_background(state)
            except BudgetExceeded:
                return EXIT_BUDGET
        return EXIT_SOLVED

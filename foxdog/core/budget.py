"""三维预算与 step 级强制（M1 教训：只在 turn 间检查，单 turn 内会无限跑）。

Budget：max_minutes / max_tokens / max_turns 三维上限，**None=该维度不限**
（用户未指定时不设任何预算上限，UX 大修后的默认）。
BudgetState：跨 turn 累计消耗（起始时间、累计 tokens），AgentLoop 每个 step 前
调 check()，超限抛 BudgetExceeded；GoalMode 捕获后 exit 2。
max_turns 仍由 GoalMode 在 turn 间检查（turn 是 GoalMode 的概念）。
预算对象可变：/setting 的 set_budget 工具运行中直接改 Budget 字段，下一次 check 即生效。
"""
from __future__ import annotations

import time

from pydantic import BaseModel


class Budget(BaseModel):
    max_minutes: float | None = None  # None=不限（默认无限）
    max_tokens: int | None = None
    max_turns: int | None = None

    def describe(self) -> str:
        def m(v: float | None) -> str:
            return "∞" if v is None else f"{v:g}"

        return f"minutes={m(self.max_minutes)} tokens={m(self.max_tokens)} turns={m(self.max_turns)}"


class BudgetExceeded(Exception):
    """预算耗尽（step 级抛出，goal 捕获 → exit 2）。"""

    def __init__(self, dimension: str, detail: str) -> None:
        self.dimension = dimension  # minutes | tokens
        super().__init__(f"预算耗尽（{dimension}）：{detail}")


class BudgetState:
    """一次 solve 的预算消耗账本（loop 每 step 前 check，每 step 后 add_tokens）。"""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.started = time.monotonic()
        self.total_tokens = 0

    @property
    def elapsed_minutes(self) -> float:
        return (time.monotonic() - self.started) / 60

    def add_tokens(self, n: int) -> None:
        self.total_tokens += n

    def check(self) -> None:
        """任一维度耗尽即抛 BudgetExceeded（None 维度跳过）。"""
        if self.budget.max_minutes is not None and self.elapsed_minutes >= self.budget.max_minutes:
            raise BudgetExceeded(
                "minutes", f"已用 {self.elapsed_minutes:.2f} 分钟 ≥ 上限 {self.budget.max_minutes} 分钟"
            )
        if self.budget.max_tokens is not None and self.total_tokens >= self.budget.max_tokens:
            raise BudgetExceeded(
                "tokens", f"已用 {self.total_tokens} tokens ≥ 上限 {self.budget.max_tokens}"
            )

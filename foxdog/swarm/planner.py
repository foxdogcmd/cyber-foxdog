"""ReasonPlanner（DESIGN §6.4）：读黑板摘要 → 严格 JSON 出 ≤4 个 intent。

用 [model.planner] 配置的便宜模型（无配置则 solver 模型）。解析要硬：
```json 围栏容忍、JSONDecodeError 重试一次、verdict 非法值 fallback explore。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm.client import ChatProvider

PLANNER_INSTRUCTION = """\
你是 CTF 解题 swarm 的规划器。根据题目与黑板摘要产出下一步探索意图（intents）。

规则：
1. 已知事实、已排除方向、在跑/已完成的 intent 不要重复派发。
2. coordinator 会给出用户设定的并发上限、当前在跑/待领数量和本轮缺口。
   **worker 数量由用户决定，不由你决定**；在不制造重复工作的前提下，尽量为本轮缺口
   产出可独立执行的 intent（单轮最多 4 个）。
3. 优先收敛（验证现有线索、推进在跑方向）而非无关发散。复杂题即使主路线明确，
   也应把能独立产出证据的工作拆开并行，例如静态原语确认、动态偏移验证、保护/符号
   核对、exploit 稳定性验证；存在真实依赖时才串行，不要虚构凑数任务。
4. **互相独立的方向一次多派（最多 4 个并发 intent，worker 会并行认领）**；
   只有相互依赖（B 依赖 A 的结论）的方向才串行——本轮只派 A。
5. 每个 intent 必须自洽：goal 含具体目标（URL/IP:端口/文件路径）与成功标准；
   category/surface/technique 三元组标定方向（黑板按它去重，别瞎编）；
   lane 仅当该方向会独占危险资源时给（如爆破 8080：brute:tcp:8080@host）。
6. audit 里列出你不信任的 fact id（编号），没有就空数组。
7. verdict：explore=继续派活；course_correct=方向漂移需要纠偏；complete=确认收工。
   complete 只在目标已完成，或所有合理方向都有证据排除时使用；此时 intents 必须为空，
   summary 必须给操作员一段简洁结论（发现、证据、未达成项和建议）。

只输出 JSON（不要任何多余文字）：
{"verdict": "explore|complete|course_correct",
 "summary": "仅 verdict=complete 时填写最终结论，否则留空",
 "intents": [{"goal": "...", "category": "...", "surface": "...", "technique": "...", "lane": null}] (≤4),
 "audit": [1, 2]}
"""


class IntentSpec(BaseModel):
    goal: str
    category: str
    surface: str
    technique: str
    lane: str | None = None


class PlanResult(BaseModel):
    verdict: Literal["explore", "complete", "course_correct"] = "explore"
    intents: list[IntentSpec] = Field(default_factory=list)  # 解析后截断到 ≤4
    audit: list[int] = Field(default_factory=list)
    # Deprecated: coordinator ignores this. Worker concurrency is owned by user config.
    desired_workers: int | None = Field(default=None, ge=1, le=4)
    summary: str = ""


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_plan(text: str) -> tuple[PlanResult, bool]:
    """Return the parsed plan and whether the payload was valid planner JSON."""
    candidates = [text.strip()]
    m = _FENCE_RE.search(text)
    if m:
        candidates.insert(0, m.group(1))
    # 容忍首尾散文：找第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("verdict") not in ("explore", "complete", "course_correct"):
            data["verdict"] = "explore"  # 非法 verdict fallback
        try:
            result = PlanResult(**data)
        except Exception:  # noqa: BLE001 - 字段级非法
            continue
        result.intents = result.intents[:4]
        return result, True
    return PlanResult(), False


def parse_plan(text: str) -> PlanResult:
    """硬解析 planner 输出；任何非法都 fallback 成空 explore（绝不炸协调循环）。"""
    return _parse_plan(text)[0]


class ReasonPlanner:
    def __init__(self, provider: ChatProvider, max_intents: int = 4) -> None:
        self.provider = provider
        self.max_intents = max_intents

    def build_prompt(self, objective: str, board_summary: str) -> str:
        return f"{PLANNER_INSTRUCTION}\n\n题目：{objective}\n\n{board_summary}"

    async def plan(
        self,
        objective: str,
        board_summary: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> PlanResult:
        """调模型出计划；JSON 解析失败重试一次。"""
        prompt = self.build_prompt(objective, board_summary)
        for attempt in range(2):
            msg = await self.provider.stream_chat(
                [{"role": "user", "content": prompt}],
                [],
                on_delta or (lambda _d: None),
                on_reasoning_delta=on_reasoning_delta,
            )
            result, parsed = _parse_plan(msg.content)
            # 合法的空 intents 是调度决策，不是解析失败；只有坏 JSON 才重试。
            if parsed or not msg.content.strip() or attempt == 1:
                return result
        return result  # pragma: no cover

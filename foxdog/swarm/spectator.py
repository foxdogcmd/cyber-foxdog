"""Spectator：观察整个 swarm，纠偏或决定收口。

旁观者只输出结构化审查结论，不直接调用工具。correct 通过 coordinator 的
高优先级 steer 机制抢占当前 worker；complete 由 coordinator 负责停止并生成报告。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from ..llm.client import ChatProvider


class SpectatorReview(BaseModel):
    verdict: Literal["continue", "correct", "complete"] = "continue"
    instruction: str = ""
    summary: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_review(text: str) -> SpectatorReview:
    candidates = [text.strip()]
    match = _FENCE.search(text)
    if match:
        candidates.insert(0, match.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return SpectatorReview(**data)
        except Exception:
            continue
    return SpectatorReview(summary="旁观者输出无法解析，保持当前计划")


class Spectator:
    def __init__(self, provider: ChatProvider) -> None:
        self.provider = provider

    @staticmethod
    def build_prompt(objective: str, board: str, workers: str, recent: str) -> str:
        return f"""你是网络安全任务的旁观者审查员，统揽整个流程，不亲自执行命令。
审查目标：{objective}
黑板（事实、死路、intents）：
{board}
当前 worker 状态：
{workers}
最近事件：
{recent}

判断方向是否偏离目标、是否存在重复/危险/遗漏。只输出 JSON：
{{"verdict":"continue|correct|complete","instruction":"correct 时给全队的下一步指令，否则为空","summary":"给操作员的结论","confidence":0.0}}
complete 只在任务已完成或所有合理方向均有证据排除时使用；不要因为暂时没有新事件就 complete。"""

    async def review(
        self,
        objective: str,
        board: str,
        workers: str,
        recent: str,
        *,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> SpectatorReview:
        msg = await self.provider.stream_chat(
            [{"role": "user", "content": self.build_prompt(objective, board, workers, recent)}],
            [],
            lambda _delta: None,
            on_reasoning_delta=on_reasoning_delta,
        )
        return parse_review(msg.content)


__all__ = ["Spectator", "SpectatorReview", "parse_review"]

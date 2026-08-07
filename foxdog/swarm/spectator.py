"""Spectator：观察整个 swarm，纠偏或决定收口。

旁观者输出结构化审查结论。correct 可附带保守的调度动作，由 coordinator
关闭重复 intent、取消对应 worker，并把纠偏写成控制指令；complete 由
coordinator 负责停止并生成报告。
"""
from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm.client import ChatProvider


class SpectatorReview(BaseModel):
    verdict: Literal["continue", "correct", "complete"] = "continue"
    instruction: str = ""
    summary: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    close_intents: list[str] = Field(default_factory=list)
    close_status: Literal["done", "barren"] = "barren"
    close_reason: str = ""
    cancel_workers: list[str] = Field(default_factory=list)
    merge_groups: list[list[str]] = Field(default_factory=list)
    blocked_by_external: str | None = None  # 新增：外部依赖标识（如 "remote:challenge.imxbt.cn:32495"）


_FENCE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL)
_INTENT_REF_RE = re.compile(r"\bI\d+(?:-[0-9a-f]{6})?\b", re.IGNORECASE)
_WORKER_REF_RE = re.compile(r"\bworker-\d+\b", re.IGNORECASE)
_ACTION_RE = re.compile(r"(?:关闭|合并|归并|终止|取消|停止|纠偏|close|merge|cancel|kill)", re.IGNORECASE)
_COMPLETE_RE = re.compile(r"(?:任务已完成|可以收口|确认收口|已解决|verdict\s*[:=]\s*complete|\"complete\"|\bcomplete\b|\bsolved\b)", re.IGNORECASE)
_CLOSE_TEXT_RE = re.compile(
    r"(?:关闭|终止|取消|停止|close|cancel|kill)[^。；;\n]*",
    re.IGNORECASE,
)
_MERGE_TEXT_RE = re.compile(
    r"(?:把|将)\s*((?:I\d+(?:-[0-9a-f]{6})?[\s、,，/和]*){2,})\s*(?:合并|归并|并入|merge)",
    re.IGNORECASE,
)


def _jsonish_loads(candidate: str) -> Any:
    text = candidate.strip()
    variants = [
        text,
        re.sub(r",\s*([}\]])", r"\1", text),
    ]
    for variant in dict.fromkeys(variants):
        try:
            return json.loads(variant)
        except json.JSONDecodeError:
            pass
        try:
            return ast.literal_eval(variant)
        except Exception:
            pass
    raise ValueError("not json-like")


def _first_review_dict(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        return dict(data)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return dict(item)
    return None


def _string_list(value: Any, *, pattern: re.Pattern[str] | None = None) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return pattern.findall(value) if pattern else [value]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and pattern:
                refs = pattern.findall(item)
                out.extend(refs or [item])
            elif item not in (None, ""):
                out.append(str(item))
        return out
    return [str(value)]


def _merge_groups(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    groups: list[list[str]] = []
    for item in value:
        refs = _string_list(item, pattern=_INTENT_REF_RE)
        if len(refs) >= 2:
            groups.append(refs)
    return groups


def _normalize_verdict(data: dict[str, Any]) -> str:
    raw = str(data.get("verdict") or "").strip().lower()
    has_action = bool(
        data.get("instruction")
        or data.get("close_reason")
        or data.get("close_intents")
        or data.get("cancel_workers")
        or data.get("merge_groups")
    )
    if raw in {"continue", "correct", "complete"}:
        return raw
    if "|" in raw:
        return "correct" if has_action else "continue"
    if any(token in raw for token in ("complete", "done", "solved", "完成", "收口", "解决")):
        return "complete"
    if any(token in raw for token in ("correct", "course", "action", "fix", "steer", "纠偏", "调整", "关闭", "合并", "取消")):
        return "correct"
    return "correct" if has_action else "continue"


def _review_from_data(data: dict[str, Any]) -> SpectatorReview | None:
    payload = dict(data)
    payload["verdict"] = _normalize_verdict(payload)
    payload["close_intents"] = _string_list(payload.get("close_intents"), pattern=_INTENT_REF_RE)
    payload["cancel_workers"] = _string_list(payload.get("cancel_workers"), pattern=_WORKER_REF_RE)
    payload["merge_groups"] = _merge_groups(payload.get("merge_groups"))
    if payload.get("close_status") not in ("done", "barren"):
        payload["close_status"] = "barren"
    try:
        payload["confidence"] = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        payload["confidence"] = 0.5
    try:
        return SpectatorReview(**payload)
    except Exception:
        return None


def _review_from_plain_text(text: str) -> SpectatorReview:
    summary = " ".join(text.strip().split())
    if not summary:
        return SpectatorReview()
    if _ACTION_RE.search(summary) and (_INTENT_REF_RE.search(summary) or _WORKER_REF_RE.search(summary)):
        refs: list[str] = []
        for match in _CLOSE_TEXT_RE.finditer(summary):
            refs.extend(_INTENT_REF_RE.findall(match.group(0)))
        merge_groups = [
            refs
            for refs in (_INTENT_REF_RE.findall(match.group(1)) for match in _MERGE_TEXT_RE.finditer(summary))
            if len(refs) >= 2
        ]
        workers = _WORKER_REF_RE.findall(summary)
        return SpectatorReview(
            verdict="correct",
            instruction=summary,
            summary=summary,
            close_intents=refs,
            cancel_workers=workers,
            merge_groups=merge_groups,
        )
    if _COMPLETE_RE.search(summary):
        return SpectatorReview(verdict="complete", summary=summary, confidence=0.6)
    return SpectatorReview()


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
            data = _first_review_dict(_jsonish_loads(candidate))
            if data is None:
                continue
            review = _review_from_data(data)
            if review is not None:
                return review
        except Exception:
            continue
    return _review_from_plain_text(text)


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

判断方向是否偏离目标、是否存在重复/危险/遗漏。**重点识别外部依赖阻塞**：
如果多个 worker 都卡在同一个外部服务（如远程靶机连接失败、API 超时、EOF），
立即标记 blocked_by_external（格式："remote:host:port" 或 "api:service_name"），
并建议暂停相关 intent，等待外部恢复或切换策略。

只输出 JSON：
verdict 必须是 "continue"、"correct"、"complete" 三者之一，不要输出枚举占位字符串。
无动作时：
{{"verdict":"continue","instruction":"","summary":"","confidence":0.5,"close_intents":[],"close_status":"barren","close_reason":"","cancel_workers":[],"merge_groups":[],"blocked_by_external":null}}
需要纠偏或关闭重复方向时：
{{"verdict":"correct","instruction":"给全队的下一步指令","summary":"给操作员的结论","confidence":0.7,"close_intents":["I3"],"close_status":"barren","close_reason":"关闭原因","cancel_workers":["worker-3"],"merge_groups":[["I12","I13"]],"blocked_by_external":null}}
检测到外部阻塞时：
{{"verdict":"correct","instruction":"暂停所有远程连接 intent，优先本地验证","summary":"远程服务不可用","confidence":0.9,"close_intents":["I6","I13","I22"],"close_status":"barren","close_reason":"远程服务连续失败，EOF/timeout","cancel_workers":[],"merge_groups":[],"blocked_by_external":"remote:challenge.imxbt.cn:32495"}}
需要关闭/合并重复 intent 时，把要关闭的 intent id 写进 close_intents；如果是一组重复 intent，merge_groups 每组保留第一个，其余由 coordinator 关闭。只关闭有明确证据覆盖或明显重复的 intent。
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

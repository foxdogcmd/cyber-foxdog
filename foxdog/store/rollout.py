"""rollout.jsonl：每 run 一个 append-only 会话记录（借鉴 codex rollout 方案）。

首行 SessionMeta（run_id、cwd、目标、模型、时间），
后续每行一个 record（message / tool_call / tool_result / event）。
异步写：调用方 put_nowait 进队列，后台 task 落盘。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunSummary:
    """一条可展示/可续跑的历史 run 摘要。"""

    run_id: str
    path: Path
    objective: str
    target: str | None
    flag_format: str | None
    model: str
    provider: str
    mode: str
    workers: int
    ts: float
    solved: bool = False
    flag: str = ""


def runs_root(workspace: str | Path | None = None) -> Path:
    """返回某工作区的 run 根目录。"""
    return (Path(workspace) if workspace is not None else Path.cwd()) / ".foxdog-runs"


class RolloutWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._fh = None

    async def start(self, session_meta: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._write_line({"type": "session_meta", **session_meta})
        self._task = asyncio.create_task(self._drain())

    def write(self, record: dict[str, Any]) -> None:
        """非阻塞写入（调用方无需 await）。"""
        if self._task is None:
            return
        record.setdefault("ts", time.time())
        self._queue.put_nowait(record)

    def _write_line(self, record: dict[str, Any]) -> None:
        assert self._fh is not None
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    async def _drain(self) -> None:
        while True:
            record = await self._queue.get()
            if record is None:
                break
            self._write_line(record)
            self._fh.flush()

    async def close(self) -> None:
        if self._task is None:
            return
        self._queue.put_nowait(None)
        await self._task
        self._fh.close()
        self._task = None


def read_session_meta(path: str | Path) -> dict[str, Any] | None:
    """读 rollout 首行 SessionMeta（foxdog runs 用）。"""
    p = Path(path)
    if not p.is_file():
        return None
    with p.open("r", encoding="utf-8") as f:
        first = f.readline().strip()
    if not first:
        return None
    try:
        meta = json.loads(first)
    except json.JSONDecodeError:
        return None
    return meta if meta.get("type") == "session_meta" else None


def list_run_summaries(workspace: str | Path | None = None, *, limit: int | None = None) -> list[RunSummary]:
    """列出可续跑的历史 run（按时间从旧到新排序）。"""
    base = runs_root(workspace)
    if not base.is_dir():
        return []
    summaries: list[RunSummary] = []
    for run_dir in base.iterdir():
        rollout = run_dir / "rollout.jsonl"
        if not run_dir.is_dir() or not rollout.is_file():
            continue
        meta = read_session_meta(rollout) or {}
        try:
            ts = float(meta.get("ts") or run_dir.stat().st_mtime)
        except (TypeError, ValueError, OSError):
            ts = 0.0
        solved = False
        flag = ""
        winner_path = run_dir / "winner.json"
        if winner_path.is_file():
            try:
                winner = json.loads(winner_path.read_text(encoding="utf-8"))
                solved = True
                flag = str(winner.get("flag") or next(iter(winner.get("flags") or []), ""))
            except (json.JSONDecodeError, OSError, TypeError):
                solved = True
        summaries.append(
            RunSummary(
                run_id=run_dir.name,
                path=run_dir,
                objective=str(meta.get("objective") or ""),
                target=str(meta.get("target") or "") or None,
                flag_format=str(meta.get("flag_format") or "") or None,
                model=str(meta.get("model") or ""),
                provider=str(meta.get("provider") or ""),
                mode=str(meta.get("mode") or ""),
                workers=max(1, int(meta.get("workers") or 1)),
                ts=ts,
                solved=solved,
                flag=flag,
            )
        )
    summaries.sort(key=lambda item: (item.ts, item.run_id))
    if limit is not None and limit >= 0:
        summaries = summaries[-limit:]
    return summaries


def find_run_summary(workspace: str | Path | None, run_id: str | None) -> RunSummary | None:
    """按 run_id 找历史 run；run_id 为空时返回最近一条。"""
    summaries = list_run_summaries(workspace)
    if not summaries:
        return None
    if not run_id:
        return summaries[-1]
    needle = run_id.strip()
    return next((item for item in summaries if item.run_id == needle), None)


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """读 rollout 全部记录（跳过首行 meta），供 resume/writeup 重放。"""
    p = Path(path)
    if not p.is_file():
        return []
    records: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if i == 0 and rec.get("type") == "session_meta":
                continue
            records.append(rec)
    return records


def replay_history(path: str | Path) -> list[dict[str, Any]]:
    """从 rollout 重建 OpenAI 风格对话历史（assistant 消息 + tool_result），供无缝续跑。

    只重放主 agent（无 worker 前缀）的 message/tool_result，按落盘顺序保持
    tool_use↔tool_result 邻接。系统提示与初始用户提示由 build_run 用 meta.objective
    重新构造，不在此重放（避免过期的环境块）。
    """
    history: list[dict[str, Any]] = []
    for rec in read_records(path):
        # 跳过 worker 子历史：主 history 只重放主 agent（coordinator worker 各自独立）
        if rec.get("worker"):
            continue
        rtype = rec.get("type")
        if rtype == "message":
            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                history.append(msg)
        elif rtype == "tool_result":
            call_id = rec.get("call_id")
            content = rec.get("content", "")
            if call_id:
                history.append({"role": "tool", "tool_call_id": call_id, "content": str(content)})
    return history

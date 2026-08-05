"""writeup 骨架生成：solve 结束（无论成败）基于 rollout.jsonl + facts.jsonl 落盘。

素材全部来自落盘记录，不靠模型事后编：目标与时间线（关键工具调用）、
已验证事实、已排除方向、flag（若有）。输出 runs/<id>/writeup.md。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _load_rollout(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读 rollout.jsonl，返回 (session_meta, records)。"""
    if not path.is_file():
        return {}, []
    meta: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "session_meta":
            meta = r
        else:
            records.append(r)
    return meta, records


def _load_facts(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    deadends: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            (facts if r.get("kind") == "fact" else deadends).append(r)
    return facts, deadends


def _load_board(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """M3 起事实板是 SQLite 黑板；返回 (facts, deadends, intents)。

    黑板不存在时回退读 M2 的 facts.jsonl（intents 为空）。"""
    from ..swarm.blackboard import Blackboard

    db = run_dir / "blackboard.db"
    if db.is_file():
        bb = Blackboard(db)
        try:
            facts = [
                {"fact": f["content"], "evidence": f.get("evidence_handle"), "status": f["status"]}
                for f in bb.facts(limit=100)
            ]
            deadends = [{"direction": d["direction"], "reason": d["reason"]} for d in bb.deadends(limit=100)]
            intents = bb.intents(limit=100)
            return facts, deadends, intents
        finally:
            bb.close()
    facts, deadends = _load_facts(run_dir / "facts.jsonl")
    return facts, deadends, []


def _ts(t: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "?"


def generate_writeup(run_dir: str | Path, final_summary: str = "") -> Path | None:
    """生成 writeup.md；rollout 不存在则返回 None。"""
    run_dir = Path(run_dir)
    meta, records = _load_rollout(run_dir / "rollout.jsonl")
    if not meta:
        return None
    facts, deadends, intents = _load_board(run_dir)
    winner: dict[str, Any] | None = None
    wpath = run_dir / "winner.json"
    if wpath.is_file():
        try:
            winner = json.loads(wpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            winner = None

    lines: list[str] = [f"# Writeup：{meta.get('objective', '')}", ""]
    lines.append("## 目标")
    lines.append(f"- run_id：{meta.get('run_id', '')}")
    if meta.get("target"):
        lines.append(f"- 靶机：{meta['target']}")
    if meta.get("flag_format"):
        lines.append(f"- flag 格式：`{meta['flag_format']}`")
    lines.append(f"- 模型：{meta.get('model', '?')}（{meta.get('provider', '?')}）")
    if meta.get("ts"):
        lines.append(f"- 开始：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta['ts']))}")
    lines.append("")

    if final_summary:
        lines.append("## 最终结论")
        lines.append(final_summary.strip())
        lines.append("")

    lines.append("## Flag")
    if winner:
        flags = winner.get("flags") or ([winner["flag"]] if winner.get("flag") else [])
        for f in flags:
            lines.append(f"- `{f}`")
        lines.append(f"- 状态：{'已集齐' if winner.get('solved', True) else '未集齐'}（{winner.get('reason', '')}）")
    else:
        lines.append("- 未拿到 flag")
    lines.append("")

    lines.append("## 已验证事实")
    lines += [f"- {r['fact']}" + (f"（evidence: `{r['evidence']}`）" if r.get("evidence") else "") for r in facts] or ["- （无）"]
    lines.append("")

    lines.append("## 已排除方向")
    lines += [f"- {r['direction']} — {r['reason']}" for r in deadends] or ["- （无）"]
    lines.append("")

    if intents:
        lines.append("## Intents 完成情况")
        for i in intents:
            line = f"- {i['id']} [{i['status']}] {i['goal']}"
            if i.get("claimed_by"):
                line += f"（{i['claimed_by']}）"
            if i.get("result"):
                line += f" ⇒ {i['result'][:100]}"
            lines.append(line)
        lines.append("")

    lines.append("## 时间线（关键工具调用）")
    interesting = {"shell_exec", "http_request", "submit_flag", "submit_answer", "shell_session", "python_eval"}
    n = 0
    for r in records:
        if r.get("type") != "tool_call":
            continue
        for call in r.get("calls", []):
            name = call.get("name", "")
            if name not in interesting and not name.startswith(("nmap", "sqlmap", "ffuf", "gobuster", "hydra", "dirsearch", "nikto")):
                continue
            args = (call.get("arguments") or "").replace("\n", " ")
            if len(args) > 120:
                args = args[:117] + "..."
            n += 1
            lines.append(f"{n}. `{_ts(r.get('ts'))}` **{name}** `{args}`")
    if n == 0:
        lines.append("（无）")
    lines.append("")

    out = run_dir / "writeup.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out

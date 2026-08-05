"""FactsBoard：系统提示注入门面，M3 起由 SQLite 黑板（blackboard.py）承载。

保留 M2 的接口与渲染格式（post_fact/post_deadend/facts/deadends/render），
底层从 facts.jsonl 换成 blackboard.db——每 turn 注入系统提示的行为不变。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .blackboard import Blackboard

RENDER_LIMIT = 15  # 注入系统提示的条数上限


class FactsBoard(Blackboard):
    """黑板 + prompt 渲染门面。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    # ------------------------------------------------------------ 兼容 M2 接口
    def post_fact(self, fact: str, evidence_handle: str | None = None, actor: str = "main") -> dict[str, Any]:  # type: ignore[override]
        eid = super().post_fact(fact, evidence_handle, actor)
        return {"kind": "fact", "fact": fact, "evidence": evidence_handle, "event_id": eid}

    def post_deadend(self, direction: str, reason: str, actor: str = "main", route_hash: str | None = None) -> dict[str, Any]:  # type: ignore[override]
        eid = super().post_deadend(direction, reason, actor, route_hash)
        return {"kind": "deadend", "direction": direction, "reason": reason, "event_id": eid}

    def facts_view(self, last: int = RENDER_LIMIT) -> list[dict[str, Any]]:
        """未 rejected 的事实，键名保持 M2 风格（fact/evidence）。"""
        return [
            {"id": f["id"], "fact": f["content"], "evidence": f.get("evidence_handle"), "status": f["status"]}
            for f in self.facts(last, status="unresolved")
        ]

    def deadends_view(self, last: int = RENDER_LIMIT) -> list[dict[str, Any]]:
        return [{"direction": d["direction"], "reason": d["reason"]} for d in self.deadends(last)]

    def directives_view(self, last: int = RENDER_LIMIT) -> list[dict[str, Any]]:
        return [
            {"content": d["content"], "actor": d["actor"], "priority": d["priority"]}
            for d in self.directives(last)
        ]

    # ------------------------------------------------------------ 渲染
    def render(self) -> str:
        """系统提示注入块：已知事实 / 控制指令 / 已排除方向。"""
        lines = [f"已知事实（最近 {RENDER_LIMIT} 条，完整见 blackboard.db；确认新事实必须 post_fact）："]
        facts = self.facts_view()
        if facts:
            for r in facts:
                ev = f" [evidence={r['evidence']}]" if r.get("evidence") else ""
                lines.append(f"- {r['fact']}{ev}")
        else:
            lines.append("（暂无）")
        lines.append("控制指令（非事实；来自操作员/旁观者/系统，按优先级遵循）：")
        directives = self.directives_view()
        if directives:
            for r in directives:
                lines.append(f"- [{r['priority']}] {r['content']}（actor={r['actor']}）")
        else:
            lines.append("（暂无）")
        lines.append("已排除方向（死路，别再试；排除新方向必须 post_deadend）：")
        deadends = self.deadends_view()
        if deadends:
            for r in deadends:
                lines.append(f"- {r['direction']} — 原因: {r['reason']}")
        else:
            lines.append("（暂无）")
        return "\n".join(lines)

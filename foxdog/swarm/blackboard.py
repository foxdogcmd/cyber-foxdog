"""黑板（DESIGN §6.3）：每 run 一个 SQLite（WAL），事件溯源。

append-only `events` 表是唯一事实源；`facts`/`intents`/`deadends` 是物化视图，
`rebuild_views()` 可全量重放重建。写入路径：业务方法先改视图（带原子守护），
再追加事件（事件的 _apply 幂等重放同一变更，保持单一代码路径）。

- Facts：content/evidence_handle/actor + 状态机（M3 只用 unresolved/rejected）。
- Intents：id 为 `I<n>-<goal sha1 前 6>`（防 muteki 的 I1 撞车）；route_hash 由
  创建者给 category:surface:technique 三要素算出；lane 可空（独占资源 key）。
  认领原子化：`UPDATE ... WHERE status='open'` + rowcount 守护（WON/LOST）。
  去重三件套：route 折叠（同 route 有 open/claimed 拒建）、近重复 goal
  （difflib ≥0.9 拒）、barren 二次去重（同 route 有 barren 结论拒）。
- Dead-ends：direction/reason/actor + route_hash（可空）。

线程安全：单连接 + threading.RLock，全部操作持锁（量小，同步即可）。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

NEAR_DUP_RATIO = 0.9  # 近重复 goal 相似度阈值
SUMMARY_LIMIT = 15  # 摘要默认条数

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    evidence_handle TEXT,
    actor TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unresolved',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    route_hash TEXT NOT NULL,
    lane TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by TEXT,
    result TEXT,
    actor TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deadends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    route_hash TEXT,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS directives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    actor TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    ts REAL NOT NULL
);
"""


def route_hash_of(category: str, surface: str, technique: str) -> str:
    """route = category:surface:technique 的哈希（同方向折叠用）。"""
    raw = f"{category.strip().lower()}:{surface.strip().lower()}:{technique.strip().lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class Blackboard:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 事件溯源内核
    def _apply(self, kind: str, payload: dict[str, Any]) -> None:
        """把事件应用到物化视图（live 写入与 rebuild 重放共用，必须幂等）。"""
        cur = self._conn
        if kind == "fact_posted":
            cur.execute(
                "INSERT INTO facts(content, evidence_handle, actor, status, ts) VALUES (?,?,?,?,?)",
                (payload["content"], payload.get("evidence_handle"), payload["actor"], "unresolved", payload["ts"]),
            )
        elif kind == "fact_rejected":
            cur.execute("UPDATE facts SET status='rejected' WHERE id=?", (payload["fact_id"],))
        elif kind == "intent_created":
            cur.execute(
                "INSERT OR IGNORE INTO intents(id, goal, route_hash, lane, status, actor, ts) VALUES (?,?,?,?,'open',?,?)",
                (payload["id"], payload["goal"], payload["route_hash"], payload.get("lane"),
                 payload["actor"], payload["ts"]),
            )
        elif kind == "intent_claimed":
            cur.execute(
                "UPDATE intents SET status='claimed', claimed_by=? WHERE id=?",
                (payload["worker_id"], payload["id"]),
            )
        elif kind == "intent_completed":
            cur.execute(
                "UPDATE intents SET status=?, result=? WHERE id=?",
                (payload["status"], payload.get("result"), payload["id"]),
            )
        elif kind == "deadend_posted":
            cur.execute(
                "INSERT INTO deadends(direction, reason, actor, route_hash, ts) VALUES (?,?,?,?,?)",
                (payload["direction"], payload["reason"], payload["actor"], payload.get("route_hash"), payload["ts"]),
            )
        elif kind == "directive_posted":
            cur.execute(
                "INSERT INTO directives(content, actor, priority, ts) VALUES (?,?,?,?)",
                (payload["content"], payload["actor"], payload.get("priority", "normal"), payload["ts"]),
            )
        else:
            raise ValueError(f"未知事件类型: {kind}")

    def _commit(self, kind: str, payload: dict[str, Any], actor: str) -> int:
        """业务变更已生效（或幂等重放）后：_apply 改视图 + 追加事件，同源同 ts。"""
        full = {"actor": actor, "ts": time.time(), **payload}
        self._apply(kind, full)
        cur = self._conn.execute(
            "INSERT INTO events(ts, actor, kind, payload) VALUES (?,?,?,?)",
            (full["ts"], actor, kind, json.dumps(full, ensure_ascii=False)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def rebuild_views(self) -> None:
        """清空物化视图，从 events 全量重放重建（可恢复性证明）。

        AUTOINCREMENT 序列一并重置，保证重放出的行 id 与 live 写入完全一致。
        """
        with self._lock:
            self._conn.execute("DELETE FROM facts")
            self._conn.execute("DELETE FROM intents")
            self._conn.execute("DELETE FROM deadends")
            self._conn.execute("DELETE FROM directives")
            self._conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('facts','intents','deadends','directives')"
            )
            rows = self._conn.execute("SELECT kind, payload FROM events ORDER BY id").fetchall()
            for row in rows:
                self._apply(row["kind"], json.loads(row["payload"]))
            self._conn.commit()

    # ------------------------------------------------------------ Facts
    def post_fact(self, content: str, evidence_handle: str | None = None, actor: str = "main") -> int:
        with self._lock:
            return self._commit("fact_posted", {"content": content, "evidence_handle": evidence_handle}, actor)

    def reject_fact(self, fact_id: int, actor: str = "planner") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET status='rejected' WHERE id=? AND status='unresolved'", (fact_id,)
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._commit("fact_rejected", {"fact_id": fact_id}, actor)  # _apply 幂等
            return True

    def facts(self, limit: int = SUMMARY_LIMIT, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sql = "SELECT * FROM facts"
            args: tuple = ()
            if status:
                sql += " WHERE status=?"
                args = (status,)
            rows = self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------ Dead-ends
    def post_deadend(
        self, direction: str, reason: str, actor: str = "main", route_hash: str | None = None
    ) -> int:
        with self._lock:
            return self._commit(
                "deadend_posted",
                {"direction": direction, "reason": reason, "route_hash": route_hash},
                actor,
            )

    def deadends(self, limit: int = SUMMARY_LIMIT) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deadends ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------ Directives
    def post_directive(self, content: str, actor: str = "system", priority: str = "normal") -> int:
        """Post non-factual control guidance for planner/worker context."""
        with self._lock:
            return self._commit(
                "directive_posted",
                {"content": content, "priority": priority},
                actor,
            )

    def directives(self, limit: int = SUMMARY_LIMIT) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM directives ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------ Intents
    def _next_intent_seq(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='intent_created'").fetchone()
        return int(row["n"]) + 1

    def create_intent(
        self,
        goal: str,
        category: str,
        surface: str,
        technique: str,
        lane: str | None = None,
        actor: str = "planner",
    ) -> tuple[bool, str]:
        """新建 intent；去重三件套拦截时返回 (False, 原因)。"""
        rh = route_hash_of(category, surface, technique)
        with self._lock:
            # barren 二次去重：同 route 已被判贫瘠的死方向不再派
            row = self._conn.execute(
                "SELECT id FROM intents WHERE route_hash=? AND status='barren'", (rh,)
            ).fetchone()
            if row:
                return False, f"route 已有 barren 结论（{row['id']}），死方向不再派发"
            # route 折叠：同 route 有在跑/待领的 intent
            rows = self._conn.execute(
                "SELECT id, goal FROM intents WHERE route_hash=? AND status IN ('open','claimed')", (rh,)
            ).fetchall()
            if rows:
                return False, f"route 折叠：同方向已有 {rows[0]['id']}（{rows[0]['goal'][:40]}）"
            # 近重复 goal 去重
            active = self._conn.execute(
                "SELECT id, goal FROM intents WHERE status IN ('open','claimed')"
            ).fetchall()
            for r in active:
                ratio = difflib.SequenceMatcher(None, goal, r["goal"]).ratio()
                if ratio >= NEAR_DUP_RATIO:
                    return False, f"近重复去重：与 {r['id']} 相似度 {ratio:.2f}"
            n = self._next_intent_seq()
            iid = f"I{n}-{hashlib.sha1(goal.encode()).hexdigest()[:6]}"
            payload = {"id": iid, "goal": goal, "route_hash": rh, "lane": lane}
            self._commit("intent_created", payload, actor)
            return True, iid

    def claim_intent(self, worker_id: str) -> dict[str, Any] | None:
        """原子认领最老 open intent：WON 返回 intent dict，LOST 返回 None。

        幂等：该 worker 已有 claimed 未收尾的 intent 时直接返回它——
        coordinator 是代 worker 认领后再派发，worker 自己再调 claim 不该 LOST
        （run 20260802 swarm 实战：worker-4 被自己已认领的 intent 搞糊涂）。"""
        with self._lock:
            mine = self._conn.execute(
                "SELECT id FROM intents WHERE status='claimed' AND claimed_by=? ORDER BY ts, id LIMIT 1",
                (worker_id,),
            ).fetchone()
            if mine is not None:
                return self.get_intent(mine["id"])
            row = self._conn.execute(
                "SELECT id FROM intents WHERE status='open' ORDER BY ts, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            iid = row["id"]
            cur = self._conn.execute(
                "UPDATE intents SET status='claimed', claimed_by=? WHERE id=? AND status='open'",
                (worker_id, iid),
            )
            if cur.rowcount != 1:  # LOST：被别的 worker 抢先
                self._conn.rollback()
                return None
            self._commit("intent_claimed", {"id": iid, "worker_id": worker_id}, worker_id)
            return self.get_intent(iid)

    def complete_intent(self, intent_id: str, status: str, result: str, actor: str = "worker") -> bool:
        """收尾 intent（status: done|barren）。"""
        if status not in ("done", "barren"):
            raise ValueError(f"非法 intent 状态: {status}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE intents SET status=?, result=? WHERE id=? AND status IN ('open','claimed')",
                (status, result, intent_id),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._commit("intent_completed", {"id": intent_id, "status": status, "result": result}, actor)
            return True

    def finish_claimed(self, intent_id: str, worker_id: str, status: str, result: str) -> bool:
        """worker 结束时兜底：只收尾仍被自己 claimed 的 intent（不覆盖 agent 已报的）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE intents SET status=?, result=? WHERE id=? AND status='claimed' AND claimed_by=?",
                (status, result, intent_id, worker_id),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._commit("intent_completed", {"id": intent_id, "status": status, "result": result}, worker_id)
            return True

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
            return dict(row) if row else None

    def intents(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM intents ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def open_intents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM intents WHERE status='open' ORDER BY ts, id"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------ 摘要
    def version(self) -> int:
        """事件总数（协调循环据此判断"黑板有变化"）。"""
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])

    def summary(self, limit: int = SUMMARY_LIMIT) -> str:
        """planner / worker prompt 用的黑板摘要。"""
        lines = ["## 黑板摘要"]
        lines.append("### 已验证事实")
        facts = self.facts(limit)
        lines += [
            f"- #{f['id']} [{f['status']}] {f['content']}"
            + (f"（evidence: {f['evidence_handle']}）" if f.get("evidence_handle") else "")
            for f in facts
        ] or ["（无）"]
        lines.append("### 已排除方向（死路）")
        lines += [f"- {d['direction']} — {d['reason']}" for d in self.deadends(limit)] or ["（无）"]
        lines.append("### 控制指令（非事实，按优先级遵循）")
        dirs = self.directives(limit)
        lines += [
            f"- #{d['id']} [{d['priority']}] {d['content']}（actor={d['actor']}）"
            for d in dirs
        ] or ["（无）"]
        lines.append("### Intents")
        ints = self.intents(limit)
        lines += [
            f"- {i['id']} [{i['status']}] {i['goal']}"
            + (f"（{i['claimed_by']}）" if i.get("claimed_by") else "")
            + (f" ⇒ {i['result'][:80]}" if i.get("result") else "")
            for i in ints
        ] or ["（无）"]
        return "\n".join(lines)

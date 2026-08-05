"""run 目录与内容寻址存储（CAS）。

目录布局（默认 base 为当前目录）：
    <base>/.foxdog-runs/<run-id>/
        rollout.jsonl   会话记录（store/rollout.py）
        winner.json     FlagGate 验证通过后写入
        inputs/         题目附件（--attach）
        work/           agent 默认工作区（相对文件路径从这里解析）
        cas/<sha256[:2]>/<sha256>   内容寻址存储（工具输出全文归档）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path


class Workspace:
    def __init__(
        self,
        base: str | os.PathLike[str] | None = None,
        run_id: str | None = None,
        seed_from: str | os.PathLike[str] | None = None,
    ) -> None:
        self.base = Path(base) if base else Path.cwd()
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.dir = self.base / ".foxdog-runs" / self.run_id
        self.cas_dir = self.dir / "cas"
        self.inputs_dir = self.dir / "inputs"
        self.work_dir = self.dir / "work"
        for p in (self.cas_dir, self.inputs_dir, self.work_dir):
            p.mkdir(parents=True, exist_ok=True)
        if seed_from is not None:
            self.seed_from(seed_from)

    def seed_from(self, source: str | os.PathLike[str]) -> None:
        """Copy resumable run state into this fresh run directory."""
        src = Path(source)
        if not src.is_dir():
            raise FileNotFoundError(f"resume source run not found: {src}")
        for name in ("inputs", "work", "cas"):
            source_dir = src / name
            target_dir = self.dir / name
            if source_dir.is_dir():
                shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        legacy_facts = src / "facts.jsonl"
        if legacy_facts.is_file():
            shutil.copy2(legacy_facts, self.dir / "facts.jsonl")
        self._seed_sqlite_db(src / "blackboard.db", self.dir / "blackboard.db")

    @staticmethod
    def _seed_sqlite_db(source: Path, target: Path) -> None:
        if not source.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(str(source))
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
            target_conn.commit()
        finally:
            target_conn.close()
            source_conn.close()

    # ------------------------------------------------------------ CAS
    def cas_store(self, data: bytes) -> str:
        """写入 CAS，返回句柄（sha256 hex）。"""
        h = hashlib.sha256(data).hexdigest()
        bucket = self.cas_dir / h[:2]
        bucket.mkdir(parents=True, exist_ok=True)
        path = bucket / h
        if not path.exists():
            path.write_bytes(data)
        return h

    def cas_path(self, handle: str) -> Path:
        return self.cas_dir / handle[:2] / handle

    def cas_exists(self, handle: str) -> bool:
        return self.cas_path(handle).is_file()

    def cas_fetch(self, handle: str, offset: int = 0, limit: int | None = None) -> bytes:
        data = self.cas_path(handle).read_bytes()
        if offset:
            data = data[offset:]
        if limit is not None:
            data = data[:limit]
        return data

    def cas_fetch_text(self, handle: str, offset: int = 0, limit: int | None = None) -> str:
        return self.cas_fetch(handle, offset, limit).decode("utf-8", errors="replace")

    def cas_files(self) -> list[Path]:
        return [p for p in self.cas_dir.rglob("*") if p.is_file()]

    # ------------------------------------------------------------ 路径约束
    def resolve(self, path: str) -> Path:
        """把模型给的路径解析到 work_dir 内，越界抛 PermissionError。"""
        p = Path(path)
        if not p.is_absolute():
            p = self.work_dir / p
        p = p.resolve()
        root = self.work_dir.resolve()
        if p != root and root not in p.parents:
            raise PermissionError(f"路径越出工作区: {path}")
        return p

    def resolve_file_path(self, path: str) -> Path:
        """解析通用文件工具路径；相对路径仍以 run 工作区为基准。"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.work_dir / p
        return p.resolve()

    # ------------------------------------------------------------ 结果
    @property
    def winner_path(self) -> Path:
        return self.dir / "winner.json"

    def write_winner(self, flag: str, reason: str = "", flags: list[str] | None = None, solved: bool = True) -> None:
        self.winner_path.write_text(
            json.dumps(
                {
                    "flag": flag,  # 最后一面（兼容旧读取方）
                    "flags": flags if flags is not None else [flag],
                    "solved": solved,
                    "reason": reason,
                    "run_id": self.run_id,
                    "ts": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def read_winner(self) -> dict | None:
        if self.winner_path.is_file():
            return json.loads(self.winner_path.read_text(encoding="utf-8"))
        return None

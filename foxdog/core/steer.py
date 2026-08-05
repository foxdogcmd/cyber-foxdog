"""Steer 中途指挥：线程安全队列（TUI 输入框 / headless stdin 线程 → agent 上下文）。

put(text) 由任意线程调用（stdin reader 线程、Textual UI 线程）；
drain() 由 agent 侧（GoalMode 每 turn 开始 / Coordinator 每拍）调用取走全部。
"""
from __future__ import annotations

import sys
import threading


class SteerQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[str] = []

    def put(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._items.append(text)

    def drain(self) -> list[str]:
        """取走并清空全部排队指令（保持顺序）。"""
        with self._lock:
            items = self._items
            self._items = []
        return items

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._items)


def start_stdin_thread(steer: SteerQueue) -> threading.Thread | None:
    """headless steer：后台线程逐行读 stdin 进队列（Windows 兼容，不阻塞主循环）。

    非 tty（管道/重定向）时跳过——外部编排不该把 stdin 数据误当指挥。
    返回 daemon 线程（进程退出随灭）；非 tty 返回 None。
    """
    try:
        if not sys.stdin.isatty():
            return None
    except (AttributeError, ValueError):
        return None

    def _reader() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:  # EOF
                return
            steer.put(line)

    t = threading.Thread(target=_reader, name="foxdog-steer-stdin", daemon=True)
    t.start()
    return t

"""后台任务 registry：task_id → asyncio.Task + ring buffer 输出。

供 shell_exec 自动分流、Ctrl+B 中途 detach、完成结果一次性通知 agent，以及
task_list / task_output / task_stop 工具使用（借鉴 LeoAI CommandTools 语义）。
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .process import ExecResult

Runner = Callable[[Callable[[str], None]], Awaitable[ExecResult]]

RING_LIMIT = 64 * 1024  # ring buffer 字符上限


@dataclass
class BackgroundTask:
    task_id: str
    command: str
    started_at: float = field(default_factory=time.time)
    buffer: deque[str] = field(default_factory=deque)
    buffered_chars: int = 0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: ExecResult | None = None
    cancelled: bool = False
    detached: bool = False
    foreground_waiting: bool = False
    notification_sent: bool = False
    detach_requested: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None

    def append_output(self, text: str) -> None:
        self.buffer.append(text)
        self.buffered_chars += len(text)
        while self.buffered_chars > RING_LIMIT and self.buffer:
            self.buffered_chars -= len(self.buffer.popleft())

    @property
    def running(self) -> bool:
        return not self.done.is_set()

    def tail(self, limit: int = 4000) -> str:
        text = "".join(self.buffer)
        return text[-limit:] if len(text) > limit else text


class BackgroundRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._counter = itertools.count(1)

    def start(self, command: str, runner: Runner) -> BackgroundTask:
        tid = f"task-{next(self._counter)}"
        bt = BackgroundTask(task_id=tid, command=command)

        async def _wrap() -> None:
            try:
                bt.result = await runner(bt.append_output)
            except asyncio.CancelledError:
                bt.cancelled = True
            except Exception as exc:  # noqa: BLE001 - 兜底记录
                bt.result = ExecResult(exit_code=-1, output="", error=f"{type(exc).__name__}: {exc}")
            finally:
                bt.done.set()

        bt._task = asyncio.create_task(_wrap())
        self._tasks[tid] = bt
        return bt

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def detach_latest(self) -> BackgroundTask | None:
        """Detach the newest shell command currently blocking a foreground tool call."""
        waiting = [task for task in self._tasks.values() if task.running and task.foreground_waiting]
        if not waiting:
            return None
        task = max(waiting, key=lambda item: item.started_at)
        task.detached = True
        task.detach_requested.set()
        return task

    def drain_completed(self) -> list[BackgroundTask]:
        """Claim newly completed detached tasks exactly once for agent notification."""
        completed: list[BackgroundTask] = []
        for task in self._tasks.values():
            if task.detached and not task.running and not task.notification_sent:
                task.notification_sent = True
                completed.append(task)
        return completed

    def has_running_detached(self) -> bool:
        return any(task.detached and task.running for task in self._tasks.values())

    def has_completed_detached(self) -> bool:
        return any(
            task.detached and not task.running and not task.notification_sent
            for task in self._tasks.values()
        )

    def list(self) -> list[dict]:
        return [
            {
                "task_id": t.task_id,
                "command": t.command,
                "running": t.running,
                "detached": t.detached,
                "cancelled": t.cancelled,
                "exit_code": t.result.exit_code if t.result else None,
                "elapsed_s": round(time.time() - t.started_at, 1),
            }
            for t in self._tasks.values()
        ]

    def output(self, task_id: str, tail: int = 4000) -> str | None:
        t = self._tasks.get(task_id)
        if t is None:
            return None
        text = t.tail(tail)
        status = "running" if t.running else ("cancelled" if t.cancelled else f"exit={t.result.exit_code if t.result else '?'}")
        return f"[{t.task_id} {status}]\n{text}"

    async def stop(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        if t is None or t._task is None:
            return False
        if t.running:
            t._task.cancel()
            try:
                await asyncio.wait_for(t.done.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        return True

    async def stop_all(self) -> None:
        for t in list(self._tasks.values()):
            await self.stop(t.task_id)

"""调度器：声明冲突的工具串行、不冲突的并行（耗时断言）。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.core.scheduler import plan_batches, run_tool_calls
from foxdog.executor.background import BackgroundRegistry
from foxdog.llm.messages import ToolCall
from foxdog.store.workspace import Workspace
from foxdog.tools import Tool, ToolAccess, ToolContext, ToolRegistry, ToolResult

DELAY = 0.4


class SleepTool(Tool):
    def __init__(self, name: str, accesses: list[ToolAccess]) -> None:
        self.name = name
        self.description = "sleep"
        self.accesses = accesses

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(DELAY)
        return ToolResult(output=f"slept {self.name}")


def _make_harness(tmp_path, tools: list[Tool]):
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    ws = Workspace(base=tmp_path, run_id="sched")
    ctx = ToolContext(ws, BackgroundRegistry(), emit=lambda e: None, fetch_output=lambda h, o, l: "")
    orch = ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx)
    return registry, orch


def _calls(registry, names):
    return [
        (ToolCall(id=f"c{i}", name=n, arguments="{}"), registry.get(n))
        for i, n in enumerate(names)
    ]


async def test_conflicting_tools_run_serial(tmp_path):
    """两个都写 shell 资源的工具必须串行：总耗时 ≈ 2×DELAY。"""
    tools = [SleepTool("a", [ToolAccess(resource="shell", mode="w")]),
             SleepTool("b", [ToolAccess(resource="shell", mode="w")])]
    registry, orch = _make_harness(tmp_path, tools)
    items = _calls(registry, ["a", "b"])
    assert len(plan_batches(items)) == 2

    start = time.monotonic()
    results = await run_tool_calls(items, orch)
    elapsed = time.monotonic() - start
    assert elapsed >= DELAY * 1.9, elapsed
    assert [r.output for r in results] == ["slept a", "slept b"]  # 顺序回填


async def test_independent_tools_run_parallel(tmp_path):
    """读写不同资源的工具并行：总耗时 ≈ 1×DELAY。"""
    tools = [SleepTool("a", [ToolAccess(resource="shell", mode="w")]),
             SleepTool("b", [ToolAccess(resource="network", mode="w")]),
             SleepTool("c", [ToolAccess(resource="workspace", mode="r")])]
    registry, orch = _make_harness(tmp_path, tools)
    items = _calls(registry, ["a", "b", "c"])
    assert len(plan_batches(items)) == 1

    start = time.monotonic()
    results = await run_tool_calls(items, orch)
    elapsed = time.monotonic() - start
    assert elapsed < DELAY * 1.9, elapsed
    assert [r.output for r in results] == ["slept a", "slept b", "slept c"]


async def test_read_write_conflict(tmp_path):
    """读与写同一资源也冲突（写-读冲突）。"""
    tools = [SleepTool("w", [ToolAccess(resource="workspace", mode="w")]),
             SleepTool("r", [ToolAccess(resource="workspace", mode="r")])]
    registry, orch = _make_harness(tmp_path, tools)
    items = _calls(registry, ["w", "r"])
    assert len(plan_batches(items)) == 2
    start = time.monotonic()
    await run_tool_calls(items, orch)
    assert time.monotonic() - start >= DELAY * 1.9

"""工具并发调度（借鉴 kimi-code tool-scheduler + codex parallel.rs）。

同一 step 内多个 tool call：accesses 无读写冲突的 gather 并行，
有冲突的分到后续批次串行；结果按 provider 原始顺序回填。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..llm.messages import ToolCall
from ..tools import Tool, ToolAccess, ToolResult

if TYPE_CHECKING:
    from .orchestrator import ToolOrchestrator


def conflicts(a: list[ToolAccess], b: list[ToolAccess]) -> bool:
    """同名资源上至少一方写即冲突。"""
    for x in a:
        for y in b:
            if x.resource == y.resource and (x.mode == "w" or y.mode == "w"):
                return True
    return False


def plan_batches(items: list[tuple[ToolCall, Tool | None]]) -> list[list[int]]:
    """贪心分批：每个 call 放进第一个不冲突的批次，放不下开新批次。"""
    batches: list[list[int]] = []
    for i, (_, tool) in enumerate(items):
        accesses = tool.accesses if tool else []
        for batch in batches:
            if all(
                not conflicts(accesses, (items[j][1].accesses if items[j][1] else []))
                for j in batch
            ):
                batch.append(i)
                break
        else:
            batches.append([i])
    return batches


async def run_tool_calls(
    items: list[tuple[ToolCall, Tool | None]],
    orchestrator: "ToolOrchestrator",
) -> list[ToolResult]:
    """分批执行，批内并行，批间串行；结果按原始顺序返回。"""
    results: list[ToolResult | None] = [None] * len(items)
    for batch in plan_batches(items):
        batch_results = await asyncio.gather(*(orchestrator.execute(items[i][0], items[i][1]) for i in batch))
        for i, res in zip(batch, batch_results):
            results[i] = res
    return [r for r in results if r is not None]

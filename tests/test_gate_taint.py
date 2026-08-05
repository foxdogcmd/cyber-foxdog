"""Orchestrator 层 FlagGate 污染扫描：工具输入进污染集，submit_flag 参数豁免。"""
from __future__ import annotations

import pytest

from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.executor.background import BackgroundRegistry
from foxdog.gate.flag_gate import FlagGate
from foxdog.llm.messages import ToolCall
from foxdog.store.workspace import Workspace
from foxdog.tools import Tool, ToolContext, ToolRegistry, ToolResult


class _EchoTool(Tool):
    name = "fake_shell"
    description = "stub"
    accesses = []

    async def run(self, args, ctx) -> ToolResult:
        return ToolResult(ok=True, output=f"ran: {args.get('command', '')}")


class _SubmitTool(Tool):
    name = "submit_flag"
    description = "stub"

    async def run(self, args, ctx) -> ToolResult:
        return ToolResult(ok=True, output="submitted")


def _make(tmp_path):
    ws = Workspace(base=tmp_path, run_id="taint-test")
    gate = FlagGate(ws, flag_format=r"flag\{.*?\}")
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
        flag_gate=gate,
    )
    registry = ToolRegistry()
    orch = ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx)
    return gate, registry, orch


@pytest.mark.asyncio()
async def test_tool_args_tainted(tmp_path):
    gate, registry, orch = _make(tmp_path)
    registry.register(_EchoTool())
    call = ToolCall(id="1", name="fake_shell", arguments='{"command": "echo flag{self_123456}"}')
    await orch.execute(call, _EchoTool())
    assert "flag{self_123456}" in gate._tainted


@pytest.mark.asyncio()
async def test_submit_flag_args_exempt(tmp_path):
    gate, registry, orch = _make(tmp_path)
    registry.register(_SubmitTool())
    call = ToolCall(id="2", name="submit_flag", arguments='{"flag": "flag{legit_999}"}')
    await orch.execute(call, _SubmitTool())
    # submit_flag 自身参数不进污染集，否则合法提交自锁
    assert "flag{legit_999}" not in gate._tainted

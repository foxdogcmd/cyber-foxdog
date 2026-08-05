"""MCP client：stdio server 连接、工具列举、select_tools 激活、调用回传；坏 server 不致命。"""
from __future__ import annotations

import sys
from pathlib import Path

from foxdog.config import McpServerConfig
from foxdog.executor.background import BackgroundRegistry
from foxdog.mcp_client import McpManager
from foxdog.recipes.loader import RecipeManager
from foxdog.recipes.tool import SelectToolsTool
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry

# 最小 stdio MCP server（mcp 2.x 的 MCPServer，即 FastMCP 继任 API）
SERVER_SRC = '''\
import asyncio
from mcp.server import MCPServer

app = MCPServer("foxdog-test")

@app.tool(description="回声测试")
def echo(text: str) -> str:
    return "echo:" + text

@app.tool(description="加法")
def add(a: int, b: int) -> int:
    return a + b

asyncio.run(app.run_stdio_async())
'''


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(
        workspace=Workspace(base=tmp_path, run_id="mcp-test"),
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )


async def test_mcp_connect_activate_call(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text(SERVER_SRC, encoding="utf-8")
    manager = McpManager()
    results = await manager.connect_all(
        {
            "t": McpServerConfig(command=sys.executable, args=[str(server)]),
            "bad": McpServerConfig(command="definitely-not-exist-foxdog-xyz"),
        }
    )
    try:
        # 坏 server 不致命，好 server 照常
        assert results["t"] is None
        assert results["bad"] is not None
        assert "mcp__t__echo" in manager.names and "mcp__t__add" in manager.names

        # 激活进 registry
        registry = ToolRegistry()
        activated, unknown = manager.activate(["mcp__t__echo", "mcp__ghost__x"], registry)
        assert activated == ["mcp__t__echo"] and unknown == ["mcp__ghost__x"]
        tool = registry.get("mcp__t__echo")
        assert tool is not None
        # schema 直接来自 MCP inputSchema
        schema = tool.openai_schema()["function"]["parameters"]
        assert "text" in schema["properties"]

        # 调用回传
        ctx = _ctx(tmp_path)
        res = await tool.run({"text": "hello-mcp"}, ctx)
        assert res.ok and "echo:hello-mcp" in res.output
    finally:
        await manager.close()
    ctx.facts.close()


async def test_mcp_via_select_tools(tmp_path):
    """select_tools 聚合：目录含 MCP 工具，names 激活 MCP 工具。"""
    server = tmp_path / "mcp_server.py"
    server.write_text(SERVER_SRC, encoding="utf-8")
    manager = McpManager()
    results = await manager.connect_all({"t": McpServerConfig(command=sys.executable, args=[str(server)])})
    assert results["t"] is None
    try:
        registry = ToolRegistry()
        select = SelectToolsTool(RecipeManager([], skipped=[]), registry, manager)
        ctx = _ctx(tmp_path)

        catalog = await select.run({}, ctx)
        assert "mcp__t__add" in catalog.output and "MCP 工具" in catalog.output

        act = await select.run({"names": ["mcp__t__add"]}, ctx)
        assert act.ok and "mcp__t__add" in act.output
        tool = registry.get("mcp__t__add")
        assert tool is not None
        res = await tool.run({"a": 19, "b": 23}, ctx)
        assert res.ok and "42" in res.output
    finally:
        await manager.close()
    ctx.facts.close()

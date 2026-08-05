"""MCP client（DESIGN §4.4 裁剪版）：把外部 MCP server 的工具接进 foxdog。

- config.toml `[mcp_servers.<name>]`：stdio（command/args/env）或 streamable HTTP（url）。
- 启动并发连接全部 server，单 server 失败不致命（记 warning，其余继续）。
- 渐进披露：MCP 工具不进默认工具表，走 select_tools 同款——目录里列
  `mcp__<server>__<tool>`（防撞名前缀）+ 描述，select_tools(names=[...]) 激活进 registry。
- 调用代理到对应 server 的 call_tool；text content 拼接，结构化内容 json.dumps，
  输出超阈值走 CAS spill。
- 懒加载：runner 只在配置了 mcp_servers 时 import 本模块（未配置零开销）。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

from .config import McpServerConfig
from .core.context import spill_text
from .tools import Tool, ToolAccess, ToolContext, ToolRegistry, ToolResult

SPILL_THRESHOLD = 32 * 1024


class McpTool(Tool):
    """一个已激活的 MCP 工具（代理调用到对应 server）。"""

    accesses = [ToolAccess(resource="mcp", mode="w")]

    def __init__(self, manager: "McpManager", prefixed_name: str, description: str, input_schema: dict) -> None:
        self.manager = manager
        self.name = prefixed_name
        self.description = description or f"MCP 工具 {prefixed_name}"
        self._schema = input_schema or {"type": "object", "properties": {}}

    def parameters(self) -> dict[str, Any]:
        return self._schema  # MCP inputSchema 直接作为 JSON schema

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await self.manager.call(self.name, args, ctx)


class McpManager:
    def __init__(self) -> None:
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, Any] = {}  # server name → ClientSession
        #: 带前缀工具名 → (server, 原始工具名, description, inputSchema)
        self._tools: dict[str, tuple[str, str, str, dict]] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def has(self, prefixed_name: str) -> bool:
        return prefixed_name in self._tools

    # ------------------------------------------------------------ 连接
    async def connect_all(self, configs: dict[str, McpServerConfig]) -> dict[str, str | None]:
        """并发连接（单 server 30s 超时防挂死）；返回 {server: None|warning}。"""
        async def one(name: str, cfg: McpServerConfig) -> tuple[str, str | None]:
            try:
                await asyncio.wait_for(self._connect_one(name, cfg), timeout=30)
                return name, None
            except Exception as exc:  # noqa: BLE001 - 单 server 失败不致命
                return name, f"{type(exc).__name__}: {exc}"

        results = await asyncio.gather(*(one(n, c) for n, c in configs.items()))
        return dict(results)

    async def _connect_one(self, name: str, cfg: McpServerConfig) -> None:
        from mcp import ClientSession  # 懒加载（模块被 import 时才装 mcp 依赖路径）

        stack = AsyncExitStack()
        try:
            if cfg.command:
                from mcp.client.stdio import StdioServerParameters, stdio_client

                import os

                env = dict(os.environ)
                env.update(cfg.env or {})
                params = StdioServerParameters(command=cfg.command, args=cfg.args, env=env)
                read, write = await stack.enter_async_context(stdio_client(params))
            elif cfg.url:
                from mcp.client.streamable_http import streamable_http_client

                read, write, _ = await stack.enter_async_context(streamable_http_client(cfg.url))
            else:
                raise ValueError("mcp_servers 配置缺 command（stdio）或 url（HTTP）")
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = await session.list_tools()
        except Exception:
            await stack.aclose()
            raise
        self._stacks[name] = stack
        self._sessions[name] = session
        for t in tools.tools:
            prefixed = f"mcp__{name}__{t.name}"
            # mcp 1.x 用别名 inputSchema，2.x 是 input_schema；都兜住
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
            self._tools[prefixed] = (name, t.name, t.description or "", dict(schema))

    async def close(self) -> None:
        for stack in self._stacks.values():
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - 关闭尽力而为
                pass
        self._stacks.clear()
        self._sessions.clear()

    # ------------------------------------------------------------ 渐进披露
    def catalog_entries(self) -> list[tuple[str, str]]:
        """select_tools 目录条目：(带前缀名, 短描述)。"""
        return [(n, self._tools[n][2]) for n in self.names]

    def activate(self, names: list[str], registry: ToolRegistry) -> tuple[list[str], list[str]]:
        """把 MCP 工具激活进 registry；返回 (激活的, 未知的)。"""
        activated: list[str] = []
        unknown: list[str] = []
        for n in names:
            entry = self._tools.get(n)
            if entry is None:
                unknown.append(n)
                continue
            _, _, desc, schema = entry
            registry.register(McpTool(self, n, desc, schema))
            activated.append(n)
        return activated, unknown

    # ------------------------------------------------------------ 调用
    async def call(self, prefixed_name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        entry = self._tools.get(prefixed_name)
        if entry is None:
            return ToolResult(ok=False, error=f"未知 MCP 工具 {prefixed_name!r}")
        server, tool_name, _, _ = entry
        session = self._sessions.get(server)
        if session is None:
            return ToolResult(ok=False, error=f"MCP server {server} 未连接")
        try:
            result = await session.call_tool(tool_name, args)
        except Exception as exc:  # noqa: BLE001 - MCP 调用异常转 ToolResult
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        parts: list[str] = []
        for c in getattr(result, "content", None) or []:
            if getattr(c, "type", None) == "text":
                parts.append(c.text)
            else:
                parts.append(json.dumps(c.model_dump(), ensure_ascii=False, default=str))
        # mcp 1.x structuredContent / 2.x structured_content；isError / is_error 同理
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if structured:
            parts.append(json.dumps(structured, ensure_ascii=False, default=str))
        text = "\n".join(parts) or "(no output)"
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            return ToolResult(ok=False, error=text)
        view, handle, truncated = spill_text(ctx.workspace, text, SPILL_THRESHOLD)
        return ToolResult(output=view, handle=handle, data={"truncated": truncated})

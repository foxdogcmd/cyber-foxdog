"""ToolOrchestrator：权限判定 → 执行 → 异常转 ToolResult 的统一管线。

- 审批结果缓存（同一 tool+args 不重复询问；M1 yolo 下不会触发 ASK）。
- 每个工具输出都归档进 CAS——FlagGate 溯源校验依赖这份落盘证据。
- 发 ToolCallBegin / ToolCallEnd 事件。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ..llm.messages import ToolCall
from ..protocol.types import ToolCallBegin, ToolCallEnd
from ..tools import Tool, ToolContext, ToolRegistry, ToolResult
from .permission import Decision, PermissionChain


_SENSITIVE_KEYS = {"api_key", "token", "secret", "password", "authorization"}
_CONFIG_TOOLS = {
    "set_model", "save_model_profile", "configure_claude_code",
    "add_mcp_server", "set_runtime", "set_platform", "set_agent",
}

def _event_arguments(name: str, arguments: str) -> str:
    """Redact secrets from config tool UI/event payloads without altering execution."""
    if name not in _CONFIG_TOOLS or not arguments:
        return arguments
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    redacted = False
    for key in list(parsed.keys()):
        if key.lower() in _SENSITIVE_KEYS and parsed[key]:
            parsed[key] = "***"
            redacted = True
    return json.dumps(parsed, ensure_ascii=False) if redacted else arguments


class ToolOrchestrator:
    def __init__(
        self,
        registry: ToolRegistry,
        permission: PermissionChain,
        ctx: ToolContext,
    ) -> None:
        self.registry = registry
        self.permission = permission
        self.ctx = ctx
        self._approval_cache: dict[str, Decision] = {}

    def _cache_key(self, name: str, args: dict[str, Any]) -> str:
        raw = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def execute(self, call: ToolCall, tool: Tool | None = None) -> ToolResult:
        started = time.monotonic()
        self.ctx.emit(
            ToolCallBegin(
                call_id=call.id,
                name=call.name,
                arguments=_event_arguments(call.name, call.arguments),
            )
        )

        async def finish(result: ToolResult) -> ToolResult:
            ms = int((time.monotonic() - started) * 1000)
            # 全部工具输出归档 CAS（FlagGate 溯源证据），与截断 spill 相互独立；
            # 句柄记入 data["cas_handle"] 供微压缩引用（不占 handle 字段，避免误报截断）。
            # 无工作区（/setting 配置 agent）时跳过归档。
            if result.output and self.ctx.workspace is not None:
                # FlagGate 时序规则另一半：输出侧首现登记（自我注入检测）
                gate = getattr(self.ctx, "flag_gate", None)
                if gate is not None:
                    gate.output_scan(result.output)
                result.data.setdefault(
                    "cas_handle",
                    self.ctx.workspace.cas_store(result.output.encode("utf-8", errors="replace")),
                )
            self.ctx.emit(
                ToolCallEnd(
                    call_id=call.id,
                    name=call.name,
                    ok=result.ok,
                    output_preview=result.output[:200],
                    error=result.error,
                    duration_ms=ms,
                    handle=result.handle or result.data.get("cas_handle"),
                )
            )
            return result

        if tool is None:
            return await finish(ToolResult(ok=False, error=f"未知工具: {call.name}"))
        # FlagGate 污染扫描：agent 工具输入里的 flag 样字符串记入污染集，
        # 防"自己注入再提交"绕过溯源（submit_flag 自身参数除外，否则合法提交自锁）
        gate = getattr(self.ctx, "flag_gate", None)
        if gate is not None and call.name != "submit_flag":
            gate.taint_scan(call.arguments)
        try:
            args = json.loads(call.arguments) if call.arguments else {}
        except json.JSONDecodeError as exc:
            return await finish(ToolResult(ok=False, error=f"工具参数不是合法 JSON: {exc}"))

        decision = self._approval_cache.get(self._cache_key(call.name, args))
        if decision is None:
            decision = self.permission.check(tool, args)
            if decision is Decision.ALLOW:
                self._approval_cache[self._cache_key(call.name, args)] = decision
        if decision is Decision.DENY:
            return await finish(ToolResult(ok=False, error=f"权限拒绝（{self.permission.mode} 模式策略链）"))
        if decision is Decision.ASK:
            # M1 headless：无审批 UI，直接拒绝并记录（协议事件类型已保留）
            return await finish(ToolResult(ok=False, error="需要审批但当前为 headless 模式，已拒绝"))

        try:
            result = await tool.run(args, self.ctx)
        except Exception as exc:  # noqa: BLE001 - 工具异常一律转 ToolResult
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return await finish(result)

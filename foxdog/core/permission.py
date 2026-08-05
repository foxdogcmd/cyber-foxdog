"""权限策略链（借鉴 kimi-code 策略链，一策略一环可插拔）。

链序：builtin-deny（硬禁区）→ mode-policy（yolo/plan）→ fallback。
M1 不做交互审批 UI，但 Decision.ASK 与 ApprovalRequest/Response 协议类型保留，
headless 模式遇 ASK 直接拒绝并记录。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Protocol

from ..tools import Tool


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class Policy(Protocol):
    def check(self, tool: Tool, args: dict[str, Any]) -> Decision | None:
        """返回 None 表示本策略不表态，交给下一环。"""
        ...


# 硬编码禁区（防误伤本机；CTF 信任靶机）
_DENY_PATTERNS = [
    re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(?:\s|$)"),  # rm -rf /
    re.compile(r"rm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/(?:\s|$)"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=.*\bof=/dev/"),
    re.compile(r":\(\)\{\s*:\|:&\s*\};:"),  # fork 炸弹
]


class BuiltinDenyPolicy:
    """硬禁区：危险 shell 模式、写工作区外路径。"""

    def check(self, tool: Tool, args: dict[str, Any]) -> Decision | None:
        if tool.name == "shell_exec":
            cmd = str(args.get("command", ""))
            for rx in _DENY_PATTERNS:
                if rx.search(cmd):
                    return Decision.DENY
        if tool.name == "shell_session" and args.get("action") == "write":
            data = str(args.get("data", ""))
            for rx in _DENY_PATTERNS:
                if rx.search(data):
                    return Decision.DENY
        return None


class ModePolicy:
    """yolo 全放行（交给 fallback allow）；plan 只允许只读工具。"""

    def __init__(self, mode: str = "yolo") -> None:
        self.mode = mode

    def check(self, tool: Tool, args: dict[str, Any]) -> Decision | None:
        if self.mode == "plan" and tool.readonly:
            return Decision.ALLOW
        return None


class FallbackPolicy:
    def __init__(self, mode: str = "yolo") -> None:
        self.mode = mode

    def check(self, tool: Tool, args: dict[str, Any]) -> Decision | None:
        return Decision.ALLOW if self.mode == "yolo" else Decision.DENY


class PermissionChain:
    def __init__(self, mode: str = "yolo", policies: list[Policy] | None = None) -> None:
        self.mode = mode
        self.policies: list[Policy] = policies or [
            BuiltinDenyPolicy(),
            ModePolicy(mode),
            FallbackPolicy(mode),
        ]

    def check(self, tool: Tool, args: dict[str, Any]) -> Decision:
        for policy in self.policies:
            decision = policy.check(tool, args)
            if decision is not None:
                return decision
        return Decision.DENY  # 链尾兜底：拒绝

    def set_mode(self, mode: str) -> None:
        """运行中切换权限模式（TUI /mode 命令）。"""
        self.mode = mode
        for policy in self.policies:
            if hasattr(policy, "mode"):
                policy.mode = mode  # type: ignore[attr-defined]

"""异构 race 引擎规划（muteki race 设计落到 foxdog 架构）。

race 阶段的 worker 集合 = 内置 solver × 1 + 每个 [model.extra.<name>] 端点 × 1
+ 外部 CLI 引擎（claude code 等，见 cli_engine.py）。
本模块负责"有哪些引擎"的规划（配置/探测/上限截断），装配在 runner.py。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig
from ..llm.client import ModelEndpoint


@dataclass
class EngineSpec:
    """一个异构 race 引擎：endpoint 非空 = 模型端点引擎；cli 非空 = 外部 CLI 引擎。"""

    name: str
    endpoint: ModelEndpoint | None = None
    cli: str | None = None

    @property
    def kind(self) -> str:
        return "cli" if self.cli else "model"

    @property
    def display_name(self) -> str:
        """Short, user-facing name used by the TUI status line."""
        if self.endpoint is not None:
            return self.endpoint.model or self.endpoint.provider or self.name.removeprefix("engine-")
        if self.name == "engine-claude":
            return "Claude Code"
        return Path(self.cli or self.name).stem


def plan_race_engines(cfg: AppConfig, *, claude_path: str | None = None) -> tuple[list[EngineSpec], list[str]]:
    """按配置收集 race 异构引擎；返回 (specs, notes)。

    - [engines].enabled=false → 空（race 只剩内置 solver）。
    - [model.extra.*] 按配置顺序各成一个模型引擎。
    - claude code：enabled 且所选执行环境探测到才加入；claude_path 由 runner 解析。
    - 总数超 agent.max_race_engines 截断并给 note。
    """
    specs: list[EngineSpec] = []
    notes: list[str] = []
    if not cfg.engines.enabled:
        return specs, notes
    # Multi-solver: additional profiles participate as race engines so
    # different model suppliers can cooperate from the start.
    for name, ep in cfg.solver_endpoints.items():
        if name != "solver":
            specs.append(EngineSpec(name=f"engine-{name}", endpoint=ep))
    for name, ep in (cfg.extra or {}).items():
        specs.append(EngineSpec(name=f"engine-{name}", endpoint=ep))
    if cfg.engines.claude_code.enabled:
        path = claude_path if claude_path is not None else shutil.which("claude")
        if path:
            specs.append(EngineSpec(name="engine-claude", cli=path))
        else:
            notes.append("当前 Claude 执行环境未找到 claude，跳过该引擎")
    cap = max(1, cfg.agent.max_race_engines)
    if len(specs) > cap:
        kept, dropped = specs[:cap], specs[cap:]
        notes.append(f"race 引擎超上限 {cap}，截断：保留 {[s.name for s in kept]}，丢弃 {[s.name for s in dropped]}")
        specs = kept
    return specs, notes

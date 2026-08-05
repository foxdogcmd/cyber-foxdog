"""Read the minimal CC Switch / Claude Code environment without persisting secrets."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ClaudeConfigError(ValueError):
    """Host Claude settings are missing or cannot authenticate a headless run."""


_EXACT_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
}
_ENV_PREFIXES = ("ANTHROPIC_DEFAULT_", "CLAUDE_CODE_")
_AUTH_KEYS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}


def default_claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def load_claude_code_env(path: str | Path | None = None) -> dict[str, str]:
    """Extract only Claude endpoint/auth/model variables from host settings.json."""
    settings_path = Path(path) if path is not None else default_claude_settings_path()
    try:
        raw: Any = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ClaudeConfigError(f"未找到本机 Claude Code 配置：{settings_path}") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClaudeConfigError(f"无法读取本机 Claude Code 配置：{type(exc).__name__}") from None

    source = raw.get("env") if isinstance(raw, dict) else None
    if not isinstance(source, dict):
        raise ClaudeConfigError("本机 Claude Code settings.json 没有 env 配置段")
    selected = {
        key: value
        for key, value in source.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and value
        and (key in _EXACT_ENV_KEYS or key.startswith(_ENV_PREFIXES))
    }
    if not any(selected.get(key) for key in _AUTH_KEYS):
        raise ClaudeConfigError("本机 Claude Code 配置缺少 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY")
    return selected

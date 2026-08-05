"""配置：内置默认 < 用户 ~/.config/foxdog/config.toml < 项目 ./foxdog.toml < 环境变量 < CLI -c key=value。

示例：
    [model.solver]
    provider = "openai-compatible"
    base_url = "https://api.deepseek.com"
    model = "deepseek-v4-flash"
    api_key_env = "DEEPSEEK_API_KEY"
    thinking = "enabled"
    reasoning_effort = "high"
    # [model.backup]  # 可选备用端点，主连续失败 2 次自动切换
    [agent]
    mode = "yolo"
    max_steps = 200
    [budget]
    max_minutes = 60
    # [runtime]
    # kind = "docker"  # 每个 solve run 一个共享工具容器；默认 local

环境变量兜底（方便测试/临时使用）：FOXDOG_PROVIDER / FOXDOG_BASE_URL /
FOXDOG_MODEL / FOXDOG_API_KEY / FOXDOG_RUNTIME。FOXDOG_PROVIDER=fake 走内置脚本化
provider 冒烟；FOXDOG_RUNTIME=local|docker 选择工具执行环境。
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .llm.client import ModelEndpoint

USER_CONFIG = Path.home() / ".config" / "foxdog" / "config.toml"
PROJECT_CONFIG = Path("foxdog.toml")


class AgentConfig(BaseModel):
    mode: str = "yolo"  # yolo | plan
    max_race_engines: int = 3  # race 阶段异构引擎上限（超出的端点按配置顺序截断）
    max_steps: int = 200
    context_window: int = 128_000  # 模型上下文窗口（token），压缩阈值按 85% 算


class BudgetConfig(BaseModel):
    max_minutes: float | None = None  # None=不限（用户未指定不设预算上限）
    max_tokens: int | None = None
    max_turns: int | None = None

    def describe(self) -> str:
        def m(v: float | None) -> str:
            return "∞" if v is None else f"{v:g}"

        return f"minutes={m(self.max_minutes)} tokens={m(self.max_tokens)} turns={m(self.max_turns)}"


class McpServerConfig(BaseModel):
    """一个 MCP server：stdio（command/args/env）或 streamable HTTP（url）。"""

    command: str | None = None
    args: list[str] = []
    env: dict[str, str] | None = None
    url: str | None = None


class PlatformConfig(BaseModel):
    """可配置的题目平台 HTTP 生命周期。"""

    fetch_url: str = ""  # 题目拉取地址（GET；支持 {id} 占位）
    fetch_method: Literal["GET", "POST"] = "GET"
    fetch_body: dict[str, Any] = Field(default_factory=dict)
    submit_url: str = ""  # 兼容旧配置
    submit_method: Literal["POST", "PUT", "PATCH"] = "POST"
    submit_body: dict[str, Any] = Field(default_factory=dict)
    status_url: str = ""  # 可选状态轮询地址，支持 {id}/{run_id}
    status_method: Literal["GET", "POST"] = "GET"
    poll_seconds: float = 0.0
    attachment_field: str = "attachments"  # JSON 中附件 URL 数组字段
    flag_format: str = ""  # 本场默认 flag 格式正则
    default_workers: int = 5  # /workers 缺省值（交互会话初始）
    expected_flags: int = 1  # 本场每题预期 flag 面数（多 flag 题 >1）


class RuntimeConfig(BaseModel):
    """工具执行环境；默认 local 保持现有宿主执行行为。"""

    kind: Literal["local", "docker", "docker-persistent"] = "local"
    image: str = "foxdog-runtime:full"
    docker_binary: str = "docker"
    network: str = "bridge"
    mounts: list[str] = Field(default_factory=list)
    auto_build: bool = True
    auto_remove: bool = True
    workdir: str = "/workspace/work"


class ClaudeCodeConfig(BaseModel):
    enabled: bool = False
    containerized: bool = True
    import_host_config: bool = False


class EnginesConfig(BaseModel):
    """异构 race 引擎；Claude 须经 /setting 显式启用。"""

    enabled: bool = True
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)


class ModelRolesConfig(BaseModel):
    """Saved model profiles assigned to long-lived runtime roles."""

    solvers: list[str] = Field(default_factory=list)
    planners: list[str] = Field(default_factory=list)
    spectators: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    solver: ModelEndpoint = ModelEndpoint()
    backup: ModelEndpoint | None = None
    planner: ModelEndpoint | None = None  # 压缩/摘要/ReasonPlanner 专用便宜模型，缺省用 solver
    spectator: ModelEndpoint | None = None  # 全局审查/收口模型，缺省不启用
    extra: dict[str, ModelEndpoint] = {}  # [model.extra.<name>] 异构 race 引擎端点
    profiles: dict[str, ModelEndpoint] = Field(default_factory=dict)
    roles: ModelRolesConfig = Field(default_factory=ModelRolesConfig)
    # Runtime projections. A worker stays on one entry for its whole lifetime;
    # this preserves Anthropic signatures and Kimi preserved-thinking history.
    solver_endpoints: dict[str, ModelEndpoint] = Field(default_factory=dict)
    planner_endpoints: dict[str, ModelEndpoint] = Field(default_factory=dict)
    agent: AgentConfig = AgentConfig()
    budget: BudgetConfig = BudgetConfig()
    mcp_servers: dict[str, McpServerConfig] = {}
    platform: PlatformConfig = PlatformConfig()
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    engines: EnginesConfig = EnginesConfig()


def _load_toml(path: Path) -> dict[str, Any]:
    if path.is_file():
        with path.open("rb") as f:
            return tomllib.load(f)
    return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _parse_override(pair: str) -> tuple[list[str], Any]:
    """`-c key=value`：key 用点分层，值尝试 tomllib 解析（数字/布尔/字符串）。"""
    key, _, raw = pair.partition("=")
    if not key or not _:
        raise ValueError(f"-c 覆盖格式应为 key=value: {pair!r}")
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip().strip('"').strip("'")
    return key.strip().split("."), value


def load_config(cli_overrides: list[str] | None = None) -> AppConfig:
    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, _load_toml(USER_CONFIG))
    merged = _deep_merge(merged, _load_toml(PROJECT_CONFIG))

    # 环境变量兜底
    env_model: dict[str, Any] = {}
    if os.environ.get("FOXDOG_PROVIDER"):
        env_model["provider"] = os.environ["FOXDOG_PROVIDER"]
    if os.environ.get("FOXDOG_BASE_URL"):
        env_model["base_url"] = os.environ["FOXDOG_BASE_URL"]
    if os.environ.get("FOXDOG_MODEL"):
        env_model["model"] = os.environ["FOXDOG_MODEL"]
    if os.environ.get("FOXDOG_API_KEY"):
        env_model["api_key"] = os.environ["FOXDOG_API_KEY"]
    if env_model:
        merged = _deep_merge(merged, {"model": {"solver": env_model}})
    if runtime_kind := os.environ.get("FOXDOG_RUNTIME", "").strip():
        merged = _deep_merge(merged, {"runtime": {"kind": runtime_kind}})

    # CLI -c 覆盖
    for pair in cli_overrides or []:
        keys, value = _parse_override(pair)
        node = merged
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    model_cfg = merged.get("model", {})
    model_fields = parse_model_config(model_cfg)
    # Explicit environment model overrides remain the strongest model source,
    # matching the pre-profile behavior used by smoke tests and one-off runs.
    if env_model:
        model_fields["solver"] = ModelEndpoint(**model_cfg.get("solver", {}))
        model_fields["solver_endpoints"] = {"solver": model_fields["solver"]}
    return AppConfig(
        **model_fields,
        agent=AgentConfig(**merged.get("agent", {})),
        budget=BudgetConfig(**merged.get("budget", {})),
        mcp_servers={k: McpServerConfig(**v) for k, v in merged.get("mcp_servers", {}).items()},
        platform=PlatformConfig(**merged.get("platform", {})),
        runtime=RuntimeConfig(**merged.get("runtime", {})),
        engines=EnginesConfig(**merged.get("engines", {})),
    )


def parse_model_config(model_cfg: dict[str, Any]) -> dict[str, Any]:
    """Parse both the legacy role slots and profile-based model configuration.

    New configuration stores reusable endpoints under ``model.profiles`` and
    assigns profile names under ``model.roles``. Legacy slots remain readable;
    they are not silently rewritten on disk.
    """

    legacy_solver = ModelEndpoint(**model_cfg.get("solver", {}))
    backup = ModelEndpoint(**model_cfg["backup"]) if model_cfg.get("backup") else None
    legacy_planner = ModelEndpoint(**model_cfg["planner"]) if model_cfg.get("planner") else None
    spectator = ModelEndpoint(**model_cfg["spectator"]) if model_cfg.get("spectator") else None
    extra = {k: ModelEndpoint(**v) for k, v in model_cfg.get("extra", {}).items()}
    profiles = {k: ModelEndpoint(**v) for k, v in model_cfg.get("profiles", {}).items()}
    roles = ModelRolesConfig(**model_cfg.get("roles", {}))

    solver_endpoints = {
        name: profiles[name]
        for name in roles.solvers
        if name in profiles
    }
    if solver_endpoints:
        solver = next(iter(solver_endpoints.values()))
    else:
        solver = legacy_solver
        solver_endpoints = {"solver": solver}

    planner_endpoints = {
        name: profiles[name]
        for name in roles.planners
        if name in profiles
    }
    planner = next(iter(planner_endpoints.values()), legacy_planner)
    if not planner_endpoints and legacy_planner is not None:
        planner_endpoints = {"planner": legacy_planner}

    return {
        "solver": solver,
        "backup": backup,
        "planner": planner,
        "spectator": spectator,
        "extra": extra,
        "profiles": profiles,
        "roles": roles,
        "solver_endpoints": solver_endpoints,
        "planner_endpoints": planner_endpoints,
    }


def build_endpoint_provider(endpoint: ModelEndpoint):
    """Build one provider from its wire protocol."""

    from .llm.client import FakeProvider, LLMClient

    if endpoint.provider == "fake":
        return FakeProvider()
    if endpoint.protocol == "anthropic":
        from .llm.anthropic_client import AnthropicClient  # 懒加载 anthropic SDK

        return AnthropicClient(endpoint)
    if endpoint.protocol == "responses":
        from .llm.responses_client import ResponsesClient

        return ResponsesClient(endpoint)
    return LLMClient(endpoint)


class ProviderChain:
    """Ordered role fallback without switching a worker between healthy models."""

    def __init__(self, providers: list[Any]) -> None:
        if not providers:
            raise ValueError("provider chain cannot be empty")
        self.providers = providers
        self.current = 0

    async def stream_chat(self, *args: Any, **kwargs: Any):
        last_error: Exception | None = None
        for offset in range(len(self.providers)):
            index = (self.current + offset) % len(self.providers)
            try:
                result = await self.providers[index].stream_chat(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - next configured role is the fallback
                last_error = exc
                continue
            self.current = index
            return result
        assert last_error is not None
        raise last_error


def build_role_provider(endpoints: dict[str, ModelEndpoint], fallback: ModelEndpoint):
    """Build an ordered role provider; later profiles are failure fallbacks."""

    selected = list(endpoints.values()) or [fallback]
    providers = [build_endpoint_provider(endpoint) for endpoint in selected]
    return providers[0] if len(providers) == 1 else ProviderChain(providers)


def build_provider(cfg: AppConfig):
    """Build the primary solver provider, retaining legacy same-wire backup."""

    from .llm.client import FakeProvider, LLMClient

    if cfg.solver.provider == "fake":
        return FakeProvider()
    if cfg.backup is None or cfg.backup.protocol != cfg.solver.protocol:
        return build_endpoint_provider(cfg.solver)
    if cfg.solver.protocol == "anthropic":
        from .llm.anthropic_client import AnthropicClient

        return AnthropicClient(cfg.solver, cfg.backup)
    if cfg.solver.protocol == "openai":
        return LLMClient(cfg.solver, cfg.backup)
    # Responses failover is handled as a protocol-aware provider chain.
    return ProviderChain([build_endpoint_provider(cfg.solver), build_endpoint_provider(cfg.backup)])


# ---------------------------------------------------------------------------
# ConfigStore：用户配置文件的读写（TUI 会话内 /model、/setting 的落盘通道）。
# 读用 tomllib；写用手写两级 TOML 序列化（不引 tomli-w 之类的额外依赖）。
# save() 原子写（tmp + replace），文件权限 0600（api_key 落盘，用户明确要求）。

_SCALAR = (str, int, float, bool)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def dump_toml(data: dict[str, Any]) -> str:
    """递归 TOML 序列化（任意深度 [a.b.c] 表）；值限标量与字符串数组。"""
    lines: list[str] = []

    def emit_table(path: list[str], body: dict[str, Any]) -> None:
        nonlocal lines
        scalars = {k: v for k, v in body.items() if isinstance(v, _SCALAR) or isinstance(v, list)}
        subs = {k: v for k, v in body.items() if isinstance(v, dict)}
        if scalars or not subs:
            lines.append(f"[{'.'.join(path)}]")
            lines += [f"{k} = {_toml_value(v)}" for k, v in scalars.items()]
            lines.append("")
        for sub, sub_body in subs.items():
            emit_table([*path, sub], sub_body)

    for section, body in data.items():
        if isinstance(body, dict):
            emit_table([section], body)
    return "\n".join(lines).strip() + "\n"


class ConfigStore:
    """~/.config/foxdog/config.toml 的读写存根（交互式配置的事实源）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else USER_CONFIG
        self.data: dict[str, Any] = {}
        if self.path.is_file():
            with self.path.open("rb") as f:
                self.data = tomllib.load(f)

    # ------------------------------------------------------------ 写入
    def save(self) -> None:
        """原子落盘（tmp+replace），权限 0600。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(dump_toml(self.data), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ 分区访问
    def section(self, name: str) -> dict[str, Any]:
        return self.data.setdefault(name, {})

    def set_model(self, role: str, **fields: Any) -> None:
        """role: solver | planner | backup | extra:<name>（[model.extra.<name>]）；None 值跳过。"""
        model = self.data.setdefault("model", {})
        if role.startswith("extra:"):
            name = role.split(":", 1)[1].strip()
            if not name:
                raise ValueError("extra: 后必须跟端点名")
            target = model.setdefault("extra", {}).setdefault(name, {})
        else:
            target = model.setdefault(role, {})
        for k, v in fields.items():
            if v is not None:
                target[k] = v

    @staticmethod
    def _profile_name(name: str) -> str:
        value = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("模型别名只能含字母、数字、_、-，且必须以字母或数字开头")
        return value

    def ensure_model_profiles(self) -> tuple[dict[str, Any], ModelRolesConfig]:
        """Expose legacy role slots as reusable profiles without deleting them."""

        model = self.data.setdefault("model", {})
        profiles = model.setdefault("profiles", {})
        role_data = model.setdefault("roles", {})
        roles = ModelRolesConfig(**role_data)

        if isinstance(model.get("solver"), dict) and "current-solver" not in profiles:
            profiles["current-solver"] = dict(model["solver"])
        if not roles.solvers and "current-solver" in profiles:
            roles.solvers = ["current-solver"]

        if isinstance(model.get("planner"), dict) and "current-planner" not in profiles:
            profiles["current-planner"] = dict(model["planner"])
        if not roles.planners:
            if "current-planner" in profiles:
                roles.planners = ["current-planner"]
            elif roles.solvers:
                roles.planners = [roles.solvers[0]]

        for old_name, endpoint in (model.get("extra", {}) or {}).items():
            if not isinstance(endpoint, dict):
                continue
            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(old_name)).strip("-") or "legacy-extra"
            profiles.setdefault(safe_name[:64], dict(endpoint))

        role_data["solvers"] = roles.solvers
        role_data["planners"] = roles.planners
        return profiles, roles

    def save_model_profile(self, name: str, **fields: Any) -> None:
        """Create or update one reusable model endpoint."""

        safe_name = self._profile_name(name)
        profiles, _roles = self.ensure_model_profiles()
        target = profiles.setdefault(safe_name, {})
        for key, value in fields.items():
            if value is not None:
                target[key] = value

    def set_model_roles(
        self,
        *,
        solvers: list[str] | None = None,
        planners: list[str] | None = None,
    ) -> None:
        """Assign saved profiles to ordered solver and planner role lists."""

        profiles, current = self.ensure_model_profiles()
        next_solvers = list(dict.fromkeys(solvers if solvers is not None else current.solvers))
        next_planners = list(dict.fromkeys(planners if planners is not None else current.planners))
        if not next_solvers:
            raise ValueError("solver 角色至少需要一个已保存模型")
        missing = [name for name in [*next_solvers, *next_planners] if name not in profiles]
        if missing:
            raise ValueError(f"未找到已保存模型: {', '.join(dict.fromkeys(missing))}")
        role_data = self.data.setdefault("model", {}).setdefault("roles", {})
        role_data["solvers"] = next_solvers
        role_data["planners"] = next_planners

    def remove_model_profile(self, name: str) -> bool:
        """Delete one profile and remove it from all role assignments."""

        profiles, roles = self.ensure_model_profiles()
        if name not in profiles:
            return False
        if len(roles.solvers) == 1 and roles.solvers[0] == name:
            raise ValueError("不能删除唯一的 solver 模型；请先给其他模型分配 solver 角色")
        profiles.pop(name)
        role_data = self.data.setdefault("model", {}).setdefault("roles", {})
        role_data["solvers"] = [item for item in roles.solvers if item != name]
        role_data["planners"] = [item for item in roles.planners if item != name]
        return True

    def remove_model_endpoint(self, name: str) -> bool:
        """删除 [model.extra.<name>] 端点；不存在返回 False。"""
        extra = self.data.setdefault("model", {}).setdefault("extra", {})
        return extra.pop(name, None) is not None

    def set_platform(self, **fields: Any) -> None:
        platform = self.data.setdefault("platform", {})
        for k, v in fields.items():
            if v is not None:
                platform[k] = v

    def set_runtime(self, **fields: Any) -> None:
        runtime = self.data.setdefault("runtime", {})
        for k, v in fields.items():
            if v is not None:
                runtime[k] = v

    def add_mcp_server(self, name: str, **fields: Any) -> None:
        servers = self.data.setdefault("mcp_servers", {})
        servers[name] = {k: v for k, v in fields.items() if v is not None}

    def remove_mcp_server(self, name: str) -> bool:
        servers = self.data.setdefault("mcp_servers", {})
        return servers.pop(name, None) is not None

    def set_agent(self, **fields: Any) -> None:
        """agent 段（mode/max_steps/engines_enabled）与 budget 段（budget_minutes → max_minutes）。"""
        agent = self.data.setdefault("agent", {})
        for k in ("mode", "max_steps", "context_window", "max_race_engines"):
            if fields.get(k) is not None:
                agent[k] = fields[k]
        if fields.get("budget_minutes") is not None:
            self.data.setdefault("budget", {})["max_minutes"] = fields["budget_minutes"]
        if fields.get("engines_enabled") is not None:
            self.data.setdefault("engines", {})["enabled"] = fields["engines_enabled"]

    def set_claude_code(self, **fields: Any) -> None:
        claude = self.data.setdefault("engines", {}).setdefault("claude_code", {})
        for key in ("enabled", "containerized", "import_host_config"):
            if fields.get(key) is not None:
                claude[key] = fields[key]

    def masked(self) -> dict[str, Any]:
        """脱敏副本（api_key → ***），供 get_config 工具/展示用。"""
        def _mask(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: ("***" if k == "api_key" and v else _mask(v)) for k, v in node.items()}
            if isinstance(node, list):
                return [_mask(x) for x in node]
            return node

        return _mask(self.data)

"""配置工具（/setting 专用）：无 shell/任意文件和任意网络工具（物理隔离）。

仅 ``list_deepseek_models`` 可读取 DeepSeek 官方模型列表；其余工具只读写配置。
``configure_claude_code`` 只能读取固定的本机 ``~/.claude/settings.json``，
且只返回环境变量名，永不返回值。
每个写工具改完即 ConfigStore.save() 落盘（原子写 + 0600），返回中文确认文本。
"""
from __future__ import annotations

import re
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, Field

from ...config import ConfigStore
from ...llm.client import ModelEndpoint, is_deepseek_endpoint
from ...llm.model_catalog import fetch_model_ids
from ...swarm.claude_config import ClaudeConfigError, default_claude_settings_path, load_claude_code_env
from ...skills.loader import USER_DIR, _synth_manifest
from .. import Tool, ToolAccess, ToolContext, ToolResult
from .poc_library import ListPocsTool, SavePocTool

DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"


class SecretVault:
    """Session-only secret handles. Values never enter model messages or tool results."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._seq = 0

    def put(self, value: str) -> str:
        self._seq += 1
        handle = f"secret://setting/{self._seq}"
        self._values[handle] = value
        return handle

    def resolve(self, value: str | None) -> str | None:
        if value and value.startswith("secret://setting/"):
            return self._values.get(value)
        return value

    def resolve_text(self, value: str | None) -> str | None:
        """Resolve secret handles embedded in URLs or other templates."""
        if value is None:
            return None
        return re.sub(
            r"secret://setting/\d+",
            lambda match: self._values.get(match.group(0), match.group(0)),
            value,
        )


class _StoreTool(Tool):
    """持有 ConfigStore 的基类；state 资源写（配置工具间串行）。"""

    accesses = [ToolAccess(resource="state", mode="w")]

    def __init__(self, store: ConfigStore) -> None:
        self.store = store


class GetConfigArgs(BaseModel):
    section: str | None = Field(default=None, description="配置段名（model/platform/runtime/engines/agent/budget/mcp_servers），空为全部")


class GetConfigTool(_StoreTool):
    name = "get_config"
    description = "读取当前配置（api_key 脱敏为 ***）。"
    args_model = GetConfigArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        data = self.store.masked()
        if a.section:
            return ToolResult(output=f"[{a.section}]\n{_fmt(data.get(a.section, {}))}")
        return ToolResult(output=_fmt(data))


class ListDeepSeekModelsArgs(BaseModel):
    pass


class ListDeepSeekModelsTool(_StoreTool):
    name = "list_deepseek_models"
    description = "从 DeepSeek 官方 GET /models 实时获取当前 API key 可用的模型 ID；配置模型前先调用。"
    args_model = ListDeepSeekModelsArgs
    accesses = [
        ToolAccess(resource="state", mode="r"),
        ToolAccess(resource="network", mode="r"),
    ]

    def _resolved_key(self) -> str:
        models = self.store.section("model")
        for role in ("solver", "planner", "backup"):
            raw = models.get(role)
            if not isinstance(raw, dict):
                continue
            try:
                endpoint = ModelEndpoint(**raw)
            except Exception:  # noqa: BLE001 - skip incomplete stored endpoints
                continue
            if is_deepseek_endpoint(endpoint):
                key = endpoint.resolved_key()
                if key:
                    return key
        return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("FOXDOG_API_KEY", "")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.parse_args(args)
        key = self._resolved_key()
        if not key:
            return ToolResult(
                ok=False,
                error="未找到 DeepSeek API key；先用 set_model 保存 key 或配置 DEEPSEEK_API_KEY。",
            )
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                response = await client.get(
                    DEEPSEEK_MODELS_URL,
                    headers={"Authorization": f"Bearer {key}"},
                )
                response.raise_for_status()
            payload = response.json()
            entries = payload.get("data") if isinstance(payload, dict) else None
            model_ids = sorted(
                str(item["id"])
                for item in (entries or [])
                if isinstance(item, dict) and item.get("id")
            )
            if not model_ids:
                return ToolResult(ok=False, error="DeepSeek /models 返回成功，但响应中没有模型 ID。")
            return ToolResult(output="DeepSeek 当前可用模型（官方实时列表）：\n" + "\n".join(f"- {mid}" for mid in model_ids))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = "API key 无效或无权限" if status == 401 else "官方接口请求失败"
            return ToolResult(ok=False, error=f"DeepSeek /models {detail}（HTTP {status}）。")
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(ok=False, error=f"DeepSeek /models 请求失败：{type(exc).__name__}: {exc}")


def _fmt(node: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(node, dict):
        lines = []
        for k, v in node.items():
            if isinstance(v, dict):
                lines.append(f"{pad}{k}:")
                lines.append(_fmt(v, indent + 1))
            else:
                lines.append(f"{pad}{k} = {v}")
        return "\n".join(lines)
    return f"{pad}{node}"


class SetModelArgs(BaseModel):
    role: str = Field(description="solver | planner | spectator | backup 或 extra:<name>")
    protocol: str | None = Field(default=None, description="openai | anthropic")
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = Field(default=None, description="模型名")
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: Literal["enabled", "disabled"] | None = Field(
        default=None,
        description="DeepSeek 思考模式 enabled | disabled",
    )
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None,
        description="DeepSeek 推理强度 low | medium | high | xhigh | max",
    )


class SetModelTool(_StoreTool):
    name = "set_model"
    description = (
        "配置模型端点并落盘。DeepSeek 可使用 protocol=openai + "
        "https://api.deepseek.com，或 protocol=anthropic + https://api.deepseek.com/anthropic；"
        "model 使用 list_deepseek_models 返回的 ID；"
        "可设置 thinking=enabled/disabled 与 reasoning_effort=low/medium/high/xhigh/max。"
    )
    args_model = SetModelArgs

    def __init__(self, store: ConfigStore, secrets: SecretVault | None = None) -> None:
        super().__init__(store)
        self.secrets = secrets

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if a.role not in ("solver", "planner", "spectator", "backup") and not a.role.startswith("extra:"):
            return ToolResult(ok=False, error="role 必须是 solver/planner/spectator/backup 或 extra:<端点名>")
        if a.role.startswith("extra:") and not a.role.split(":", 1)[1].strip():
            return ToolResult(ok=False, error="extra: 后必须跟端点名")
        if a.protocol is not None and a.protocol not in ("openai", "responses", "anthropic"):
            return ToolResult(ok=False, error="protocol 必须是 openai/responses/anthropic")
        resolved_key = self.secrets.resolve(a.api_key) if self.secrets else a.api_key
        current = self.store.section("model").get(a.role, {})
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update({
            key: value for key, value in {
                "protocol": a.protocol,
                "base_url": a.base_url,
                "api_key": resolved_key,
                "model": a.model,
            }.items() if value is not None
        })
        if a.model and merged.get("base_url") and merged.get("api_key"):
            ids = await fetch_model_ids(ModelEndpoint(**merged))
            if ids and a.model not in ids:
                return ToolResult(
                    ok=False,
                    error=f"模型 ID {a.model!r} 不在端点实时列表中；可用值：{', '.join(ids)}",
                )
        self.store.set_model(
            a.role,
            protocol=a.protocol,
            base_url=a.base_url,
            api_key=resolved_key,
            model=a.model,
            temperature=a.temperature,
            max_tokens=a.max_tokens,
            thinking=a.thinking,
            reasoning_effort=a.reasoning_effort,
        )
        self.store.save()
        shown = {k: v for k, v in self.store.section("model").get(a.role, {}).items() if k != "api_key"}
        return ToolResult(output=f"已保存模型配置 [{a.role}]：{shown}" + ("（api_key 已由宿主安全落盘，不回显）" if a.api_key else ""))


class ListModelsArgs(BaseModel):
    role: str = Field(default="solver", description="已保存端点：solver/planner/backup 或 extra:<name>")


class ListModelsTool(_StoreTool):
    name = "list_models"
    description = "从指定已保存端点实时获取模型 ID；配置模型前调用，支持 DeepSeek、Kimi 与兼容端点。"
    args_model = ListModelsArgs
    accesses = [ToolAccess(resource="state", mode="r"), ToolAccess(resource="network", mode="r")]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        role = self.parse_args(args).role
        model_data = self.store.section("model")
        raw = model_data.get("extra", {}).get(role.split(":", 1)[1]) if role.startswith("extra:") else model_data.get(role)
        if not isinstance(raw, dict):
            return ToolResult(ok=False, error=f"未找到已保存端点 {role}")
        try:
            ids = await fetch_model_ids(ModelEndpoint(**raw))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=f"模型列表获取失败：{type(exc).__name__}: {exc}")
        if not ids:
            return ToolResult(ok=False, error="端点未提供模型列表或当前网络/密钥不可用；可继续手动填写模型 ID。")
        return ToolResult(output="当前可用模型：\n" + "\n".join(f"- {item}" for item in ids))


class SetPlatformArgs(BaseModel):
    fetch_url: str | None = Field(default=None, description="题目拉取地址（GET，支持 {id} 占位）")
    fetch_method: Literal["GET", "POST"] | None = None
    fetch_body: dict[str, Any] | None = None
    submit_url: str | None = Field(default=None, description="答案提交地址")
    submit_method: Literal["POST", "PUT", "PATCH"] | None = None
    submit_body: dict[str, Any] | None = Field(default=None, description="请求体模板，支持 {answer}/{flag}/{run_id}/{details}")
    status_url: str | None = Field(default=None, description="状态轮询地址，支持 {id}/{run_id}")
    status_method: Literal["GET", "POST"] | None = None
    poll_seconds: float | None = Field(default=None, ge=0, description="状态轮询间隔，0 表示关闭")
    attachment_field: str | None = Field(default=None, description="响应 JSON 中附件 URL 数组字段")
    flag_format: str | None = Field(default=None, description="本场默认 flag 格式正则")
    default_workers: int | None = Field(default=None, description="默认 worker 数")
    expected_flags: int | None = Field(default=None, description="本场每题预期 flag 面数（多 flag 题 >1）")


class SetPlatformTool(_StoreTool):
    name = "set_platform"
    description = "配置统一平台：题目拉取地址、flag 提交地址、默认 flag 格式、默认 worker 数、每题预期 flag 面数。"
    args_model = SetPlatformArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        self.store.set_platform(
            fetch_url=a.fetch_url,
            fetch_method=a.fetch_method,
            fetch_body=a.fetch_body,
            submit_url=a.submit_url,
            submit_method=a.submit_method,
            submit_body=a.submit_body,
            status_url=a.status_url,
            status_method=a.status_method,
            poll_seconds=a.poll_seconds,
            attachment_field=a.attachment_field,
            flag_format=a.flag_format,
            default_workers=a.default_workers,
            expected_flags=a.expected_flags,
        )
        self.store.save()
        return ToolResult(output=f"已保存平台配置：{self.store.section('platform')}")


class SetRuntimeArgs(BaseModel):
    kind: Literal["local", "docker", "docker-persistent"] | None = Field(default=None, description="工具执行环境 local | docker(临时) | docker-persistent(常驻 foxdog-kali)")
    image: str | None = Field(default=None, description="Docker 工具镜像名")
    docker_binary: str | None = Field(default=None, description="Docker/Podman CLI 路径或命令名")
    network: str | None = Field(default=None, description="容器网络名，通常 bridge")
    mounts: list[str] | None = Field(
        default=None,
        description="额外挂载，Docker volume 格式 host:container[:ro]；不会自动挂载 home/凭据/docker.sock",
    )
    auto_build: bool | None = Field(default=None, description="镜像缺失时自动构建")
    auto_remove: bool | None = Field(default=None, description="run 结束后删除容器")
    workdir: str | None = Field(default=None, description="容器默认目录，默认 /workspace/work")


class SetRuntimeTool(_StoreTool):
    name = "set_runtime"
    description = (
        "配置 shell_exec/外部配方的执行环境。Docker 模式每个 solve run 使用一个共享长生命周期容器；"
        "模型、凭据、文件工具、CAS、FlagGate 和黑板仍留在宿主。"
    )
    args_model = SetRuntimeArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if a.workdir is not None and not a.workdir.startswith("/"):
            return ToolResult(ok=False, error="Docker workdir 必须是容器内绝对路径")
        self.store.set_runtime(**a.model_dump())
        self.store.save()
        return ToolResult(output=f"已保存执行环境配置：{self.store.section('runtime')}（下一次任务生效）")


class RemoveModelEndpointArgs(BaseModel):
    name: str = Field(description="要删除的 extra 端点名（[model.extra.<name>]）")


class RemoveModelEndpointTool(_StoreTool):
    name = "remove_model_endpoint"
    description = "删除一个异构引擎端点（[model.extra.<name>]）。"
    args_model = RemoveModelEndpointArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if not self.store.remove_model_endpoint(a.name):
            return ToolResult(ok=False, error=f"端点 {a.name} 不存在")
        self.store.save()
        return ToolResult(output=f"已删除端点 {a.name}")


class AddMcpServerArgs(BaseModel):
    name: str
    command: str | None = Field(default=None, description="stdio 启动命令")
    args: list[str] | None = Field(default=None, description="stdio 命令参数")
    url: str | None = Field(default=None, description="streamable HTTP 地址")


class AddMcpServerTool(_StoreTool):
    name = "add_mcp_server"
    description = "新增 MCP server（stdio: command+args；或 HTTP: url）。新会话生效。"
    args_model = AddMcpServerArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if not a.command and not a.url:
            return ToolResult(ok=False, error="command（stdio）或 url（HTTP）至少给一个")
        self.store.add_mcp_server(a.name, command=a.command, args=a.args, url=a.url)
        self.store.save()
        return ToolResult(output=f"已保存 MCP server {a.name}（重启会话后生效）")


class RemoveMcpServerArgs(BaseModel):
    name: str


class RemoveMcpServerTool(_StoreTool):
    name = "remove_mcp_server"
    description = "移除 MCP server。"
    args_model = RemoveMcpServerArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if not self.store.remove_mcp_server(a.name):
            return ToolResult(ok=False, error=f"MCP server {a.name} 不存在")
        self.store.save()
        return ToolResult(output=f"已移除 MCP server {a.name}（重启会话后生效）")


class SetAgentArgs(BaseModel):
    mode: str | None = Field(default=None, description="yolo | plan")
    max_steps: int | None = None
    budget_minutes: float | None = Field(default=None, description="预算分钟数（budget.max_minutes）")
    engines_enabled: bool | None = Field(default=None, description="race 异构引擎总开关（多端点+claude CLI）")
    max_race_engines: int | None = Field(default=None, description="race 引擎数上限（默认 3）")


class SetAgentTool(_StoreTool):
    name = "set_agent"
    description = "配置 agent 行为：权限模式、max_steps、预算分钟数、race 异构引擎开关与上限。"
    args_model = SetAgentArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if a.mode is not None and a.mode not in ("yolo", "plan"):
            return ToolResult(ok=False, error="mode 必须是 yolo/plan")
        self.store.set_agent(mode=a.mode, max_steps=a.max_steps, budget_minutes=a.budget_minutes, engines_enabled=a.engines_enabled, max_race_engines=a.max_race_engines)
        self.store.save()
        return ToolResult(output=f"已保存 agent 配置：agent={self.store.section('agent')} budget={self.store.section('budget')}")


class ConfigureClaudeCodeArgs(BaseModel):
    enabled: bool = Field(default=True, description="是否启用 Claude Code race 引擎")
    import_host_config: bool = Field(default=True, description="运行时从本机 ~/.claude/settings.json 注入 CC Switch 环境")
    containerized: bool = Field(default=True, description="在 Docker 工具 runtime 中运行；false 才允许宿主 CLI")


class ConfigureClaudeCodeTool(_StoreTool):
    name = "configure_claude_code"
    description = (
        "配置 Claude Code race 引擎。可检查本机 CC Switch/Claude settings.json，"
        "只保存导入开关，token/端点值不写入 FoxDog 配置且不在输出中回显。"
    )
    args_model = ConfigureClaudeCodeArgs

    def __init__(self, store: ConfigStore, settings_path: str | Path | None = None) -> None:
        super().__init__(store)
        self.settings_path = Path(settings_path) if settings_path is not None else default_claude_settings_path()

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        imported_names: list[str] = []
        if a.enabled and a.import_host_config:
            try:
                imported_names = sorted(load_claude_code_env(self.settings_path))
            except ClaudeConfigError as exc:
                return ToolResult(ok=False, error=str(exc))
        self.store.set_claude_code(
            enabled=a.enabled,
            containerized=a.containerized,
            import_host_config=a.import_host_config,
        )
        self.store.save()
        mode = "Docker 工具 runtime" if a.containerized else "宿主机（高风险）"
        detail = f"；运行时将注入 {', '.join(imported_names)}（值不落盘）" if imported_names else ""
        return ToolResult(output=f"已保存 Claude Code：enabled={a.enabled}，执行位置={mode}{detail}")


class ManageSkillArgs(BaseModel):
    action: Literal["list", "add", "remove"] = Field(description="操作")
    name: str | None = Field(default=None, description="技能名；add 时可省略，使用 manifest.yaml 的 name")
    source: str | None = Field(default=None, description="本地技能目录或 GitHub HTTPS 仓库地址")


class ManageSkillTool(_StoreTool):
    name = "manage_skill"
    description = "管理用户技能：列出、从本地目录/GitHub 添加、或移除。支持单技能（含 SKILL.md，manifest.yaml 可选）与多技能包（递归导入所有含 SKILL.md 的子目录，各注册为独立技能）。"
    args_model = ManageSkillArgs
    accesses = [ToolAccess(resource="state", mode="w"), ToolAccess(resource="network", mode="r")]

    @staticmethod
    def _skill_name(root: Path) -> str | None:
        try:
            data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
            value = data.get("name") if isinstance(data, dict) else None
            return str(value).strip() if value else None
        except Exception:
            return None

    @staticmethod
    def _safe_name(raw: str) -> str:
        """技能名规范化：只留字母数字 -_，其余转 -（多技能包子目录名可能含空格/点）。"""
        return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-").lower() or "skill"

    @classmethod
    def _discover_skills(cls, root: Path) -> list[Path]:
        """发现 root 下所有含 SKILL.md 的技能目录（支持多技能包递归）。

        - 单技能布局（root 或唯一子目录含 SKILL.md）→ 返回该目录。
        - 多技能包（如 reverse-skill：skills/ 下多个场景子目录各带 SKILL.md，
          且 skills/ 本身还有一个路由 SKILL.md）→ 路由目录与各子技能目录都收，
          各自成独立技能。深度上限 4 层，避免扫爆超大仓库。
        """
        found: list[Path] = []
        max_depth = 4

        def _has_child_skill(d: Path) -> bool:
            try:
                return any(
                    c.is_dir() and not c.name.startswith(".") and (c / "SKILL.md").is_file()
                    for c in d.iterdir()
                )
            except OSError:
                return False

        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            has_md = (cur / "SKILL.md").is_file()
            if has_md:
                found.append(cur)
                # 有 SKILL.md 但子目录还有各自 SKILL.md（路由/容器目录）→ 继续下探；
                # 否则是叶子技能，不再递归。
                if not _has_child_skill(cur) or depth >= max_depth:
                    continue
            if depth >= max_depth:
                continue
            try:
                for child in sorted(cur.iterdir()):
                    if child.is_dir() and not child.name.startswith("."):
                        stack.append((child, depth + 1))
            except OSError:
                continue
        return found

    @staticmethod
    def _repo_copy_ignores(dir_path: str, names: list[str]) -> set[str]:
        del dir_path
        ignored = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
        ignored.update(
            name for name in names
            if name.endswith((".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"))
        )
        return ignored

    @staticmethod
    def _bundle_root(root: Path, skill_dirs: list[Path]) -> Path | None:
        """Return root when this is a multi-skill bundle with shared resources."""
        bundle_skills = root / "skills"
        if len(skill_dirs) <= 1 or not (bundle_skills / "SKILL.md").is_file():
            return None
        try:
            return root if all(bundle_skills in d.parents or d == bundle_skills for d in skill_dirs) else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _display_skill_name(skill_dir: Path) -> str:
        try:
            return _synth_manifest(skill_dir, skill_dir / "SKILL.md").name
        except Exception:  # noqa: BLE001
            return skill_dir.name

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        USER_DIR.mkdir(parents=True, exist_ok=True)
        if a.action == "list":
            names = sorted(p.name for p in USER_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())
            return ToolResult(output="用户技能：\n" + ("\n".join(f"- {n}" for n in names) if names else "（无）"))
        if not a.name and a.action == "remove":
            return ToolResult(ok=False, error="remove 必须提供 name")
        if a.action == "remove":
            target = USER_DIR / a.name
            if not target.is_dir():
                return ToolResult(ok=False, error=f"技能不存在：{a.name}")
            shutil.rmtree(target)
            return ToolResult(output=f"已移除技能 {a.name}（下次任务加载）")
        if not a.source:
            return ToolResult(ok=False, error="add 必须提供 source（本地目录或 GitHub HTTPS URL）")
        with tempfile.TemporaryDirectory(prefix="foxdog-skill-") as temp:
            root = Path(temp) / "repo"
            repo_label = a.name or ""
            if a.source.startswith(("https://github.com/", "https://gitlab.com/")):
                proc = subprocess.run(["git", "clone", "--depth", "1", a.source, str(root)], capture_output=True, text=True, timeout=120)
                if proc.returncode:
                    return ToolResult(ok=False, error=f"git clone 失败：{proc.stderr[-500:]}")
                if not repo_label:
                    repo_label = a.source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            else:
                src = Path(a.source).expanduser().resolve()
                if not src.is_dir():
                    return ToolResult(ok=False, error="source 不是本地目录")
                shutil.copytree(src, root)
                if not repo_label:
                    repo_label = src.name

            skill_dirs = self._discover_skills(root)
            if not skill_dirs:
                return ToolResult(ok=False, error="source 中未找到任何含 SKILL.md 的技能目录")

            bundle_root = self._bundle_root(root, skill_dirs)
            if bundle_root is not None:
                bundle_name = self._safe_name(a.name or repo_label)
                target = USER_DIR / bundle_name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(bundle_root, target, ignore=self._repo_copy_ignores)
                imported = [self._display_skill_name(d) for d in skill_dirs]
                head = f"已添加技能包 {bundle_name}，包含 {len(imported)} 个技能（新任务自动发现）：\n"
                body = "\n".join(f"- {n}" for n in imported[:40])
                more = f"\n…共 {len(imported)} 个" if len(imported) > 40 else ""
                return ToolResult(output=head + body + more)

            # 单技能：优先用 manifest name 或用户给的 name；多技能包：<repo>-<子目录>
            single = len(skill_dirs) == 1
            imported: list[str] = []
            skipped: list[str] = []

            def _ignore_child_skills(dir_path: str, names: list[str]) -> set[str]:
                # 容器/路由目录（自身有 SKILL.md 且子目录也有 SKILL.md）导入时，
                # 不把子技能目录一起 copy 进来（它们各自单独导入，避免重复）。
                base = Path(dir_path)
                return {
                    n for n in names
                    if (base / n).is_dir() and (base / n / "SKILL.md").is_file()
                }

            for skill_dir in skill_dirs:
                if single:
                    skill_name = a.name or self._skill_name(skill_dir) or self._safe_name(repo_label)
                else:
                    sub = self._skill_name(skill_dir) or skill_dir.name
                    skill_name = self._safe_name(f"{repo_label}-{sub}")
                skill_name = self._safe_name(skill_name)
                target = USER_DIR / skill_name
                if target.exists():
                    shutil.rmtree(target)
                # 容器目录只 copy 自身文件，叶子技能全量 copy
                is_container = any(
                    other != skill_dir and skill_dir in other.parents
                    for other in skill_dirs
                )
                try:
                    if is_container:
                        shutil.copytree(skill_dir, target, ignore=_ignore_child_skills)
                    else:
                        shutil.copytree(skill_dir, target)
                    imported.append(skill_name)
                except OSError as exc:
                    skipped.append(f"{skill_name}（{type(exc).__name__}）")

            if not imported:
                return ToolResult(ok=False, error="技能导入失败：" + "；".join(skipped))
            head = f"已添加 {len(imported)} 个技能（新任务自动发现，activate_skill 加载）：\n"
            body = "\n".join(f"- {n}" for n in imported[:40])
            more = f"\n…共 {len(imported)} 个" if len(imported) > 40 else ""
            note = ("\n跳过：" + "；".join(skipped)) if skipped else ""
            return ToolResult(output=head + body + more + note)


def register_config_tools(registry, store: ConfigStore, secrets: SecretVault | None = None) -> None:
    """/setting 工具集：无 shell/文件/任意网络，仅放行 DeepSeek 官方模型查询。"""
    for tool in (
        GetConfigTool(store),
        ListDeepSeekModelsTool(store),
        ListModelsTool(store),
        SetModelTool(store, secrets),
        RemoveModelEndpointTool(store),
        SetPlatformTool(store),
        SetRuntimeTool(store),
        AddMcpServerTool(store),
        RemoveMcpServerTool(store),
        SetAgentTool(store),
        ConfigureClaudeCodeTool(store),
        ManageSkillTool(store),
        SavePocTool(),
        ListPocsTool(),
    ):
        registry.register(tool)

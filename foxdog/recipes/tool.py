"""配方工具与渐进披露（借鉴 kimi-code select_tools / codex tool_search）。

- RecipeTool：一份激活配方的运行时形态。参数渲染成 argv 数组
  （command + 渲染参数，经 executor.run_argv 数组传参，禁止 shell 拼接；
  combined 与 additional_args 按 shlex 拆分）。
- SelectToolsTool：无参调用返回全部可用配方的 name+short_description；
  带 names 调用把配方激活并注册进工具表（loop 每 step 重建工具表，下一步生效）。
- RecipeHelpTool：取配方的完整 description（MCP resources/read 双轨制思路）。
"""
from __future__ import annotations

import shlex
from typing import Any

from pydantic import BaseModel, Field, create_model

from ..executor.process import run_argv
from ..tools import Tool, ToolAccess, ToolContext, ToolRegistry, ToolResult
from .loader import Recipe, RecipeManager, RecipeSpec

_TYPE_MAP = {"string": str, "integer": int, "float": float, "boolean": bool}


def _build_args_model(spec: RecipeSpec) -> type[BaseModel]:
    """按配方参数动态生成 pydantic 模型（同时用于 JSON schema 与参数校验）。"""
    fields: dict[str, Any] = {}
    for p in spec.parameters:
        tp = _TYPE_MAP[p.type]
        if p.required:
            fields[p.name] = (tp, Field(description=p.help or p.name))
        else:
            fields[p.name] = (tp | None, Field(default=p.default, description=p.help or p.name))
    if spec.additional_args:
        fields["additional_args"] = (
            str | None,
            Field(default=None, description="额外原始参数（按 shell 分词追加到 argv 末尾）"),
        )
    return create_model(f"{spec.name}_args", **fields)


class RecipeTool(Tool):
    accesses = [ToolAccess(resource="shell", mode="w")]

    def __init__(self, recipe: Recipe) -> None:
        self.recipe = recipe
        self.name = recipe.spec.name
        detail = recipe.spec.description.strip() or recipe.spec.short_description
        self.description = f"[外部工具 {recipe.spec.command}] {detail}".strip()
        self.args_model = _build_args_model(recipe.spec)

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        """把模型参数渲染成 argv 数组（positional 按声明顺序；combined/additional_args shlex 拆分）。"""
        a = self.parse_args(args)
        values = a.model_dump()
        spec = self.recipe.spec
        argv = [spec.command]
        for p in spec.parameters:
            v = values.get(p.name)
            if v is None:
                continue
            if p.mapping == "positional":
                argv.append(str(v))
            elif p.mapping == "combined":
                argv.extend(shlex.split(str(v)))
            elif p.mapping == "flag":
                flag = p.flag or f"--{p.name}"
                if isinstance(v, bool):
                    if v:
                        argv.append(flag)
                else:
                    argv.extend([flag, str(v)])
        if spec.additional_args and values.get("additional_args"):
            argv.extend(shlex.split(str(values["additional_args"])))
        return argv

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            argv = self.build_argv(args)
        except (ValueError, TypeError) as exc:
            return ToolResult(ok=False, error=f"参数渲染失败: {exc}")
        runtime = getattr(ctx, "runtime", None)
        argv_runner = runtime.run_argv if runtime is not None else run_argv
        res = await argv_runner(
            argv,
            cwd=ctx.workspace.work_dir,
            idle_timeout=min(300.0, self.recipe.spec.timeout),
            total_timeout=self.recipe.spec.timeout,
            cas_store=ctx.workspace.cas_store,
        )
        if res.error:
            return ToolResult(ok=False, error=res.error)
        note = ""
        if res.idle_timed_out:
            note = "[无输出空闲超时，已 kill]\n"
        elif res.timed_out:
            note = "[总时限超时，已 kill]\n"
        return ToolResult(
            ok=res.exit_code == 0 and not (res.timed_out or res.idle_timed_out),
            output=f"{note}exit_code={res.exit_code}\n{res.output}",
            handle=res.handle,
            data={"argv": argv, "exit_code": res.exit_code},
        )


class SelectToolsArgs(BaseModel):
    names: list[str] | None = Field(
        default=None, description="要激活的配方工具名列表；不传则返回全部可用配方目录"
    )


class SelectToolsTool(Tool):
    name = "select_tools"
    description = (
        "外部工具渐进披露：无参调用列出全部可用工具（YAML 配方 + MCP 工具，name+一句话说明）；"
        "传 names=[...] 把对应工具的完整 schema 激活进工具表（下一 step 可用）。"
    )
    accesses = [ToolAccess(resource="meta", mode="w")]
    args_model = SelectToolsArgs

    def __init__(self, manager: RecipeManager, registry: ToolRegistry, mcp=None) -> None:
        self.manager = manager
        self.registry = registry
        self.mcp = mcp  # McpManager | None（未配置 MCP 时 None）

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if not a.names:
            catalog = self.manager.catalog()
            if self.mcp is not None and self.mcp.names:
                lines = ["", "MCP 工具（外部 server 提供）："]
                lines += [f"- {n}: {d}" for n, d in self.mcp.catalog_entries()]
                catalog += "\n".join(lines)
            return ToolResult(output=catalog)
        recipes, unknown = self.manager.activate(a.names)
        lines: list[str] = []
        for r in recipes:
            tool = RecipeTool(r)
            self.registry.register(tool)
            params = "、".join(
                f"{p.name}({p.mapping}{',必填' if p.required else ''})" for p in r.spec.parameters
            )
            lines.append(f"已激活 {r.name}: {r.spec.short_description}（参数: {params or '无'}）")
        # 配方里没找到的交给 MCP
        if unknown and self.mcp is not None:
            activated, unknown = self.mcp.activate(unknown, self.registry)
            for n in activated:
                lines.append(f"已激活 {n}（MCP 工具）")
        if unknown:
            lines.append("未知工具: " + "、".join(unknown))
        lines.append("激活的工具已加入工具表，本 turn 下一 step 起可直接调用。")
        return ToolResult(ok=not unknown, output="\n".join(lines))


class RecipeHelpArgs(BaseModel):
    name: str = Field(description="配方工具名")


class RecipeHelpTool(Tool):
    name = "recipe_help"
    description = "查看外部工具配方的完整说明（参数、示例、注意事项）。"
    accesses = [ToolAccess(resource="meta", mode="r")]
    args_model = RecipeHelpArgs

    def __init__(self, manager: RecipeManager) -> None:
        self.manager = manager

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        r = self.manager.recipes.get(a.name)
        if r is None:
            return ToolResult(ok=False, error=f"未知配方 {a.name!r}（未安装或未加载）")
        spec = r.spec
        lines = [f"# {spec.name}（命令: {spec.command}，超时 {spec.timeout}s）", spec.description.strip()]
        if spec.parameters:
            lines.append("参数：")
            for p in spec.parameters:
                bits = [p.mapping]
                bits.append("必填" if p.required else f"默认 {p.default!r}")
                if p.flag:
                    bits.append(f"旗标 {p.flag}")
                lines.append(f"- {p.name}: {p.help or ''}（{'，'.join(bits)}）")
        if spec.additional_args:
            lines.append("支持 additional_args 传任意额外参数。")
        return ToolResult(output="\n".join(lines))

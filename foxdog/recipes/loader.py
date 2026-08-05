"""配方加载器：扫描内置 builtin/*.yaml + 用户 ~/.config/foxdog/recipes/*.yaml。

解析规格：name/command/short_description/description/parameters[mapping:
flag|positional|combined, default, required, flag]/additional_args/timeout。
shutil.which 检测 command，未安装跳过并记录原因；同名配方用户目录覆盖内置。

加载成功的配方不进默认工具表（渐进披露）：由 select_tools 工具激活，
见 recipes/tool.py。
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

BUILTIN_DIR = Path(__file__).parent / "builtin"
USER_DIR = Path.home() / ".config" / "foxdog" / "recipes"


class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "float", "boolean"] = "string"
    required: bool = False
    default: str | int | float | bool | None = None
    mapping: Literal["flag", "positional", "combined"] = "flag"
    flag: str | None = None  # mapping=flag 时的旗标串，缺省 --{name}
    help: str = ""


class RecipeSpec(BaseModel):
    """一份 YAML 配方文件。"""

    name: str
    command: str
    short_description: str = ""
    description: str = ""
    parameters: list[ParamSpec] = Field(default_factory=list)
    additional_args: bool = False  # 允许模型传 additional_args 原始参数兜底
    timeout: float = 300.0


class Recipe(BaseModel):
    """检测通过、可激活的配方。"""

    spec: RecipeSpec
    source: str  # builtin | user

    @property
    def name(self) -> str:
        return self.spec.name


class RecipeManager:
    """持有全部可用配方与激活状态；select_tools 的后端。"""

    def __init__(self, recipes: list[Recipe], skipped: list[str] | None = None) -> None:
        self.recipes = {r.name: r for r in recipes}
        self.skipped = skipped or []
        self.activated: set[str] = set()

    def catalog(self) -> str:
        """无参 select_tools 的目录文本：name + short_description。"""
        lines = ["可用外部工具配方（select_tools(names=[...]) 激活完整 schema 后使用）："]
        for r in sorted(self.recipes.values(), key=lambda r: r.name):
            mark = "（已激活）" if r.name in self.activated else ""
            lines.append(f"- {r.name}: {r.spec.short_description}{mark}")
        if self.skipped:
            lines.append("未安装已跳过：" + "、".join(self.skipped))
        lines.append("详细用法：recipe_help(name) 或激活后查看工具描述。")
        return "\n".join(lines)

    def activate(self, names: list[str]) -> tuple[list[Recipe], list[str]]:
        """激活配方，返回 (成功列表, 未知名列表)。"""
        ok: list[Recipe] = []
        unknown: list[str] = []
        for n in names:
            r = self.recipes.get(n)
            if r is None:
                unknown.append(n)
            else:
                self.activated.add(n)
                ok.append(r)
        return ok, unknown


def parse_recipe(path: Path, source: str) -> RecipeSpec:
    """解析单份 YAML；格式非法抛异常（调用方捕获记录）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配方顶层必须是 mapping")
    return RecipeSpec(**data)


def load_recipes(
    builtin_dir: Path | None = None,
    user_dir: Path | None = None,
    command_available: Callable[[str], bool] | None = None,
) -> tuple[list[Recipe], list[str]]:
    """扫描两个目录，返回 (可用配方, 跳过原因列表)。

    ``command_available`` 可由执行 runtime 注入，使配方按实际执行环境而非
    FoxDog 宿主机的 PATH 判定；缺省仍保持原有 ``shutil.which`` 行为。
    """
    if command_available is None:
        command_available = lambda command: shutil.which(command) is not None  # noqa: E731

    specs: dict[str, tuple[RecipeSpec, str]] = {}
    skipped: list[str] = []
    for directory, source in (
        (builtin_dir if builtin_dir is not None else BUILTIN_DIR, "builtin"),
        (user_dir if user_dir is not None else USER_DIR, "user"),
    ):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                spec = parse_recipe(path, source)
            except Exception as exc:  # noqa: BLE001 - 坏配方不致命，记录跳过
                skipped.append(f"{path.name}（解析失败: {exc}）")
                continue
            specs[spec.name] = (spec, source)  # 用户目录后扫描，覆盖同名内置

    recipes: list[Recipe] = []
    for spec, source in specs.values():
        if not command_available(spec.command):
            skipped.append(f"{spec.name}（未安装: {spec.command}）")
            continue
        recipes.append(Recipe(spec=spec, source=source))
    return recipes, skipped

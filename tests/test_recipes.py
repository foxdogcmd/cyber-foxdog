"""YAML 配方：加载（PATH 注入假可执行文件）、参数渲染（三种 mapping + additional_args）、
select_tools 渐进披露激活流程、未安装跳过。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from foxdog.recipes.loader import Recipe, RecipeManager, RecipeSpec, load_recipes
from foxdog.recipes.tool import RecipeHelpTool, RecipeTool, SelectToolsTool
from foxdog.executor.process import ExecResult
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry
from foxdog.executor.background import BackgroundRegistry

FAKE_YAML = """\
name: fake_scan
command: faketool
short_description: 假扫描器
description: |
  详细说明：这是测试用假工具。
parameters:
  - name: target
    type: string
    required: true
    mapping: positional
  - name: level
    type: integer
    default: 1
    mapping: flag
    flag: "-l"
  - name: flags
    type: string
    mapping: combined
additional_args: true
timeout: 60
"""

PY_YAML = f"""\
name: pytool
command: {sys.executable.replace(chr(92), "/")}
short_description: python 假工具
parameters:
  - name: code
    type: string
    required: true
    mapping: combined
"""


def _make_fake_tool(tmp_path: Path) -> Path:
    """造一个假的可执行文件进临时目录，返回目录（由调用方注入 PATH）。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if sys.platform == "win32":
        (bindir / "faketool.bat").write_text("@echo faketool %*\n", encoding="utf-8")
    else:
        script = bindir / "faketool"
        script.write_text("#!/bin/sh\necho faketool \"$@\"\n", encoding="utf-8")
        script.chmod(0o755)
    return bindir


def test_load_with_path_injection(tmp_path, monkeypatch):
    bindir = _make_fake_tool(tmp_path)
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "fake.yaml").write_text(FAKE_YAML, encoding="utf-8")
    (recipes_dir / "gone.yaml").write_text(
        "name: gone_tool\ncommand: definitely-not-exist-xyz-123\nshort_description: 不存在\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    recipes, skipped = load_recipes(builtin_dir=recipes_dir, user_dir=tmp_path / "empty")
    names = [r.name for r in recipes]
    assert "fake_scan" in names
    assert "gone_tool" not in names  # 未安装跳过
    assert any("gone_tool" in s and "未安装" in s for s in skipped)


def test_builtin_recipes_parse():
    """内置 10 个配方全部能解析（本机装没装不影响解析）。"""
    recipes, skipped = load_recipes(user_dir=Path("/nonexistent-foxdog-test"))
    loaded = {r.name for r in recipes}
    skipped_names = {s.split("（")[0] for s in skipped}
    expected = {"nmap_scan", "ffuf", "gobuster", "sqlmap", "hydra", "john", "hashcat", "binwalk", "nikto", "dirsearch"}
    assert expected <= loaded | skipped_names


def test_load_uses_injected_command_availability(tmp_path):
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "fake.yaml").write_text(FAKE_YAML, encoding="utf-8")
    (recipes_dir / "gone.yaml").write_text(
        "name: gone_tool\ncommand: unavailable-in-runtime\nshort_description: 不存在\n",
        encoding="utf-8",
    )
    checked: list[str] = []

    def is_available(command: str) -> bool:
        checked.append(command)
        return command == "faketool"

    recipes, skipped = load_recipes(
        builtin_dir=recipes_dir,
        user_dir=tmp_path / "empty",
        command_available=is_available,
    )

    assert [recipe.name for recipe in recipes] == ["fake_scan"]
    assert checked == ["faketool", "unavailable-in-runtime"]
    assert any("gone_tool" in reason and "未安装" in reason for reason in skipped)


def _recipe(mapping_extra: list[dict] | None = None) -> Recipe:
    spec = RecipeSpec(
        name="t",
        command="tool",
        parameters=[
            {"name": "pos", "type": "string", "required": True, "mapping": "positional"},
            {"name": "num", "type": "integer", "mapping": "flag", "flag": "-n"},
            {"name": "verbose", "type": "boolean", "mapping": "flag"},
            {"name": "opts", "type": "string", "mapping": "combined"},
        ]
        + (mapping_extra or []),
        additional_args=True,
    )
    return Recipe(spec=spec, source="builtin")


def test_build_argv_three_mappings_and_additional():
    tool = RecipeTool(_recipe())
    argv = tool.build_argv(
        {
            "pos": "10.0.0.1",
            "num": 3,
            "verbose": True,
            "opts": '--deep "a b" -x',
            "additional_args": "--tail=5 rest",
        }
    )
    assert argv == ["tool", "10.0.0.1", "-n", "3", "--verbose", "--deep", "a b", "-x", "--tail=5", "rest"]


def test_build_argv_defaults_and_omitted():
    tool = RecipeTool(_recipe())
    # 只传必填：可选项一律不出现；bool False 不出现
    argv = tool.build_argv({"pos": "t", "verbose": False})
    assert argv == ["tool", "t"]


async def test_recipe_tool_executes_argv(tmp_path):
    """用 python 自己当假命令，验证数组传参执行（-c 经 combined 分词）。"""
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "py.yaml").write_text(PY_YAML, encoding="utf-8")
    recipes, skipped = load_recipes(builtin_dir=recipes_dir, user_dir=tmp_path / "empty")
    assert not skipped and len(recipes) == 1
    tool = RecipeTool(recipes[0])
    ws = Workspace(base=tmp_path, run_id="recipe-exec")
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )
    result = await tool.run({"code": "-c \"print('hello-recipe')\""}, ctx)
    assert result.ok, result.error
    assert "hello-recipe" in result.output
    assert result.data["argv"][:1] == [sys.executable.replace("\\", "/")]


async def test_recipe_tool_uses_runtime_run_argv(tmp_path):
    class FakeRuntime:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict]] = []

        async def run_argv(self, argv: list[str], **kwargs) -> ExecResult:
            self.calls.append((argv, kwargs))
            return ExecResult(exit_code=0, output="runtime-result\n", handle="cas-runtime")

    ws = Workspace(base=tmp_path, run_id="recipe-runtime")
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )
    runtime = FakeRuntime()
    ctx.runtime = runtime

    result = await RecipeTool(_recipe()).run({"pos": "target"}, ctx)

    assert result.ok
    assert result.output == "exit_code=0\nruntime-result\n"
    assert result.handle == "cas-runtime"
    assert len(runtime.calls) == 1
    argv, kwargs = runtime.calls[0]
    assert argv == ["tool", "target"]
    assert kwargs["cwd"] == ws.work_dir
    assert kwargs["idle_timeout"] == 300.0
    assert kwargs["total_timeout"] == 300.0
    assert kwargs["cas_store"] == ws.cas_store


async def test_select_tools_activation_flow(tmp_path):
    spec = RecipeSpec(name="fake_scan", command="faketool", short_description="假扫描器",
                      description="详细说明文本", parameters=[])
    manager = RecipeManager([Recipe(spec=spec, source="builtin")], skipped=["gone_tool（未安装: gone）"])
    registry = ToolRegistry()
    select = SelectToolsTool(manager, registry)
    helper = RecipeHelpTool(manager)
    ctx = ToolContext(
        workspace=Workspace(base=tmp_path, run_id="select-tools"),
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )

    # 1. 无参 → 目录含 name + short_description + 跳过项
    catalog = await select.run({}, ctx)
    assert "fake_scan" in catalog.output and "假扫描器" in catalog.output
    assert "gone_tool" in catalog.output
    assert registry.get("fake_scan") is None  # 未激活不进工具表

    # 2. recipe_help 拿详细描述
    help_res = await helper.run({"name": "fake_scan"}, ctx)
    assert "详细说明文本" in help_res.output

    # 3. 激活 → 注册进工具表
    act = await select.run({"names": ["fake_scan", "nope"]}, ctx)
    assert "已激活 fake_scan" in act.output and "nope" in act.output
    assert registry.get("fake_scan") is not None
    assert any(t["function"]["name"] == "fake_scan" for t in registry.openai_tools())

    # 4. 重复激活幂等
    act2 = await select.run({"names": ["fake_scan"]}, ctx)
    assert act2.ok

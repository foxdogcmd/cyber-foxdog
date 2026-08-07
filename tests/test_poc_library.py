"""save_poc/list_pocs：实战漏洞知识自动沉淀为用户 skill。"""
from __future__ import annotations

from foxdog.config import ConfigStore
from foxdog.core import prompts
from foxdog.executor.background import BackgroundRegistry
from foxdog.skills.loader import load_skills
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry, register_builtin_tools
from foxdog.tools.builtin import poc_library
from foxdog.tools.builtin.config_edit import register_config_tools


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(
        workspace=Workspace(base=tmp_path, run_id="poc-test"),
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )


async def test_save_poc_generates_discoverable_skill(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(poc_library, "USER_DIR", skills_dir)

    registry = ToolRegistry()
    register_builtin_tools(registry)
    save = registry.get("save_poc")
    list_pocs = registry.get("list_pocs")
    assert save is not None and list_pocs is not None

    res = await save.run(
        {
            "name": "CVE-2024-12345 Oracle RCE",
            "title": "Oracle 示例 RCE",
            "cve": "CVE-2024-12345",
            "product": "Oracle Example",
            "vuln_type": "RCE",
            "description": "版本小于 x 时可用恶意请求触发。",
            "evidence": "HTTP/1.1 200 OK\nflag 未出现在知识库",
            "poc_code": "import requests\nrequests.get(target)\n",
            "references": ["https://example.com/advisory"],
            "requires_tools": ["python3"],
        },
        _ctx(tmp_path),
    )
    assert res.ok, res.error

    root = skills_dir / "cve-2024-12345-oracle-rce"
    assert (root / "manifest.yaml").is_file()
    assert (root / "SKILL.md").is_file()
    assert (root / "poc.py").is_file()
    assert (root / "references" / "evidence.txt").is_file()

    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    loaded = load_skills(
        is_tool_available=lambda n: True,
        builtin_dir=builtin_dir,
        user_dir=skills_dir,
    )
    names = {s.name for s in loaded}
    assert "cve-2024-12345-oracle-rce" in names

    listed = await list_pocs.run({}, _ctx(tmp_path))
    assert listed.ok
    assert "cve-2024-12345-oracle-rce" in listed.output


async def test_save_poc_requires_overwrite(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(poc_library, "USER_DIR", skills_dir)
    registry = ToolRegistry()
    register_builtin_tools(registry)
    save = registry.get("save_poc")

    args = {"name": "same-poc", "title": "Same", "description": "x"}
    first = await save.run(args, _ctx(tmp_path))
    assert first.ok

    second = await save.run(args, _ctx(tmp_path))
    assert not second.ok and "已存在" in second.error

    overwrite = await save.run({**args, "title": "Updated", "overwrite": True}, _ctx(tmp_path))
    assert overwrite.ok
    skill = (skills_dir / "same-poc" / "SKILL.md").read_text(encoding="utf-8")
    assert "Updated" in skill


def test_config_registry_includes_save_poc(tmp_path):
    registry = ToolRegistry()
    register_config_tools(registry, ConfigStore(tmp_path / "config.toml"))
    assert registry.get("save_poc") is not None
    assert registry.get("list_pocs") is not None


def test_system_prompt_mentions_knowledge_saving():
    prompt = prompts.build_system_prompt()
    assert "save_poc" in prompt
    assert "list_pocs" in prompt

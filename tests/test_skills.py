"""技能系统：loader、activate_skill 注入、requires.tools 未满足标记不可用。"""
from __future__ import annotations

from pathlib import Path

from foxdog.executor.background import BackgroundRegistry
from foxdog.skills.loader import SkillRegistry, load_skills
from foxdog.skills.tool import ActivateSkillTool
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(
        workspace=Workspace(base=tmp_path, run_id="skills-test"),
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
    )


def test_builtin_skills_loaded():
    skills = load_skills(is_tool_available=lambda n: True)
    names = {s.name for s in skills}
    assert {"php-file-write-waf", "web-recon", "sqli-basics"} <= names
    for s in skills:
        assert s.description
        assert s.skill_md.is_file()
    legacy = {s.name: s for s in skills}
    for name in ("php-file-write-waf", "web-recon", "sqli-basics"):
        # 手册正文要真材实料（80 行以上）
        lines = legacy[name].skill_md.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 80, f"{name} 只有 {len(lines)} 行"


def test_builtin_reverse_skill_bundle_loaded():
    skills = load_skills(is_tool_available=lambda n: True)
    by_name = {s.name: s for s in skills}
    assert "reverse-skill-router" in by_name
    assert "apk-reverse" in by_name
    assert "ida-reverse" in by_name
    assert "Routes reverse engineering" in by_name["reverse-skill-router"].description
    assert by_name["apk-reverse"].description.startswith("在 CLI 环境下做 Android APK 逆向")
    assert by_name["apk-reverse"].resource_root.name == "reverse-skill"
    assert (by_name["apk-reverse"].resource_root / "LICENSE").is_file()


def test_builtin_secskills_ctf_bundle_loaded():
    skills = load_skills(is_tool_available=lambda n: True)
    by_name = {s.name: s for s in skills}
    for name in ("ctf-web", "ctf-crypto", "ctf-pwn", "ctf-forensics", "ctf-misc", "ctf-reverse"):
        assert name in by_name, f"missing builtin ctf skill {name}"
        assert by_name[name].resource_root.name == "secskills-ctf"
        assert (by_name[name].resource_root / "LICENSE").is_file()
    assert "semgrep-code-audit" in by_name
    assert "semgrep" in by_name["semgrep-code-audit"].missing_tools or by_name["semgrep-code-audit"].available


def test_builtin_poc_library_skill_loaded():
    skills = load_skills(is_tool_available=lambda n: True)
    by_name = {s.name: s for s in skills}
    assert "poc-library" in by_name
    text = by_name["poc-library"].skill_md.read_text(encoding="utf-8")
    assert "~/.config/foxdog/skills/<poc-id>/" in text
    assert "manage_skill" in text
    assert "nuclei" in text


async def test_activate_skill_reads_bundle_resource(tmp_path):
    skills = load_skills(is_tool_available=lambda n: True)
    tool = ActivateSkillTool(SkillRegistry(skills))

    res = await tool.run({"name": "apk-reverse"}, _ctx(tmp_path))
    assert res.ok, res.error
    assert "APK 逆向 CLI 作业规范" in res.output
    assert "activate_skill(name='apk-reverse', resource='相对路径')" in res.output

    res = await tool.run({"name": "apk-reverse", "resource": "../field-journal/precedent-reverse.md"}, _ctx(tmp_path))
    assert res.ok, res.error
    assert "precedent-reverse.md" in res.output

    res = await tool.run({"name": "apk-reverse", "resource": "../../LICENSE"}, _ctx(tmp_path))
    assert res.ok, res.error
    assert "MIT License" in res.output

    res = await tool.run({"name": "apk-reverse", "resource": "../../../pyproject.toml"}, _ctx(tmp_path))
    assert not res.ok and "越界" in res.error


def test_requires_tools_unavailable(tmp_path):
    root = tmp_path / "skills"
    skill_dir = root / "needs-nmap"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.yaml").write_text(
        "name: needs-nmap\ndescription: 需要 nmap\nwhen: 扫描时\nrequires:\n  tools: [nmap]\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# 内容\n", encoding="utf-8")

    skills = load_skills(
        is_tool_available=lambda n: False,  # 什么工具都没有
        builtin_dir=root,
        user_dir=tmp_path / "empty",
    )
    assert len(skills) == 1
    s = skills[0]
    assert not s.available and s.missing_tools == ["nmap"]


async def test_activate_skill_injects_full_text(tmp_path):
    skills = load_skills(is_tool_available=lambda n: True)
    registry = SkillRegistry(skills)
    tool = ActivateSkillTool(registry)
    ctx = _ctx(tmp_path)

    res = await tool.run({"name": "php-file-write-waf"}, ctx)
    assert res.ok, res.error
    # SKILL.md 全文作为 tool result 注入上下文
    assert "session upload progress" in res.output
    assert "auto_prepend_file" in res.output
    assert "PHP_SESSION_UPLOAD_PROGRESS" in res.output

    # 未知技能报错并列出可用清单
    res = await tool.run({"name": "nope"}, ctx)
    assert not res.ok and "未知技能" in res.error and "web-recon" in res.error


async def test_activate_unavailable_skill_rejected(tmp_path):
    root = tmp_path / "skills"
    skill_dir = root / "needs-tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.yaml").write_text(
        "name: needs-tool\ndescription: x\nrequires:\n  tools: [definitely-missing-xyz]\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# 内容\n", encoding="utf-8")
    skills = load_skills(is_tool_available=lambda n: False, builtin_dir=root, user_dir=tmp_path / "empty")
    tool = ActivateSkillTool(SkillRegistry(skills))
    res = await tool.run({"name": "needs-tool"}, _ctx(tmp_path))
    assert not res.ok and "缺少工具" in res.error and "definitely-missing-xyz" in res.error


def test_load_skills_synthesizes_manifest_when_absent(tmp_path):
    """无 manifest.yaml 的技能目录：用目录名 + SKILL.md 首行标题合成 manifest。"""
    root = tmp_path / "skills"
    skill_dir = root / "ida-reverse"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# IDA 逆向工作流\n\n打开二进制……\n", encoding="utf-8")
    skills = load_skills(is_tool_available=lambda n: True, builtin_dir=root, user_dir=tmp_path / "empty")
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "ida-reverse"
    assert s.description == "IDA 逆向工作流"
    assert s.available


def test_load_skills_parses_frontmatter_manifest(tmp_path):
    root = tmp_path / "skills"
    skill_dir = root / "frontmatter-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: fm-name\n"
        "description: frontmatter description\n"
        "requires:\n"
        "  tools: [jadx]\n"
        "---\n"
        "# Ignored Title\n",
        encoding="utf-8",
    )
    skills = load_skills(is_tool_available=lambda n: n == "jadx", builtin_dir=root, user_dir=tmp_path / "empty")
    assert len(skills) == 1
    assert skills[0].name == "fm-name"
    assert skills[0].description == "frontmatter description"
    assert skills[0].available


async def test_load_skills_discovers_bundle_and_reads_resources(tmp_path):
    root = tmp_path / "builtin"
    bundle = root / "reverse-skill"
    (bundle / "skills" / "field-journal").mkdir(parents=True)
    (bundle / "skills" / "apk-reverse").mkdir(parents=True)
    (bundle / "skills" / "SKILL.md").write_text(
        "---\nname: reverse-skill-router\ndescription: router\n---\n# Router\n",
        encoding="utf-8",
    )
    (bundle / "skills" / "apk-reverse" / "SKILL.md").write_text(
        "---\nname: apk-reverse\ndescription: apk\n---\n# APK\n",
        encoding="utf-8",
    )
    (bundle / "skills" / "field-journal" / "precedent-reverse.md").write_text("precedent", encoding="utf-8")
    (bundle / "LICENSE").write_text("license", encoding="utf-8")

    skills = load_skills(is_tool_available=lambda n: True, builtin_dir=root, user_dir=tmp_path / "empty")
    by_name = {s.name: s for s in skills}
    assert {"reverse-skill-router", "apk-reverse"} <= set(by_name)
    assert by_name["apk-reverse"].resource_root == bundle
    tool = ActivateSkillTool(SkillRegistry(skills))
    res = await tool.run({"name": "apk-reverse", "resource": "../field-journal/precedent-reverse.md"}, _ctx(tmp_path))
    assert res.ok and "precedent" in res.output


async def test_manage_skill_imports_multi_skill_repo(tmp_path):
    """多技能包（reverse-skill 布局）：递归导入所有含 SKILL.md 的子目录，各成独立技能。"""
    import foxdog.skills.loader as loader_mod
    from foxdog.config import ConfigStore
    from foxdog.tools.builtin.config_edit import ManageSkillTool

    # 造一个 reverse-skill 风格的本地仓库：skills/ 下多个场景子目录各带 SKILL.md，顶层无 manifest
    repo = tmp_path / "reverse-skill"
    (repo / "skills" / "apk-reverse").mkdir(parents=True)
    (repo / "skills" / "apk-reverse" / "SKILL.md").write_text("# APK 逆向\n步骤……\n", encoding="utf-8")
    (repo / "skills" / "ida-reverse").mkdir(parents=True)
    (repo / "skills" / "ida-reverse" / "SKILL.md").write_text("# IDA 逆向\n步骤……\n", encoding="utf-8")
    (repo / "skills" / "SKILL.md").write_text("# 路由索引\n主入口……\n", encoding="utf-8")

    # 把用户技能目录指到 tmp，避免污染真实 ~/.config
    user_dir = tmp_path / "user-skills"
    monkey_dir = user_dir
    orig_user_dir = loader_mod.USER_DIR
    import foxdog.tools.builtin.config_edit as ce
    ce.USER_DIR = monkey_dir
    try:
        tool = ManageSkillTool(ConfigStore(tmp_path / "c.toml"))
        ctx = _ctx(tmp_path)
        res = await tool.run({"action": "add", "source": str(repo)}, ctx)
        assert res.ok, res.error
        # 顶层 routing SKILL.md + 两个场景子技能保留在一个 bundle，避免相对资源路径断裂。
        imported_dirs = sorted(p.name for p in monkey_dir.iterdir() if p.is_dir())
        assert imported_dirs == ["reverse-skill"]
        assert (monkey_dir / "reverse-skill" / "skills" / "apk-reverse" / "SKILL.md").is_file()
        assert "reverse-skill" in res.output and "apk-reverse" in res.output and "ida-reverse" in res.output
    finally:
        ce.USER_DIR = orig_user_dir

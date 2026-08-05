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
        # 手册正文要真材实料（80 行以上）
        lines = s.skill_md.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 80, f"{s.name} 只有 {len(lines)} 行"


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
        # 顶层 routing SKILL.md + 两个场景子技能 = 至少 3 个（前缀 reverse-skill-）
        imported = sorted(p.name for p in monkey_dir.iterdir() if p.is_dir())
        assert any("apk-reverse" in n for n in imported), imported
        assert any("ida-reverse" in n for n in imported), imported
        assert len(imported) >= 3, imported
    finally:
        ce.USER_DIR = orig_user_dir

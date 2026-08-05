"""技能加载器：扫描内置 skills/builtin/<name>/ + 用户 ~/.config/foxdog/skills/<name>/。

每个技能一个目录：
    manifest.yaml   name/description/when(触发场景)/requires.tools（可选）
    SKILL.md        正文（activate_skill 注入上下文）

manifest.yaml 缺失时：用目录名 + SKILL.md 首个标题自动合成 name/description
（支持 reverse-skill 这类无 manifest 的多技能包，见 ManageSkillTool 的递归导入）。

系统提示只列 name+description（渐进披露）；requires.tools 未满足标记不可用。
"""
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

BUILTIN_DIR = Path(__file__).parent / "builtin"
USER_DIR = Path.home() / ".config" / "foxdog" / "skills"


class SkillManifest(BaseModel):
    name: str
    description: str = ""
    when: str = ""  # 触发场景描述
    requires: dict[str, list[str]] = Field(default_factory=dict)  # requires.tools


def _synth_manifest(skill_dir: Path, skill_md: Path) -> SkillManifest:
    """无 manifest.yaml 时从目录名 + SKILL.md 首行标题合成 manifest。"""
    name = skill_dir.name
    desc = ""
    try:
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # 首个 markdown 标题或首个非空行当描述
            desc = re.sub(r"^#+\s*", "", stripped)[:200]
            break
    except Exception:  # noqa: BLE001
        pass
    return SkillManifest(name=name, description=desc or name)


class SkillInfo(BaseModel):
    manifest: SkillManifest
    skill_md: Path
    source: str  # builtin | user
    available: bool = True
    missing_tools: list[str] = Field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def description(self) -> str:
        return self.manifest.description


def load_skills(
    is_tool_available: Callable[[str], bool] | None = None,
    builtin_dir: Path | None = None,
    user_dir: Path | None = None,
) -> list[SkillInfo]:
    """扫描两个根目录，按 requires.tools 标记可用性；同名用户技能覆盖内置。"""
    if is_tool_available is None:
        # 缺省：内置工具名不算（由调用方传入含 registry 的判断），只看外部命令
        is_tool_available = lambda name: shutil.which(name) is not None  # noqa: E731

    found: dict[str, SkillInfo] = {}
    for root, source in (
        (builtin_dir if builtin_dir is not None else BUILTIN_DIR, "builtin"),
        (user_dir if user_dir is not None else USER_DIR, "user"),
    ):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            manifest_path = child / "manifest.yaml"
            skill_md = child / "SKILL.md"
            if not child.is_dir() or not skill_md.is_file():
                continue
            try:
                if manifest_path.is_file():
                    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                    manifest = SkillManifest(**data)
                else:
                    # 无 manifest：从目录名 + SKILL.md 首行标题合成（多技能包子技能）
                    manifest = _synth_manifest(child, skill_md)
            except Exception:  # noqa: BLE001 - 坏技能跳过
                continue
            missing = [t for t in manifest.requires.get("tools", []) if not is_tool_available(t)]
            found[manifest.name] = SkillInfo(
                manifest=manifest,
                skill_md=skill_md,
                source=source,
                available=not missing,
                missing_tools=missing,
            )
    return list(found.values())


class SkillRegistry:
    """技能目录：系统提示列 name+description；activate 读 SKILL.md 全文。"""

    def __init__(self, skills: list[SkillInfo]) -> None:
        self.skills = {s.name: s for s in skills}

    def get(self, name: str) -> SkillInfo | None:
        return self.skills.get(name)

    def list(self) -> list[SkillInfo]:
        return list(self.skills.values())

    def activate(self, name: str) -> tuple[bool, str]:
        """返回 (ok, SKILL.md 全文或错误信息)。"""
        s = self.skills.get(name)
        if s is None:
            return False, f"未知技能 {name!r}。可用: {', '.join(sorted(self.skills))}"
        if not s.available:
            return False, f"技能 {name} 不可用：缺少工具 {', '.join(s.missing_tools)}"
        content = s.skill_md.read_text(encoding="utf-8")
        return True, f"[技能手册 {name} 已加载]\n{content}"

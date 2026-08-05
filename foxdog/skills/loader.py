"""技能加载器：扫描内置 skills/builtin/<name>/ + 用户 ~/.config/foxdog/skills/<name>/。

每个技能一个目录：
    manifest.yaml   name/description/when(触发场景)/requires.tools（可选）
    SKILL.md        正文（activate_skill 注入上下文）

manifest.yaml 缺失时：用目录名 + SKILL.md 首个标题自动合成 name/description
（支持 SKILL.md YAML frontmatter 与 reverse-skill 这类多技能 bundle）。

系统提示只列 name+description（渐进披露）；requires.tools 未满足标记不可用。
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

BUILTIN_DIR = Path(__file__).parent / "builtin"
USER_DIR = Path.home() / ".config" / "foxdog" / "skills"
MAX_SKILL_RESOURCE_CHARS = 80_000


class SkillManifest(BaseModel):
    name: str
    description: str = ""
    when: str = ""  # 触发场景描述
    requires: dict[str, list[str]] = Field(default_factory=dict)  # requires.tools


def _frontmatter_manifest(skill_dir: Path, skill_md: Path) -> SkillManifest | None:
    """Parse Codex-style YAML frontmatter from SKILL.md when present."""
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return None
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("name", skill_dir.name)
    return SkillManifest(**data)


def _synth_manifest(skill_dir: Path, skill_md: Path) -> SkillManifest:
    """无 manifest.yaml/frontmatter 时从目录名 + SKILL.md 首个正文标题合成 manifest。"""
    fm = _frontmatter_manifest(skill_dir, skill_md)
    if fm is not None:
        return fm
    name = skill_dir.name
    desc = ""
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
        in_frontmatter = lines[:1] == ["---"]
        for line in lines[1:] if in_frontmatter else lines:
            stripped = line.strip()
            if in_frontmatter and stripped == "---":
                in_frontmatter = False
                continue
            if in_frontmatter:
                continue
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
    skill_dir: Path
    resource_root: Path
    source: str  # builtin | user
    available: bool = True
    missing_tools: list[str] = Field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def description(self) -> str:
        return self.manifest.description

    def read_resource(self, resource: str) -> tuple[bool, str]:
        """Read a bounded support file relative to the skill directory."""
        raw = resource.strip()
        if not raw:
            return False, "resource 不能为空"
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.drive or "\x00" in raw:
            return False, "resource 必须是相对路径"
        try:
            target = (self.skill_dir / candidate).resolve()
            root = self.resource_root.resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                return False, f"resource 越界：{resource}"
            if not target.is_file():
                return False, f"resource 不存在：{resource}"
            data = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return False, f"resource 读取失败：{type(exc).__name__}: {exc}"
        rel = target.relative_to(root).as_posix()
        if len(data) > MAX_SKILL_RESOURCE_CHARS:
            data = data[:MAX_SKILL_RESOURCE_CHARS] + "\n\n[资源已截断，缩小 resource 或分段读取。]"
        return True, f"[技能资源 {self.name}:{rel}]\n{data}"


def _nested_skill_dirs(base: Path, max_depth: int = 5) -> list[Path]:
    """Return all directories under base that contain SKILL.md, including base."""
    found: list[Path] = []

    def visit(path: Path, depth: int) -> None:
        if (path / "SKILL.md").is_file():
            found.append(path)
        if depth >= max_depth:
            return
        try:
            children = sorted(path.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                visit(child, depth + 1)

    visit(base, 0)
    return found


def _skill_candidates(root: Path) -> list[tuple[Path, Path]]:
    """Discover (skill_dir, resource_root) pairs under a loader root.

    Normal skills are direct children. A bundle may be laid out as
    <name>/skills/**/SKILL.md with shared resources under <name>/skills and
    adjacent support directories at <name>/...
    """
    candidates: list[tuple[Path, Path]] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return candidates
    for child in children:
        if not child.is_dir():
            continue
        if (child / "SKILL.md").is_file():
            candidates.append((child, child))
        bundle_skills = child / "skills"
        if bundle_skills.is_dir():
            for skill_dir in _nested_skill_dirs(bundle_skills):
                candidates.append((skill_dir, child))
    return candidates


def _load_manifest(skill_dir: Path, skill_md: Path) -> SkillManifest:
    manifest_path = skill_dir / "manifest.yaml"
    if manifest_path.is_file():
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        return SkillManifest(**data)
    return _synth_manifest(skill_dir, skill_md)


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
        for skill_dir, resource_root in _skill_candidates(root):
            skill_md = skill_dir / "SKILL.md"
            try:
                manifest = _load_manifest(skill_dir, skill_md)
            except Exception:  # noqa: BLE001 - 坏技能跳过
                continue
            missing = [t for t in manifest.requires.get("tools", []) if not is_tool_available(t)]
            found[manifest.name] = SkillInfo(
                manifest=manifest,
                skill_md=skill_md,
                skill_dir=skill_dir,
                resource_root=resource_root,
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

    def activate(self, name: str, resource: str | None = None) -> tuple[bool, str]:
        """返回 (ok, SKILL.md/资源全文或错误信息)。"""
        s = self.skills.get(name)
        if s is None:
            return False, f"未知技能 {name!r}。可用: {', '.join(sorted(self.skills))}"
        if not s.available:
            return False, f"技能 {name} 不可用：缺少工具 {', '.join(s.missing_tools)}"
        if resource:
            return s.read_resource(resource)
        content = s.skill_md.read_text(encoding="utf-8")
        hint = (
            "\n\n[资源提示] 若本手册要求读取相对路径资料，可再次调用 "
            f"activate_skill(name={name!r}, resource='相对路径')。"
        )
        return True, f"[技能手册 {name} 已加载]\n{content}{hint}"

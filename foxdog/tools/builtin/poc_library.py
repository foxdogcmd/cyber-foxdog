"""用户漏洞/POC 知识库工具：把实战发现的漏洞方法沉淀成可检索 skill。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult
from ...skills.loader import USER_DIR


def _safe_name(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-").lower()
    return value or "poc"


def _short(value: str, limit: int = 300) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class SavePocArgs(BaseModel):
    name: str = Field(description="短 id，例如 cve-2024-xxxx 或 product-vuln-name")
    title: str = Field(description="漏洞/POC 标题")
    cve: str | None = Field(default=None, description="CVE 编号，可多个")
    product: str | None = Field(default=None, description="受影响组件/产品")
    vuln_type: str | None = Field(default=None, description="漏洞类型，如 RCE、SQLi、XXE")
    description: str = Field(default="", description="适用条件、利用思路、影响版本等目标无关描述")
    evidence: str | None = Field(default=None, description="真实工具输出/观察片段，不要包含靶机专属 flag")
    poc_code: str | None = Field(default=None, description="POC 代码、payload 或命令")
    references: list[str] | None = Field(default=None, description="来源 URL 或参考链接")
    requires_tools: list[str] | None = Field(default=None, description="执行 POC 需要的工具")
    overwrite: bool = Field(default=False, description="同名已存在时是否覆盖")


class SavePocTool(Tool):
    name = "save_poc"
    description = (
        "把实战中发现/验证过的漏洞、POC 或利用方法保存到用户知识库。"
        "自动生成 manifest.yaml、SKILL.md、poc 文件和证据，新任务启动后可用 activate_skill 检索。"
    )
    accesses = [ToolAccess(resource="state", mode="w")]
    args_model = SavePocArgs

    def _render_skill(self, a: SavePocArgs) -> str:
        lines = [f"# {a.title}", ""]
        lines.append("## 元数据")
        lines.append(f"- name: {_safe_name(a.name)}")
        if a.cve:
            lines.append(f"- CVE: {a.cve}")
        if a.product:
            lines.append(f"- Product: {a.product}")
        if a.vuln_type:
            lines.append(f"- Type: {a.vuln_type}")
        if a.references:
            lines.append("- References: " + " ".join(a.references))
        lines.extend(["", "## 适用条件", a.description or "（待补充）"])
        if a.poc_code:
            lines.extend(["", "## 方法 / POC", "```text", a.poc_code.strip(), "```"])
        if a.evidence:
            lines.extend(["", "## 证据", "```text", a.evidence.strip(), "```"])
        return "\n".join(lines) + "\n"

    def _write_manifest(self, root: Path, a: SavePocArgs) -> None:
        description = _short(
            " ".join(
                x
                for x in (
                    a.title,
                    f"CVE: {a.cve}" if a.cve else "",
                    f"产品: {a.product}" if a.product else "",
                    f"类型: {a.vuln_type}" if a.vuln_type else "",
                )
                if x
            )
        )
        when = _short(
            " ".join(
                x
                for x in (
                    f"遇到 {a.product}" if a.product else "",
                    f"且疑似 {a.vuln_type}" if a.vuln_type else "",
                    a.description,
                )
                if x
            ),
            limit=500,
        )
        manifest = {
            "name": _safe_name(a.name),
            "description": description,
            "when": when,
            "requires": {"tools": a.requires_tools or []},
            "kind": "poc",
        }
        (root / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        name = _safe_name(a.name)
        root = USER_DIR / name
        if root.exists() and not a.overwrite:
            return ToolResult(
                ok=False,
                error=(
                    f"用户技能 {name} 已存在：{root}。"
                    "用 overwrite=true 覆盖，或换一个 name。"
                ),
            )
        root.mkdir(parents=True, exist_ok=True)
        (root / "references").mkdir(exist_ok=True)
        self._write_manifest(root, a)
        (root / "SKILL.md").write_text(self._render_skill(a), encoding="utf-8")
        if a.poc_code and a.poc_code.strip():
            if re.search(r"(?m)^(import |from |#!.*python)", a.poc_code):
                poc_name = "poc.py"
            else:
                poc_name = "poc.txt"
            (root / poc_name).write_text(a.poc_code.strip() + "\n", encoding="utf-8")
        if a.evidence and a.evidence.strip():
            (root / "references" / "evidence.txt").write_text(
                a.evidence.strip() + "\n",
                encoding="utf-8",
            )
        return ToolResult(
            output=(
                f"已保存 POC 技能 {name}\n"
                f"路径: {root}\n"
                "新任务启动后会自动发现；检索："
                f"activate_skill(name={name!r}, resource='SKILL.md')"
            )
        )


class ListPocsArgs(BaseModel):
    pass


class ListPocsTool(Tool):
    name = "list_pocs"
    description = "列出用户 POC/漏洞知识库中已保存的技能，避免重复保存。"
    accesses = [ToolAccess(resource="state", mode="r")]
    args_model = ListPocsArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not USER_DIR.is_dir():
            return ToolResult(output="用户 POC 库：\n（无）")
        rows: list[str] = []
        for child in sorted(USER_DIR.iterdir()):
            if not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            try:
                data = yaml.safe_load((child / "manifest.yaml").read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                continue
            if data.get("kind") != "poc":
                continue
            desc = str(data.get("description") or "").strip()
            rows.append(f"- {child.name}: {desc}")
        return ToolResult(output=("用户 POC 库：\n" + ("\n".join(rows) if rows else "（无）")))

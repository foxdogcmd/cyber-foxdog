"""文件工具：read/write/edit 跟随执行环境的文件系统视图（与 shell 所见一致）。

- local runtime：宿主路径（相对基于工作区，绝对路径原样直通）。
- docker runtime：/workspace ↔ 宿主工作区双向映射；界外宿主路径读写都拒绝
  （唯一入口 import_file）；容器内 /workspace 之外路径引导 shell_exec。
glob/grep 保持工作区范围（bind mount 两端内容一致）。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ...core.context import spill_text
from .. import Tool, ToolAccess, ToolContext, ToolResult

READ_LIMIT = 32 * 1024  # 读文件进上下文的阈值


def _resolve(ctx: ToolContext, path: str, *, write: bool):
    """模型路径 → 宿主可读写 Path（随 runtime 语义）；越权抛 PermissionError。"""
    return ctx.runtime.resolve_fs_path(path, base_dir=ctx.work_dir, write=write)


def _display_rel(ctx: ToolContext, path) -> str:
    p = path.resolve()
    for root in (ctx.work_dir, ctx.workspace.work_dir):
        if root is None:
            continue
        try:
            return str(p.relative_to(root.resolve()))
        except ValueError:
            pass
    return str(p)


class ReadArgs(BaseModel):
    path: str = Field(description="文件路径（相对路径基于工作区；容器模式与 shell 共用 /workspace 视图）")


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取文件内容（与 shell 命令同一文件系统视图；容器模式下只能读 /workspace 工作区，"
        "宿主上的文件先 import_file 复制进工作区）。相对路径基于工作区，大文件截断。"
    )
    accesses = [ToolAccess(resource="workspace", mode="r")]
    args_model = ReadArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            p, hint = _resolve(ctx, a.path, write=False)
        except PermissionError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not p.is_file():
            return ToolResult(ok=False, error=f"文件不存在: {ctx.runtime.display_path(p)}")
        text = p.read_text(encoding="utf-8", errors="replace")
        view, handle, truncated = spill_text(ctx.workspace, text, READ_LIMIT)
        output = f"{hint}\n{view}" if hint else view
        return ToolResult(output=output, handle=handle, data={"truncated": truncated})


class WriteArgs(BaseModel):
    path: str
    content: str


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "写入或覆盖文件（与 shell 命令同一文件系统视图；容器模式下只能写 /workspace 工作区）。"
        "相对路径基于工作区。"
    )
    accesses = [ToolAccess(resource="workspace", mode="w")]
    args_model = WriteArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            p, _hint = _resolve(ctx, a.path, write=True)
        except PermissionError as exc:
            return ToolResult(ok=False, error=str(exc))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(a.content, encoding="utf-8")
        return ToolResult(output=f"已写入 {ctx.runtime.display_path(p)}（{len(a.content)} 字符）")


class EditArgs(BaseModel):
    path: str
    old: str = Field(description="要被替换的原文（必须唯一出现）")
    new: str = Field(description="替换后的内容")


class EditFileTool(Tool):
    name = "edit_file"
    description = "精确替换文件文本（old 必须唯一出现；与 shell 命令同一文件系统视图）。相对路径基于工作区。"
    accesses = [ToolAccess(resource="workspace", mode="w")]
    args_model = EditArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            p, _hint = _resolve(ctx, a.path, write=True)
        except PermissionError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not p.is_file():
            return ToolResult(ok=False, error=f"文件不存在: {ctx.runtime.display_path(p)}")
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(a.old)
        if count == 0:
            return ToolResult(ok=False, error="old 未在文件中出现")
        if count > 1:
            return ToolResult(ok=False, error=f"old 出现 {count} 次，不唯一")
        p.write_text(text.replace(a.old, a.new, 1), encoding="utf-8")
        return ToolResult(output=f"已替换 {ctx.runtime.display_path(p)}")


class GlobArgs(BaseModel):
    pattern: str = Field(description="glob 模式，如 **/*.py")


class GlobTool(Tool):
    name = "glob"
    description = "按 glob 模式列出工作区内文件（与 shell 同一文件系统视图，最多 200 条）。"
    accesses = [ToolAccess(resource="workspace", mode="r")]
    args_model = GlobArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        root = ctx.work_dir
        work_root = ctx.workspace.work_dir.resolve()
        matches = sorted(
            p.resolve()
            for p in root.glob(a.pattern)
            if p.is_file() and (p.resolve() == work_root or work_root in p.resolve().parents)
        )[:200]
        rel = [_display_rel(ctx, p) for p in matches]
        return ToolResult(output="\n".join(rel) or "(无匹配)")


class GrepArgs(BaseModel):
    pattern: str = Field(description="正则表达式")
    path: str = Field(default=".", description="搜索起始目录（工作区内）")
    max_matches: int = Field(default=50, le=200)


class GrepTool(Tool):
    name = "grep"
    description = "在工作区内按正则搜索文件内容（与 shell 同一文件系统视图），输出 文件:行号:内容。"
    accesses = [ToolAccess(resource="workspace", mode="r")]
    args_model = GrepArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            root = ctx.resolve_work_path(a.path)
        except PermissionError as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            rx = re.compile(a.pattern)
        except re.error as exc:
            return ToolResult(ok=False, error=f"正则无效: {exc}")
        hits: list[str] = []
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    rel = _display_rel(ctx, f) if f != root else f.name
                    hits.append(f"{rel}:{i}:{line[:300]}")
                    if len(hits) >= a.max_matches:
                        break
            if len(hits) >= a.max_matches:
                break
        return ToolResult(output="\n".join(hits) or "(无匹配)")

"""import_file：把宿主机文件/目录复制进 workspace inputs/（两端路径策略的入口）。

痛点：Windows 上的题目附件（桌面/下载）在 docker 模式下容器看不到，模型
容易瞎猜两端路径。统一入口：先 import_file，返回 workspace 相对路径 +
执行环境（容器/宿主）绝对路径。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import Tool, ToolAccess, ToolContext, ToolResult


class ImportFileArgs(BaseModel):
    path: str = Field(description="宿主机上的文件或目录路径（如 C:\\Users\\x\\Desktop\\chall.zip）")


class ImportFileTool(Tool):
    name = "import_file"
    description = (
        "把宿主机上的文件/目录复制进 workspace inputs/（Windows 上的题目附件先用它，别猜两端路径）。"
        "目录保持结构整体复制。返回 workspace 相对路径与执行环境（容器/宿主）绝对路径。"
    )
    accesses = [ToolAccess(resource="workspace", mode="w")]
    args_model = ImportFileArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        src = Path(a.path).expanduser()
        if not src.exists():
            return ToolResult(ok=False, error=f"路径不存在: {src}")
        dst = ctx.workspace.inputs_dir / src.name
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                kind, size_note = "目录", ""
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                kind, size_note = "文件", f"（{dst.stat().st_size} 字节）"
        except OSError as exc:
            return ToolResult(ok=False, error=f"复制失败: {exc}")
        rel = dst.relative_to(ctx.workspace.dir).as_posix()  # 统一 POSIX 分隔（两端通用）
        runtime = getattr(ctx, "runtime", None)
        if runtime is not None and getattr(runtime, "kind", "local") != "local":
            exec_path = runtime.host_to_container(dst)
            env_note = f"容器内绝对路径：{exec_path}"
        else:
            exec_path = str(dst)
            env_note = f"宿主绝对路径：{exec_path}"
        return ToolResult(
            output=f"已导入{kind}{size_note}：{rel}\n{env_note}",
            data={"relative": rel, "exec_path": exec_path},
        )

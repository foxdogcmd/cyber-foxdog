"""输出归档与取回（借鉴 LeoAI get_tool_result_archive）。

工具输出超过阈值时截断进上下文，全文落盘 CAS，
模型可用 fetch_output(handle, offset, limit) 按需取回任意片段。
"""
from __future__ import annotations

from ..store.workspace import Workspace

DEFAULT_FETCH_LIMIT = 4000


def spill_text(
    workspace: Workspace,
    text: str,
    threshold: int = 64 * 1024,
) -> tuple[str, str | None, bool]:
    """超阈值则全文入 CAS，返回 (截断后文本, handle, truncated)。"""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= threshold:
        return text, None, False
    handle = workspace.cas_store(data)
    half = threshold // 2
    head = data[:half].decode("utf-8", errors="replace")
    tail = data[-half:].decode("utf-8", errors="replace")
    return f"{head}\n...[省略 {len(data) - threshold} 字节]...\n{tail}", handle, True


def fetch_output(workspace: Workspace, handle: str, offset: int = 0, limit: int = DEFAULT_FETCH_LIMIT) -> str:
    """按句柄取回归档输出片段。"""
    if not workspace.cas_exists(handle):
        return f"error: 未知句柄 {handle!r}"
    total = workspace.cas_path(handle).stat().st_size
    text = workspace.cas_fetch_text(handle, offset=offset, limit=limit)
    end = offset + len(text.encode("utf-8", errors="replace"))
    footer = f"\n[handle={handle} offset={offset} 显示到 {end}/{total} 字节]"
    return text + footer

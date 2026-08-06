"""TUI 自定义组件与黑板渲染（纯函数可单测）。

- ReasoningBlock：固定五行的灰色斜体 reasoning 尾窗，实时跟随最新内容。
- ToolCallRow：时间线里的工具调用行。普通工具默认一行摘要；write_file 默认展示
  路径与前 10 行内容，edit_file 默认展示 unified diff，点击展开剩余行。
  进行中用 braille spinner，完成后成功绿/失败红作为常驻历史状态。
- render_facts/deadends/intents：黑板面板渲染。文本尾部折叠省略（key 信息在前），
  每节最多 20 条（侧栏紧凑态最多 8 条）+ "+N more"；intents 按状态分组带图标。
- agent_text：agent 消息着色（`code` 橙色、**粗体**），● 前缀由调用方加。
- render_todos：任务清单（✓ 灰 / ■ 黄 / □ 默认色）。
"""
from __future__ import annotations

import difflib
import json
import re
import textwrap
from typing import Any

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

BOARD_LIMIT = 20  # 每节最多条数
BOARD_COMPACT_LIMIT = 8  # 侧栏紧凑态最多条数，避免未展开时信息过密
TAIL_LIMIT = 200  # 单条文本尾部折叠长度

_SPINNER = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"
FILE_PREVIEW_LINES = 10
REASONING_BUFFER_CHARS = 6000
_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x9d[^\x07]*(?:\x07|\x1b\\)|"
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x9b[0-?]*[ -/]*[@-~])"
)


def clean_terminal_text(value: Any) -> str:
    """Remove terminal control sequences before untrusted text enters the TUI."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_ESCAPE_RE.sub("", text)
    return "".join(
        char
        for char in text
        if char in "\n\t"
        or (
            ord(char) >= 0x20
            and ord(char) != 0x7F
            and not 0x80 <= ord(char) <= 0x9F
        )
    )


def tail_fold(text: str, limit: int = TAIL_LIMIT) -> str:
    """超长折叠尾部省略号（key 信息在前）。"""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------ agent 文本着色
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def agent_text(raw: str) -> Text:
    """agent 消息渲染：`内联代码` 橙色、**粗体**保留（轻量手动着色，流式安全）。"""
    out = Text()
    # 先按 `code` 切段（奇数段为普通文本，偶数段为代码）
    segments = raw.split("`")
    for i, seg in enumerate(segments):
        if not seg:
            continue
        if i % 2 == 1:
            out.append(seg, style="dark_orange")
            continue
        pos = 0
        for m in _BOLD_RE.finditer(seg):
            if m.start() > pos:
                out.append(seg[pos : m.start()])
            out.append(m.group(1), style="bold")
            pos = m.end()
        if pos < len(seg):
            out.append(seg[pos:])
    return out


# ------------------------------------------------------------ 任务清单渲染
_TODO_MARK = {"done": ("✓", "dim"), "in_progress": ("■", "yellow"), "pending": ("□", "")}


def render_todos(items: list[Any]) -> Text:
    """todo 清单：✓ 已完成（灰）、■ 进行中（黄）、□ 待办。"""
    out = Text("任务清单\n", style="bold")
    if not items:
        out.append("（无）", style="dim")
        return out
    for item in items:
        mark, style = _TODO_MARK.get(getattr(item, "status", "pending"), ("□", ""))
        out.append(f"{mark} {getattr(item, 'content', '')}\n", style=style)
    return out


# ------------------------------------------------------------ 模型 thinking 尾窗
class ReasoningBlock(Vertical):
    """Inline five-line tail view for streamed model reasoning."""

    can_focus = False

    DEFAULT_CSS = """
    ReasoningBlock {
        height: 5;
        min-height: 5;
        max-height: 5;
        overflow-x: hidden;
        overflow-y: hidden;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    ReasoningBlock .reasoning-text {
        height: 5;
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self, request_id: str, worker: str | None = None) -> None:
        super().__init__()
        self.request_id = request_id
        self.worker = worker
        self._buffer = ""
        self._buffer_parts: list[str] = []
        self._footer = ""
        self._refresh_scheduled = False
        # rewrap 缓存：同 width 且 buffer/footer 未变时直接复用（resize 扇出 N 块时
        # 未变 width 的块走这里 O(1)，不再 O(buffer×width) 全量 rewrap）。
        self._wrapped_width: int | None = None
        self._wrapped_fp: tuple | None = None
        self._wrapped_footer_fp: tuple | None = None
        self._wrapped_lines: list[str] = []

    def _buffer_fingerprint(self) -> tuple:
        b = self._buffer
        return (len(b), b[:8], b[-8:]) if b else (0, "", "")

    def _footer_fingerprint(self) -> tuple:
        f = self._footer
        return (len(f), f[:8], f[-8:]) if f else (0, "", "")

    def compose(self):
        yield Static("", classes="reasoning-text")

    def append(self, delta: str) -> None:
        if not delta:
            return
        # Keep token appends linear; repeated ``self._buffer += delta`` becomes
        # quadratic for long reasoning streams before the tail window is shown.
        self._buffer_parts.append(delta)
        self._schedule_refresh()

    def _materialize_buffer(self) -> None:
        if self._buffer_parts:
            self._buffer += "".join(self._buffer_parts)
            self._buffer_parts.clear()
        if len(self._buffer) > REASONING_BUFFER_CHARS:
            tail = self._buffer[-REASONING_BUFFER_CHARS:]
            newline = tail.find("\n")
            self._buffer = "…\n" + (tail[newline + 1 :] if newline >= 0 else tail)

    def finish(self, duration_ms: int, completion_tokens: int | None, *, ok: bool = True) -> None:
        seconds = duration_ms / 1000
        minutes, remaining = divmod(int(seconds), 60)
        duration = f"{minutes}m{remaining:02d}s" if minutes else f"{seconds:.1f}s"
        token_note = f" · {completion_tokens:,} tokens" if completion_tokens is not None else ""
        worker = f"[{self.worker}] " if self.worker else ""
        state = "完成" if ok else "中断"
        self._footer = f"\n{worker}thinking {state} · {duration}{token_note}"
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        self.call_after_refresh(self._refresh_content)

    def _wrap_lines(self, text: str, width: int) -> list[str]:
        out: list[str] = []
        for line in text.splitlines() or [""]:
            out.extend(
                textwrap.wrap(
                    line,
                    width=width,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
            )
        return out

    def _refresh_content(self) -> None:
        self._refresh_scheduled = False
        self._materialize_buffer()
        width = max(20, self.content_size.width or self.size.width or 80)
        fp = self._buffer_fingerprint()
        ffp = self._footer_fingerprint()
        # 同 width + buffer/footer 未变 → 复用缓存（resize 扇出时 N 个未变块 O(1)）
        if (
            self._wrapped_width == width
            and self._wrapped_fp == fp
            and self._wrapped_footer_fp == ffp
        ):
            visual_lines = self._wrapped_lines
        else:
            visual_lines = self._wrap_lines(self._buffer + self._footer, width)
            self._wrapped_width = width
            self._wrapped_fp = fp
            self._wrapped_footer_fp = ffp
            self._wrapped_lines = visual_lines
        if not self.is_mounted:
            return
        try:
            child = self.query_one(".reasoning-text", Static)
            child.update(Text("\n".join(visual_lines[-5:]), style="dim italic"), layout=False)
        except Exception:
            pass

    def on_resize(self, _event: Any) -> None:
        self._schedule_refresh()

    def snapshot(self) -> dict:
        """窗口化重建用的状态快照（buffer + footer 终态）。"""
        self._materialize_buffer()
        return {"buffer": self._buffer, "footer": self._footer, "expanded": self.has_class("expanded")}

    @classmethod
    def restore(cls, request_id: str, worker: str | None, snap: dict) -> "ReasoningBlock":
        """从快照重建：恢复 buffer + footer 终态，一次性刷新。"""
        block = cls(request_id, worker)
        block._buffer = snap.get("buffer", "")
        block._footer = snap.get("footer", "")
        if snap.get("expanded"):
            block.add_class("expanded")
        # 挂载后 call_after_refresh 会触发首帧；强制标记需要刷新
        block._refresh_scheduled = False
        return block


# ------------------------------------------------------------ 黑板渲染
def render_facts(
    facts: list[dict[str, Any]], limit: int = BOARD_LIMIT, *, compact: bool = False
) -> Text:
    out = Text()
    out.append("已知事实", style="bold cyan")
    out.append(f"  {len(facts)}\n", style="dim")
    if not facts:
        out.append("  暂无", style="dim italic")
        return out
    effective_limit = min(limit, BOARD_COMPACT_LIMIT) if compact else limit
    shown_items = facts[-effective_limit:]
    for idx, f in enumerate(shown_items):
        out.append("  ◆ " if compact else "◆ ", style="cyan")
        out.append(tail_fold(f["fact"], 96 if compact else TAIL_LIMIT))
        if f.get("evidence"):
            out.append(f"\n  ↳ {tail_fold(f['evidence'], 72 if compact else 140)}", style="dim cyan")
        out.append("\n\n" if compact and idx < len(shown_items) - 1 else "\n")
    extra = len(facts) - effective_limit
    if extra > 0:
        out.append(f"… +{extra} more", style="dim cyan")
    return out


def render_deadends(
    deadends: list[dict[str, Any]], limit: int = BOARD_LIMIT, *, compact: bool = False
) -> Text:
    out = Text()
    out.append("已排除方向", style="bold red")
    out.append(f"  {len(deadends)}\n", style="dim")
    if not deadends:
        out.append("  暂无", style="dim italic")
        return out
    effective_limit = min(limit, BOARD_COMPACT_LIMIT) if compact else limit
    shown_items = deadends[-effective_limit:]
    for idx, d in enumerate(shown_items):
        out.append("  × " if compact else "× ", style="red")
        out.append(tail_fold(d["direction"], 64 if compact else 100))
        out.append("\n  ↳ ", style="dim")
        out.append(tail_fold(d["reason"], 72 if compact else 140), style="dim")
        out.append("\n\n" if compact and idx < len(shown_items) - 1 else "\n")
    extra = len(deadends) - effective_limit
    if extra > 0:
        out.append(f"… +{extra} more", style="dim red")
    return out


_INTENT_ICON = {"claimed": "⧗", "open": "○", "done": "✓", "barren": "✗"}
_INTENT_ORDER = ["claimed", "open", "done", "barren"]
_INTENT_STYLE = {"claimed": "yellow", "open": "cyan", "done": "green", "barren": "red"}


def render_intents(
    intents: list[dict[str, Any]], limit: int = BOARD_LIMIT, *, compact: bool = False
) -> Text:
    """intents 按状态分组：⧗ claimed / ○ open / ✓ done / ✗ barren，每条一行。"""
    out = Text()
    out.append("Intents", style="bold magenta")
    out.append(f"  {len(intents)}\n", style="dim")
    if not intents:
        out.append("  暂无", style="dim italic")
        return out
    effective_limit = min(limit, BOARD_COMPACT_LIMIT) if compact else limit
    by_status: dict[str, list[dict[str, Any]]] = {k: [] for k in _INTENT_ORDER}
    for i in intents:
        status = i.get("status", "open")
        by_status[status if status in by_status else "open"].append(i)
    shown = 0
    for status in _INTENT_ORDER:
        group = by_status.get(status) or []
        if not group:
            continue
        style = _INTENT_STYLE[status]
        out.append(f"{_INTENT_ICON[status]} {status}", style=f"bold {style}")
        out.append(f"  {len(group)}\n", style="dim")
        for i in group:
            if shown >= effective_limit:
                break
            goal = tail_fold(" ".join(str(i["goal"]).split()), 64 if compact else 120)
            out.append(f"  {i['id']}", style="dim")
            if i.get("claimed_by"):
                out.append(" [by ", style="dim")
                out.append(str(i["claimed_by"]), style="magenta")
                out.append("]", style="dim")
            out.append(f" {goal}\n")
            shown += 1
        if compact and shown < effective_limit:
            out.append("\n")
    extra = len(intents) - shown
    if extra > 0:
        out.append(f"… +{extra} more", style="dim magenta")
    return out


# ------------------------------------------------------------ 黑板单条（全屏可展开）
# 每条返回 (collapsed, expanded_summary, detail, foldable)：
#   collapsed       折叠态摘要（tail_fold 截断 + dim「[展开]」提示）
#   expanded_summary 展开态摘要（全文，替换折叠摘要就地显示）
#   detail          展开时摘要下方的附行（evidence / reason），折叠态隐藏
#   foldable        是否可展开（短条目 False，不可点）
def fact_item(f: dict[str, Any]) -> tuple[Text, Text, Text, bool]:
    full = " ".join(str(f.get("fact", "")).split())
    evidence = f.get("evidence") or ""
    foldable = len(full) > TAIL_LIMIT or len(evidence) > 140
    collapsed = Text()
    collapsed.append("◆ ", style="cyan")
    collapsed.append(tail_fold(full, TAIL_LIMIT))
    if evidence:
        collapsed.append("\n  ↳ ", style="dim cyan")
        collapsed.append(tail_fold(evidence, 140), style="dim cyan")
    if foldable:
        collapsed.append("  [展开]", style="dim")
    expanded_summary = Text()
    expanded_summary.append("◆ ", style="cyan")
    expanded_summary.append(full)
    detail = Text()
    if evidence:
        detail.append(f"  ↳ {evidence}", style="dim cyan")
    return collapsed, expanded_summary, detail, foldable


def deadend_item(d: dict[str, Any]) -> tuple[Text, Text, Text, bool]:
    direction = " ".join(str(d.get("direction", "")).split())
    reason = " ".join(str(d.get("reason", "")).split())
    foldable = len(direction) > 100 or len(reason) > 140
    collapsed = Text()
    collapsed.append("× ", style="red")
    collapsed.append(tail_fold(direction, 100))
    if reason:
        collapsed.append("\n  ↳ ", style="dim")
        collapsed.append(tail_fold(reason, 140), style="dim")
    if foldable:
        collapsed.append("  [展开]", style="dim")
    expanded_summary = Text()
    expanded_summary.append("× ", style="red")
    expanded_summary.append(direction)
    detail = Text()
    if reason:
        detail.append(f"  ↳ {reason}", style="dim")
    return collapsed, expanded_summary, detail, foldable


def intent_item(i: dict[str, Any]) -> tuple[Text, Text, Text, bool]:
    goal = " ".join(str(i.get("goal", "")).split())
    foldable = len(goal) > 120
    status = i.get("status", "open")
    icon = _INTENT_ICON.get(status, "○")
    style = _INTENT_STYLE.get(status, "cyan")
    prefix = f"  {icon} {i.get('id', '')}"
    if i.get("claimed_by"):
        prefix += f" [by {i['claimed_by']}]"
    collapsed = Text()
    collapsed.append(f"{prefix} ", style=f"dim {style}")
    collapsed.append(tail_fold(goal, 120))
    if foldable:
        collapsed.append("  [展开]", style="dim")
    expanded_summary = Text()
    expanded_summary.append(f"{prefix} ", style=f"dim {style}")
    expanded_summary.append(goal)
    return collapsed, expanded_summary, Text(), foldable


class BoardItem(Vertical):
    """黑板单条：摘要常驻，全文 + 附行点击/回车就地展开（同 ToolCallRow 范式）。

    折叠态显示 collapsed 摘要；展开态把摘要换成 expanded_summary 全文，并显示 detail 附行。
    update_content 刷新内容时保留 expanded class（2s 刷新不丢展开态）。
    """

    DEFAULT_CSS = """
    BoardItem { height: auto; padding: 0 0 1 1; }
    BoardItem .bi-detail { display: none; padding-left: 3; color: $text-muted; }
    BoardItem.expanded .bi-detail { display: block; }
    BoardItem.foldable:hover .bi-summary { background: $surface-lighten-1; }
    """

    def __init__(
        self,
        collapsed: Text,
        expanded_summary: Text,
        detail: Text,
        foldable: bool,
        *,
        start_expanded: bool = False,
    ) -> None:
        super().__init__()
        self._collapsed = collapsed
        self._expanded_summary = expanded_summary
        self._detail = detail or Text()
        self.foldable = foldable
        self.can_focus = foldable  # 短条目不进焦点环、不可点
        self.item_key: str | None = None
        if foldable:
            self.add_class("foldable")
        if foldable and start_expanded:
            self.add_class("expanded")

    def compose(self):
        yield Static(self._current_summary(), classes="bi-summary")
        yield Static(self._detail, classes="bi-detail")

    @property
    def expanded(self) -> bool:
        return self.has_class("expanded")

    def _current_summary(self) -> Text:
        return self._expanded_summary if self.expanded else self._collapsed

    def toggle(self) -> None:
        if not self.foldable:
            return
        self.set_class(not self.expanded, "expanded")
        self.query_one(".bi-summary", Static).update(self._current_summary())

    def update_content(
        self, collapsed: Text, expanded_summary: Text, detail: Text
    ) -> None:
        """刷新内容但保留展开态（2s 刷新时由 diff 调用）。"""
        self._collapsed = collapsed
        self._expanded_summary = expanded_summary
        self._detail = detail or Text()
        self.query_one(".bi-summary", Static).update(self._current_summary())
        self.query_one(".bi-detail", Static).update(self._detail)

    def on_click(self, event) -> None:
        self.toggle()
        event.stop()  # 不冒泡到 app.on_click（避免误触"开全屏"）

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            self.toggle()
            event.prevent_default()
            event.stop()


# ------------------------------------------------------------ 工具调用折叠行
class ToolCallRow(Vertical):
    """Tool row with persistent status and structured previews for file changes."""

    can_focus = True

    DEFAULT_CSS = """
    ToolCallRow {
        width: 1fr;
        min-width: 0;
        max-width: 100%;
        height: auto;
        overflow-x: hidden;
    }
    ToolCallRow .tc-summary {
        width: 1fr;
        min-width: 0;
        max-width: 100%;
        overflow-x: hidden;
        color: $text-muted;
    }
    ToolCallRow.succeeded .tc-summary { color: $success; }
    ToolCallRow.failed .tc-summary { color: $error; }
    ToolCallRow .tc-detail {
        display: none;
        width: 1fr;
        min-width: 0;
        max-width: 100%;
        overflow-x: hidden;
        padding-left: 2;
        color: $text-muted;
    }
    ToolCallRow.expanded .tc-detail, ToolCallRow.file-change .tc-detail { display: block; }
    """

    def __init__(self, call_id: str, name: str, arguments: str, worker: str | None = None) -> None:
        self._file_change = name in {"write_file", "edit_file"}
        super().__init__(classes="file-change" if self._file_change else None)
        self.call_id = call_id
        self.tool_name = name
        self.arguments = clean_terminal_text(arguments)
        try:
            parsed = json.loads(arguments) if arguments else {}
            self.parsed_arguments = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            self.parsed_arguments = {}
        self.worker = worker
        self.ok: bool | None = None
        self.duration_ms = 0
        self.error: str | None = None
        self.handle: str | None = None
        self.preview = ""
        self._spin = 0
        self._spin_timer = None
        # edit_file diff 缓存：按 (old,new,path) 内容哈希，避免每次展开重跑 unified_diff
        self._diff_cache_key: tuple | None = None
        self._diff_cache_lines: list[str] | None = None

    def compose(self):
        yield Static(self._summary(), classes="tc-summary")
        yield Static(self._detail(), classes="tc-detail")

    def on_mount(self) -> None:
        if self.ok is None:  # 进行中：行尾 spinner
            self._spin_timer = self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if not self.is_mounted:
            return
        self._spin = (self._spin + 1) % len(_SPINNER)
        self.query_one(".tc-summary", Static).update(self._summary(), layout=False)

    def _summary(self) -> Text:
        if self._file_change and self.parsed_arguments.get("path"):
            args = clean_terminal_text(self.parsed_arguments["path"])
        else:
            args = " ".join(clean_terminal_text(self.arguments).split())
        if len(args) > 60:
            args = args[:59] + "…"
        prefix = Text()
        if self.worker:
            import hashlib as _hl
            worker = clean_terminal_text(self.worker)
            label = worker.rsplit('-', 1)[0] if '-' in worker else worker
            palette = ['cyan', 'yellow', 'green', 'blue', 'magenta', 'red', 'bright_blue', 'bright_green']
            _h = int(_hl.md5(label.encode()).hexdigest()[:8], 16)
            prefix.append(f"[{worker}] ", style=palette[_h % len(palette)])
        if self.ok is None:
            mark, style = _SPINNER[self._spin], "yellow"
        else:
            mark = "✓" if self.ok else "✗"
            style = "green" if self.ok else "red"
        if self.error == "已中断":
            tail = "（已中断）"
        else:
            tail = f"（{self.duration_ms}ms）" if self.ok is not None else ""
        out = Text()
        out.append(prefix)
        out.append(f"{mark} ", style=style)
        out.append(f"{self.tool_name} {args}{tail}")
        return out

    def set_result(
        self,
        ok: bool,
        duration_ms: int,
        preview: str = "",
        error: str | None = None,
        handle: str | None = None,
    ) -> None:
        self.ok = ok
        self.duration_ms = duration_ms
        self.error = clean_terminal_text(error) if error is not None else None
        self.handle = handle
        self.preview = clean_terminal_text(preview)
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        if not self.is_mounted:
            # 已折叠（窗口化卸载）：只存终态，不碰 DOM；重建时 rebuild 从本对象复制
            return
        self.set_class(ok, "succeeded")
        self.set_class(not ok, "failed")
        self.query_one(".tc-summary", Static).update(self._summary())
        if self.expanded or self._file_change:
            self.query_one(".tc-detail", Static).update(self._detail())

    def _file_change_detail(self) -> Text:
        out = Text()
        path = clean_terminal_text(self.parsed_arguments.get("path", "(unknown path)"))
        out.append(f"path: {path}\n", style="bold")
        if self.tool_name == "write_file":
            out.append("content:\n", style="dim")
            lines = [
                clean_terminal_text(line)
                for line in str(self.parsed_arguments.get("content", "")).splitlines()
            ] or [""]
            visible = lines if self.expanded else lines[:FILE_PREVIEW_LINES]
            number_width = max(2, len(str(len(lines))))
            for number, line in enumerate(visible, 1):
                out.append(f"{number:>{number_width}} │ ", style="dim")
                out.append(line)
                out.append("\n")
        else:
            out.append("diff:\n", style="dim")
            old = str(self.parsed_arguments.get("old", ""))
            new = str(self.parsed_arguments.get("new", ""))
            # 缓存键：长度 + 首尾切片（同进程内判定 old/new 是否变化，避免每次展开重跑 diff）
            cache_key = (len(old), old[:8] + old[-8:] if old else "", len(new), new[:8] + new[-8:] if new else "", path)
            if self._diff_cache_key == cache_key and self._diff_cache_lines is not None:
                lines = self._diff_cache_lines
            else:
                lines = list(
                    difflib.unified_diff(
                        [clean_terminal_text(line) for line in old.splitlines()],
                        [clean_terminal_text(line) for line in new.splitlines()],
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                        lineterm="",
                    )
                )
                if not lines:
                    lines = ["(no changes)"]
                self._diff_cache_key = cache_key
                self._diff_cache_lines = lines
            visible = lines if self.expanded else lines[:FILE_PREVIEW_LINES]
            for line in visible:
                if line.startswith("+") and not line.startswith("+++"):
                    style = "green"
                elif line.startswith("-") and not line.startswith("---"):
                    style = "red"
                elif line.startswith("@@"):
                    style = "cyan"
                else:
                    style = "dim"
                out.append(f"{line}\n", style=style)
        hidden = len(lines) - len(visible)
        if hidden > 0:
            out.append(f"… +{hidden} lines\n", style="dim")
        return out

    def _detail(self) -> Text:
        if self._file_change:
            out = self._file_change_detail()
        else:
            out = Text()
            out.append("参数:\n", style="dim")
            out.append(f"{self.arguments}\n", style="dim")
        if self.error:
            out.append(f"└ 错误: {clean_terminal_text(self.error)}\n", style="red")
        if self.preview and not self._file_change:
            cleaned_preview = clean_terminal_text(self.preview)
            preview = cleaned_preview[:1024]
            if len(cleaned_preview) > 1024:
                preview += "…"
            lines = preview.splitlines() or [preview]
            out.append("输出:\n", style="dim")
            for i, line in enumerate(lines):
                out.append(f"{'└' if i == 0 else ' '} {line}\n")
        if self.handle:
            out.append(f"└ cas_handle: {self.handle}\n", style="dim")
        return out

    @property
    def expanded(self) -> bool:
        return self.has_class("expanded")

    def toggle(self) -> None:
        if self.expanded:
            self.remove_class("expanded")
        else:
            self.add_class("expanded")
        self.query_one(".tc-summary", Static).update(self._summary())
        self.query_one(".tc-detail", Static).update(self._detail())

    def on_click(self) -> None:
        self.toggle()

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            self.toggle()
            event.prevent_default()
            event.stop()


# ------------------------------------------------------------ agent 消息块（一个消息一个 ●）
class AgentStreamBlock(Vertical):
    """agent 文本块：worker 标签、● 与正文首行在同一行。

    一个逻辑消息一个 ●：跨多个 model request 的相邻文本由上层合并进同一块
    （interactive._render_event 的相邻合并逻辑），续行不再被腰斩成两条 ●。
    """

    DEFAULT_CSS = """
    AgentStreamBlock {
        width: 1fr;
        min-width: 0;
        max-width: 100%;
        height: auto;
        overflow-x: hidden;
    }
    AgentStreamBlock .agent-text {
        width: 1fr;
        min-width: 0;
        max-width: 100%;
        height: auto;
        overflow-x: hidden;
    }
    """

    def __init__(self, worker: str | None = None) -> None:
        super().__init__(classes="agent-stream")
        self.worker = worker
        # 缓存上次渲染的 raw：pump 多 key 时常有重复 set_body 同值，跳过零计算。
        self._body_raw: str | None = None

    def compose(self):
        yield Static(self._render_text(self._body_raw or ""), classes="agent-text")

    def _prefix(self) -> Text:
        out = Text()
        if self.worker:
            label = self.worker.rsplit("-", 1)[0] if "-" in self.worker else self.worker
            import hashlib as _hl

            palette = ["cyan", "yellow", "green", "blue", "magenta", "red", "bright_blue", "bright_green"]
            h = int(_hl.md5(label.encode()).hexdigest()[:8], 16)
            out.append(f"[{self.worker}] ", style=palette[h % len(palette)])
        out.append("● ", style="cyan")
        return out

    def _render_text(self, raw: str) -> Text:
        out = self._prefix()
        out.append_text(agent_text(raw.lstrip("\r\n")))
        return out

    def set_body(self, raw: str) -> None:
        """正文更新（同值跳过；Rich 折行保证 ASCII 单词不腰斩）。"""
        if raw == self._body_raw:
            return  # pump 抖动或无新 delta，零计算
        self._body_raw = raw
        if not self.is_mounted:
            return  # 已折叠：只缓存 raw，重建时 rebuild 重新 set_body
        self.query_one(".agent-text", Static).update(self._render_text(raw))

    def _set_body_if_mounted(self, raw: str) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one(".agent-text", Static).update(self._render_text(raw))
        except Exception:
            pass

"""Timeline 视口窗口化：单向裁剪旧 widget，合并成顶部只读历史摘要。

根因：#timeline 是单个 VerticalScroll，每个事件 mount 一个 widget 且永不卸载，
Textual 滚动时 layout/compositing 成本随挂载 widget 总数线性增长 → 长任务卡顿。

策略（单向裁剪，2026-08-05 重写）：维护有序逻辑条目列表。挂载数超过 _MAX_MOUNTED
时，从最旧的非 pinned 条目起分帧卸载一批，把它们的纯文本快照追加进内部历史，
并只在 child 0 渲染一行只读历史摘要（一个 widget，文本增长不增渲染成本）。

**关键：滚动不再触发任何 mount/unmount。** 旧方案在滚动回调里 fold/unfold 重建
上百 widget，改变内容高度又反过来触发滚动回调，与 Textual scroll anchor 冲突，
形成 fold↔scroll 抖动/卡死（用户实测"滚不动"）。单向裁剪彻底断开这条重入路径：
滚回顶部看到的是静态历史摘要，交互 widget 只在视口附近保留。

流式中的块（pinned）永不裁剪。裁剪只在 append 时按增量计数触发，并用短定时器拆成
小切片执行，避免一次 remove/render 过多 widget 把滚轮帧卡住；滚动回调是纯空
操作（只维护基线，不扫描不挂载）。

线程模型：所有方法在 Textual 主循环（asyncio）调用。裁剪期间设重入锁，防止
mount(before=0) 引发的 scroll_y 变化递归回裁剪。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from rich.text import Text
from textual.containers import VerticalScroll
from textual import events
from textual.widgets import Static

# 计数兜底：最多保留这么多已挂载条目（防超长任务把树撑大）。
# 每条是独立 widget，Textual layout 成本 ∝ 挂载 widget 数；滚动视口不需要保留整场历史。
_MAX_MOUNTED = 96
# 每轮裁剪把挂载数降到 _MAX_MOUNTED - _TRIM_BATCH，避免每 append 都触发裁剪（迟滞）。
_TRIM_BATCH = 24
# 每个裁剪切片最多卸载的 widget 数。保持极小值，避免滚轮期间出现周期性长帧。
_TRIM_SLICE = 4
# 分片裁剪间隔；独立于 Textual 是否触发下一次 repaint，保证后台回收能追上。
_TRIM_INTERVAL = 0.02
# 内部历史快照保留的最大字符数（屏幕只渲染摘要）；超出丢最旧。
_HISTORY_MAX_CHARS = 60_000
_HISTORY_ENTRY_MAX_CHARS = 1_200
_HISTORY_RENDER_TAIL_CHARS = 160
_HISTORY_HEAD = "… 已折叠 {count} 条历史；新内容在下方"


class TimelineScroll(VerticalScroll):
    """#timeline 容器：滚轮手势释放 follow anchor（否则锚底会把每次上翻弹回）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._anchor_check_scheduled = False

    def _schedule_anchor_check(self) -> None:
        """Coalesce a burst of wheel events into one post-layout anchor check."""
        if self._anchor_check_scheduled:
            return
        self._anchor_check_scheduled = True
        self.call_after_refresh(self._anchor_if_at_end)

    def _anchor_if_at_end(self) -> None:
        self._anchor_check_scheduled = False
        try:
            if self.max_scroll_y <= 0 or self.is_vertical_scroll_end:
                # anchor() calls scroll_end(immediate=True). Avoid doing that on
                # every activity tick when the timeline is already anchored.
                if not getattr(self, "_anchored", False) or getattr(self, "_anchor_released", False):
                    self.anchor()
        except Exception:
            pass

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        # Textual's pointer scrolling deliberately passes release_anchor=False
        # to its internal scroll helper. Since the timeline starts anchored to
        # the bottom, that made every wheel movement snap straight back. A
        # wheel gesture is an explicit user navigation, so release the follow
        # anchor before delegating to the normal scroll implementation.
        self.release_anchor()
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.release_anchor()
        super()._on_mouse_scroll_down(event)
        self._schedule_anchor_check()


@dataclass
class Entry:
    """一个逻辑条目：rebuild 工厂 + 当前挂载状态。"""

    kind: str  # "static" | "agent" | "reasoning" | "tool"
    rebuild: Callable[[], Any]  # 返回新 widget（惰性；单向裁剪下裁掉后不再重建）
    snapshot: dict[str, Any] = field(default_factory=dict)
    widget: Any = None
    is_mounted: bool = False
    pinned: bool = False  # 流式中：永不裁剪
    trimmed: bool = False  # 已并入历史快照
    expanded: bool = False  # ToolCallRow expanded class 快照（保留字段兼容）


def _plain_of(widget: Any) -> str:
    """提取 widget 的纯文本渲染（用于历史快照）。

    复合 widget（Vertical 容器：ToolCallRow/AgentStreamBlock/ReasoningBlock）遍历
    其 Static 子孙拼接；单个 Static 直接取 render。失败返回空串（宁可丢一条历史文本
    也不炸 UI）。
    """
    try:
        if isinstance(widget, Static):
            content = widget.render()
            return content.plain if isinstance(content, Text) else str(content)
        # 复合 widget：遍历 Static 子孙
        parts: list[str] = []
        for child in widget.query(Static):
            try:
                # display:none 的明细（未展开的工具详情）跳过，与视觉一致
                if getattr(child.styles, "display", None) == "none":
                    continue
                content = child.render()
                text = content.plain if isinstance(content, Text) else str(content)
            except Exception:
                text = ""
            if text.strip():
                parts.append(text)
        return "\n".join(parts)
    except Exception:
        return ""


def _compact_snapshot(text: str, limit: int = _HISTORY_ENTRY_MAX_CHARS) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _snapshot_of(entry: Entry) -> str:
    """Return a cheap text snapshot for folded history.

    Prefer widget-owned cached text / summaries over walking every descendant;
    expanded tool details and long agent bodies are exactly the cases that made
    trim slices show up as scroll stutters.
    """
    widget = entry.widget
    if widget is None:
        return ""
    try:
        if entry.kind == "agent" and hasattr(widget, "_body_raw"):
            body = getattr(widget, "_body_raw", "") or ""
            worker = getattr(widget, "worker", None)
            prefix = f"[{worker}] ● " if worker else "● "
            return _compact_snapshot(prefix + body.lstrip("\r\n"))
        if entry.kind == "tool" and hasattr(widget, "_summary"):
            summary = widget._summary()  # type: ignore[attr-defined]
            text = summary.plain if isinstance(summary, Text) else str(summary)
            return _compact_snapshot(text)
        if entry.kind == "reasoning" and hasattr(widget, "snapshot"):
            snap = widget.snapshot()
            footer = str(snap.get("footer", "")).strip()
            buffer = str(snap.get("buffer", "")).strip()
            text = footer or buffer
            return _compact_snapshot(text)
    except Exception:
        return ""
    return _compact_snapshot(_plain_of(widget))


class TimelineWindowizer:
    """管理 #timeline 的挂载/裁剪。把挂载总数从 O(事件数) 降到 O(视口)。

    单向：裁剪只发生在 append（增量计数触发），滚动回调不做任何挂载操作。
    """

    def __init__(self, container: Any, app: Any) -> None:
        self._container = container
        self._app = app
        self._entries: list[Entry] = []
        # 顶部只读历史快照（单个 Static，累积被裁剪条目的纯文本）
        self._history: Static | None = None
        self._history_text = ""
        self._history_count = 0
        # 增量维护的已挂载非 pinned 条目数（避免每次 append 全表 enumerate）
        self._mounted_count = 0
        # 裁剪重入锁：mount(before=0) 会引发 scroll_y 变化，防止递归回裁剪
        self._trimming = False
        self._trim_scheduled = False

    # ------------------------------------------------------------ 条目
    def _container_live(self) -> bool:
        return bool(
            getattr(self._container, "is_attached", False)
            and getattr(self._container, "is_mounted", False)
        )

    def append(self, entry: Entry, widget: Any = None) -> tuple:
        """追加条目并立即挂载（同步 mount，与原 _write/_write_line 语义一致）。

        widget 非 None 时直接用（避免预建流式块被 rebuild 替换）。返回
        (AwaitMount|None, entry)：流式调用方可 await 确保子组件就绪再 set_body。
        """
        self._entries.append(entry)
        # 容器未挂载（拆卸中）：只记条目，不挂载（避免 MountError）
        if not self._container_live():
            entry.is_mounted = False
            return None, entry
        if widget is None:
            widget = entry.rebuild()
        entry.widget = widget
        entry.is_mounted = True
        if not entry.pinned:
            self._mounted_count += 1
        ret = self._container.mount(widget)
        # 超限后排到短定时器分片裁剪；append 本身不承担大批量 remove/render。
        if self._mounted_count > _MAX_MOUNTED:
            self._schedule_trim()
        return ret, entry

    def _schedule_trim(self) -> None:
        if self._trim_scheduled or self._trimming or not self._container_live():
            return
        self._trim_scheduled = True
        try:
            self._container.set_timer(_TRIM_INTERVAL, self._trim_oldest, name="timeline-trim")
            scheduled = True
        except Exception:
            scheduled = False
        if not scheduled:
            self._trim_scheduled = False
            self._trim_oldest()

    def _trim_oldest(self) -> None:
        """把最旧的非 pinned 已挂载条目裁剪进历史快照，一个刷新只处理小切片。

        目标 = _MAX_MOUNTED - _TRIM_BATCH（迟滞：一次裁一批，不是每条都裁）。
        pinned（流式中块）跳过，保留挂载。
        """
        self._trim_scheduled = False
        if self._trimming or not self._container_live():
            return
        target = _MAX_MOUNTED - _TRIM_BATCH
        if self._mounted_count <= target:
            return
        self._trimming = True
        try:
            trimmed_texts: list[str] = []
            trimmed_count = 0
            for e in self._entries:
                if self._mounted_count <= target:
                    break
                if trimmed_count >= _TRIM_SLICE:
                    break
                if not e.is_mounted or e.pinned or e.trimmed:
                    continue
                # 抓纯文本快照（widget 还挂着，能 render）
                if e.widget is not None:
                    e.expanded = getattr(e.widget, "expanded", False) or e.expanded
                    text = _snapshot_of(e)
                    if text.strip():
                        trimmed_texts.append(text)
                    try:
                        e.widget.remove()
                    except Exception:
                        pass
                e.widget = None
                e.is_mounted = False
                e.trimmed = True
                self._mounted_count -= 1
                trimmed_count += 1
            if trimmed_count:
                self._append_history("\n".join(trimmed_texts), count=trimmed_count)
        finally:
            self._trimming = False
        if self._mounted_count > target:
            self._schedule_trim()

    def _append_history(self, text: str, *, count: int) -> None:
        """把被裁剪条目的纯文本追加进内部历史，并只渲染一行轻量摘要。"""
        self._history_count += count
        if text:
            combined = (self._history_text + "\n" + text) if self._history_text else text
        else:
            combined = self._history_text
        # 超长截断：丢最旧的一段（保留尾部，靠近视口的更相关）
        if len(combined) > _HISTORY_MAX_CHARS:
            combined = combined[-_HISTORY_MAX_CHARS:]
            nl = combined.find("\n")
            if nl != -1:
                combined = combined[nl + 1:]
        self._history_text = combined
        body = self._history_body()
        if self._history is None or not getattr(self._history, "is_mounted", False):
            ph = Static(body)
            ph.add_class("timeline-placeholder")
            self._history = ph
            try:
                self._container.mount(ph, before=0)
            except Exception:
                self._history = None
        else:
            self._history.update(body, layout=False)

    def _history_body(self) -> Text:
        tail = " ".join(self._history_text.split())[-_HISTORY_RENDER_TAIL_CHARS:]
        body = _HISTORY_HEAD.format(count=self._history_count)
        if tail:
            body += f" · 最近：{tail}"
        return Text(body, style="dim italic", no_wrap=False)

    def clear(self) -> None:
        """清空所有条目与挂载（/new 新会话）。"""
        self._entries.clear()
        self._history = None
        self._history_text = ""
        self._history_count = 0
        self._mounted_count = 0
        self._trimming = False
        self._trim_scheduled = False
        try:
            self._container.remove_children()
        except Exception:
            pass

    def mark_pinned(self, entry: Entry, pinned: bool) -> None:
        """流式块 pin/unpin。解除 pin 后计入挂载计数（后续可被裁剪）。"""
        if entry.pinned == pinned:
            return
        entry.pinned = pinned
        if entry.is_mounted:
            # pin→unpin：现在算入可裁剪挂载数；unpin→pin：从计数移出
            self._mounted_count += 1 if not pinned else -1
            if not pinned and self._mounted_count > _MAX_MOUNTED and not self._trimming:
                self._schedule_trim()

    def find_entry_by_widget(self, widget: Any) -> Entry | None:
        for e in self._entries:
            if e.widget is widget:
                return e
        return None

    def is_mounted(self, entry: Entry) -> bool:
        return entry.is_mounted

    def apply_tool_result(self, entry: Entry, **result_kwargs) -> None:
        """ToolCallRow 收到结果：存快照（裁剪后 widget 已不在，快照仍留在 entry）。"""
        entry.snapshot.update(result_kwargs)

    # ------------------------------------------------------------ 兼容旧接口
    def on_scroll(self) -> None:
        """单向裁剪：滚动不再触发任何挂载操作（旧方案在此 fold/unfold 导致卡死）。"""
        return

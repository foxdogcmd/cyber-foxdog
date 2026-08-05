"""交互式会话 TUI（kimi-code 风格）：裸 `foxdog` / `foxdog chat` 进入。

会话模型：一个会话 = 一段持续对话历史 + 每次任务一个独立 run。
provider/权限链/env/技能注册表/MCP 会话级共享（runner.SessionContext）；
Workspace/FlagGate/Blackboard 题目级隔离（build_run 每次新建），
对话 history 延续（agent 记得上一题怎么打的）。

统一解题平台 & UX：
- /model 引导配模型（双协议，key 不回显，测连接后落盘 0600，热切换）。
- /setting 全局 copilot：随时可用（解题中走 planner 模型），受限配置工具 +
  指挥工具（get_status/steer_team/kill_worker/suppress_intent/set_budget）。
- 时间线工具调用是可折叠 ToolCallRow（dim 摘要，点击/回车展开详情）。
- Ctrl+C 有选区时复制；无选区时运行中中断、空闲退出，始终走 TUI 清理逻辑。
- 预算默认无限（None），/budget 动态调整（运行中立即生效）。
- 黑板面板三节渲染（尾部折叠 +N more，intents 状态分组）；/board 全屏切换。

事件泵：emit → SimpleQueue → 定时器渲染；execute_run finally 负责资源清理。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual._cells import cell_len
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .config import ConfigStore, ModelEndpoint
from .core.goal import EXIT_BUDGET, EXIT_COMPLETE, EXIT_ERROR, EXIT_SOLVED
from .core.loop import AgentLoop
from .core.orchestrator import ToolOrchestrator
from .core.steer import SteerQueue
from .executor.background import BackgroundRegistry
from .llm.messages import system_message, user_message
from .protocol.types import Event
from .runner import SessionContext, SolveRun, build_run, build_session, execute_run
from .store.rollout import RunSummary, find_run_summary, list_run_summaries, read_records, replay_history
from .tools import ToolContext, ToolRegistry
from .tools.builtin.config_edit import register_config_tools
from .tools.builtin.copilot import register_copilot_tools
from .tui_driver import get_tui_driver_class
from .widgets import (
    ReasoningBlock,
    ToolCallRow,
    AgentStreamBlock,
    BoardItem,
    _INTENT_ICON,
    _INTENT_ORDER,
    agent_text,
    deadend_item,
    fact_item,
    intent_item,
    render_deadends,
    render_facts,
    render_intents,
    render_todos,
)

try:
    from importlib.metadata import version as _pkg_version

    _VERSION = _pkg_version("foxdog")
except Exception:  # noqa: BLE001 - 未安装元数据（源码直跑）
    _VERSION = "dev"

WELCOME = "🦊 foxdog v{v} — CTF 统一解题平台\n输入题目描述开始解题；解题中直接打字回车 = steer 指挥；/help 查看命令。"
CONFIG_HINT = (
    "⚠ 未配置模型：请先 /model 配置（协议/base_url/api_key/模型名，落盘 ~/.config/foxdog/config.toml）；"
    "冒烟测试可用 FOXDOG_PROVIDER=fake。"
)
HELP_TEXT = """\
不会用就 /setting 直接说需求（自然语言配置/指挥）。
任务：
  /fetch <id|URL>     拉题目描述进输入框（平台 fetch_url 或直接抓 URL）
  /target <URL>       设下一任务靶机        例：/target http://10.0.0.5:8080
  /format <REGEX>     设下一任务 flag 格式  例：/format PCTF\\{[^{}]*\\}
  /workers <N>        设下一任务 worker 数  例：/workers 4
配置：
  /model              查看/引导式配置模型（/model list 看当前，/model show 看全部端点及状态）
  /setting <要求>     对话式配置与指挥      例：/setting worker 数改 6
  /budget             查看预算；/budget minutes 30 | tokens 500000 | off
控制：
  <打字回车>          解题中 = steer 指挥
  Esc                 中断当前任务（/board 全屏时 = 返回）
  Ctrl+B              将当前等待中的 shell 命令转到后台
  Ctrl+C              有选区复制；运行中中断；空闲时连按两次退出
  /new                清空对话历史开新会话
  /quit               退出
查看：
  /board              全屏黑板视图（再按或 Esc 返回）
  /runs               历史 run
  /resume [run_id]    无缝续跑上一任务（省略则续最近一个）
  /help               本清单"""

    # /model 引导式配置：菜单步骤（↑↓ 选择回车确认，Esc 任意步取消）
_PROTOCOL_OPTIONS: list[tuple[str, str]] = [
    ("openai", "openai（chat completions）— DeepSeek/Kimi/兼容 OpenAI Chat"),
    ("responses", "openai（responses）— OpenAI o-series Responses API"),
    ("anthropic", "anthropic — Claude/Kimi Code Messages API"),
]
_PRESET_OPTIONS: list[tuple[str, str, dict | None]] = [
    ("manual", "全手动（自己填 base_url）", None),
    ("kimi-open", "Kimi 开放平台（OpenAI 兼容）", {
        "protocol": "openai", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2",
    }),
    ("kimi-code", "Kimi Code（Anthropic 兼容）", {
        "protocol": "anthropic", "base_url": "https://api.kimi.com/coding/", "model": "kimi-for-coding",
    }),
    ("deepseek-open", "DeepSeek（OpenAI 兼容）", {
        "protocol": "openai", "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
    }),
    ("deepseek-anth", "DeepSeek（Anthropic 兼容）", {
        "protocol": "anthropic", "base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat",
    }),
]
_SETUP_STEPS = ["protocol", "preset", "base_url", "api_key", "model"]

SETTING_SYSTEM = """\\
你是 foxdog 的会话副驾（copilot），与用户进行连续对话。用户用自然语言提要求，你调用工具完成，
可以多轮追问直到确认需求完整。

能力分两类：
1. 配置：list_deepseek_models 查官方实时模型列表、set_model（protocol 认 openai/responses/anthropic）、
   set_platform（平台地址/worker 数）、set_runtime（local/Docker 工具环境）、
   configure_claude_code（容器 Claude + 本机 CC Switch 配置注入）、
   add/remove_mcp_server、set_agent（权限/步数/预算）、get_config 看现状。
2. 指挥（解题中）：get_status 看任务/worker/token 状态、steer_team 指挥全体方向、
   kill_worker 终止某个 worker、suppress_intent 压制某个方向、set_budget 动态调预算。

常见配置模板：
- DeepSeek（OpenAI 格式）：protocol=openai, base_url=https://api.deepseek.com
  先 list_deepseek_models 拿实时模型列表，不要凭记忆杜撰模型名。
  思考模式默认 enabled；reasoning_effort 可选 low/medium/high/xhigh/max。
- DeepSeek（Anthropic 格式）：protocol=anthropic, base_url=https://api.deepseek.com/anthropic
- Kimi 开放平台：protocol=openai, base_url=https://api.moonshot.cn/v1
  模型名 kimi-k3 / kimi-k2.7-code / kimi-k2.6；K3 用 reasoning_effort，K2.x 用 thinking。
- Kimi Code（Claude Code 接入）：protocol=anthropic, base_url=https://api.kimi.com/coding/
  模型名按会员档位（kimi-for-coding / k3-256k / k3 等）。
- OpenAI 官方 o-series：protocol=responses, base_url=https://api.openai.com/v1

通用规则：
- api_key 缺失时才向用户询问，且不要回显或记录到对话里。
- max_tokens 是单次输出上限，不是整场预算；不确定时留空。
- 字段不确定就先 get_config 再问清；改完用中文简要确认。
- 不要做无关操作；用户的追问要结合之前的对话上下文回应（这是连续对话）。
"""

_ACTIVITY_SPINNER = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"
_LEAKED_SGR_MOUSE_RE = re.compile(r"(?:\x1b)?\[<\d+;-?\d+;-?\d+[mM]")
_CTRL_C_EXIT_WINDOW = 2.0
_CTRL_C_DUPLICATE_WINDOW = 0.08
_EVENT_PUMP_LIMIT = 256


@dataclass
class SlashCommand:
    name: str
    desc: str
    handler: Callable[[str], None]  # 参数为命令后的文本


class CommandInput(TextArea):
    """命令输入框（多行）：Enter 提交，Shift+Enter/Ctrl+J 换行；长粘贴折叠占位。

    既有行为全部保留：`/` 命令补全弹层与 /model 菜单的按键路由、▏ 竖线软件
    光标、placeholder 闪烁关相位补空格不左右跳。

    竖线光标根因（实测排查）：Textual 启动时写 `?25l` 隐藏硬件光标，Input/
    TextArea 都是用组件样式画**反色块软件光标**；DECSCUSR（`CSI 6 q`）只影响
    硬件光标，序列注入无效。修法是 `_draw_cursor=False` 关掉 TextArea 原生
    反色光标，render_line 里统一覆盖 `▏`，与 Claude Code 视觉一致。

    长粘贴：>200 字符的粘贴进 paste store，输入框只插青色占位符
    `[粘贴的文本#N]`；提交时 `expand_pastes` 还原全文。占位符按普通文本删，
    找不到占位符的 paste 项随 clear 丢弃（简单方案，不做 atomic delete）。
    """

    _BAR = "▏"
    _PASTE_TOKEN_RE = re.compile(r"\[粘贴的文本#(\d+)\]")
    PASTE_FOLD_THRESHOLD = 200

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("soft_wrap", True)  # 软换行：无横向滚动条
        kwargs.setdefault("highlight_cursor_line", False)  # 光标行不整行上色
        super().__init__(*args, **kwargs)
        self._pastes: dict[int, str] = {}
        self._paste_seq = 0
        self.password = False  # 兼容旧 Input API：True 时掩码显示（/model api_key）

    # ------------------------------------------------------------ 永久聚焦
    def _on_blur(self, _event: events.Blur) -> None:
        """输入框常驻焦点（codex/kimi-code 终端语义）：失焦后下一帧恢复。

        除应用退出外，任何路径失焦（点击别处/焦点被拢走）都拉回，
        保证切回窗口后随手打字一定落进输入框。
        """
        app = self.app
        if not isinstance(app, ChatApp) or not app.is_running or app._exit:
            return
        self.call_later(self._restore_focus)

    def _restore_focus(self) -> None:
        app = self.app
        if not isinstance(app, ChatApp) or not app.is_running or app._exit:
            return
        if app.screen.focused is not self:
            self.focus()

    # ------------------------------------------------------------ 兼容旧 Input API
    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.text = value

    @property
    def cursor_position(self) -> int:
        """光标在全文中的字符偏移（兼容旧单行 Input 语义）。"""
        row, col = self.selection.end
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + col

    @cursor_position.setter
    def cursor_position(self, position: int) -> None:
        position = max(0, min(position, len(self.text)))
        row = 0
        for line in self.text.split("\n"):
            if position <= len(line):
                self.move_cursor((row, position))
                return
            position -= len(line) + 1
            row += 1

    def clear(self) -> None:
        super().clear()
        self._pastes.clear()

    # ------------------------------------------------------------ 长粘贴折叠
    async def _on_paste(self, event: events.Paste) -> None:
        if event.text and len(event.text) > self.PASTE_FOLD_THRESHOLD:
            self._paste_seq += 1
            self._pastes[self._paste_seq] = event.text
            token = f"[粘贴的文本#{self._paste_seq}]"
            if result := self._replace_via_keyboard(token, *self.selection):
                self.move_cursor(result.end_location)
                self.focus()
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    def expand_pastes(self, text: str) -> str:
        """提交前把占位符还原成真实粘贴内容；孤儿占位符（已被删改）保留原文。"""
        return self._PASTE_TOKEN_RE.sub(
            lambda m: self._pastes.get(int(m.group(1)), m.group(0)), text
        )

    # ------------------------------------------------------------ 渲染（▏ 光标 / 掩码 / 占位符高亮）
    @property
    def _draw_cursor(self) -> bool:
        return False  # 关掉原生反色块光标，统一用 ▏ 覆盖

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self.password and self.text:
            strip = self._mask_strip(strip)
        if not self.text:
            if y != 0:
                return strip
            if self.has_focus and self._cursor_visible:
                # 原生光标已被 _draw_cursor 关掉，直接在 placeholder 前插入竖线。
                return self._bar_overlay(strip, 0, insert=True)
            # 闪烁关闭相位用普通空格占位，placeholder 的起点不会左右跳动。
            return self._pad_placeholder(strip)
        strip = self._style_paste_tokens(strip)
        scroll_x, scroll_y = self.scroll_offset
        cursor_x, cursor_y = self._cursor_offset
        if self.has_focus and self._cursor_visible and scroll_y + y == cursor_y:
            # 非空输入：插入式；原字符会以正常文本样式重新绘制，不留反色块。
            strip = self._bar_overlay(strip, cursor_x - scroll_x, insert=True)
        return strip

    def _mask_strip(self, strip: Strip) -> Strip:
        """password 模式：所有字符替换为 •（等宽，光标偏移不受影响）。"""
        return Strip([Segment("•" * len(seg.text), seg.style) for seg in strip])

    def _style_paste_tokens(self, strip: Strip) -> Strip:
        """把 `[粘贴的文本#N]` 占位符染成青色加粗（在 ▏ 覆盖之前做， spans 对齐）。"""
        text = strip.text
        if "粘贴的文本" not in text:
            return strip
        spans = [m.span() for m in self._PASTE_TOKEN_RE.finditer(text)]
        if not spans:
            return strip
        token_style = Style(color="cyan", bold=True)
        out: list[Segment] = []
        pos = 0
        for seg in strip:
            buf = ""
            buf_style = seg.style
            for ch in seg.text:
                style = (
                    token_style
                    if any(start <= pos < end for start, end in spans)
                    else seg.style
                )
                if buf and style != buf_style:
                    out.append(Segment(buf, buf_style))
                    buf = ""
                buf += ch
                buf_style = style
                pos += 1
            if buf:
                out.append(Segment(buf, buf_style))
        return Strip(out)

    def _pad_placeholder(self, strip: Strip) -> Strip:
        """空输入时在头部预留一个普通空格（样式与首个 segment 一致）。"""
        first_style = None
        for seg in strip:
            if seg.text:
                first_style = seg.style
                break
        return Strip([Segment(" ", first_style or Style()), *strip])

    def _bar_overlay(self, strip: Strip, x: int, insert: bool) -> Strip:
        """在光标格画 ▏。insert=True 插入（字符保留，宽度 +1）；False 替换该格。

        原字符用邻近正常文本样式恢复；竖线单独使用已去反色的 `text-area--cursor`
        组件样式。默认主题的光标是显式反色 fg/bg（不是 reverse 标志），仅
        Style(reverse=False) 洗不掉，且 Monochrome filter 要求样式为实体 Style。
        """
        if x < 0:
            return strip
        cells: list[tuple[str, Style | None]] = []
        pos = 0
        cursor_idx: int | None = None
        for seg in strip:
            for ch in seg.text:
                if cursor_idx is None and pos <= x < pos + cell_len(ch):
                    cursor_idx = len(cells)
                cells.append((ch, seg.style))
                pos += cell_len(ch)
        if not cells:
            return strip
        # 邻近正常样式：优先光标后的字符，其次光标前，最后空 Style
        normal_style = (
            (cells[cursor_idx + 1][1] if cursor_idx is not None and cursor_idx + 1 < len(cells) else None)
            or (cells[cursor_idx - 1][1] if cursor_idx is not None and cursor_idx > 0 else None)
            or (cells[0][1] if cursor_idx is None else None)
            or Style()
        )
        bar_style = self.get_component_rich_style("text-area--cursor")
        out: list[Segment] = []
        for i, (ch, style) in enumerate(cells):
            if i == cursor_idx:
                out.append(Segment(self._BAR, bar_style))
                if insert:
                    out.append(Segment(ch, normal_style))
                # insert=False：替换该格（预留空格位置，文本零位移）
            else:
                out.append(Segment(ch, style))
        if cursor_idx is None:  # 光标在末尾（pad 的空格之后）
            out.append(Segment(self._BAR, bar_style))
        return Strip(out)

    async def _on_key(self, event) -> None:
        app = self.app
        if isinstance(app, ChatApp) and app._resume_picker_active:
            if event.key == "escape":
                app._hide_resume_picker(cancelled=True)
            elif event.key == "up":
                app._resume_picker_move(-1)
            elif event.key == "down":
                app._resume_picker_move(1)
            elif event.key in ("enter", "tab"):
                app._resume_picker_select()
            else:
                app._hide_resume_picker(cancelled=False)
                await super()._on_key(event)
                return
            event.prevent_default()
            event.stop()
            return
        if isinstance(app, ChatApp) and app._setup is not None:
            # /model 引导流程：Esc 任意步取消；菜单步 ↑↓/回车选择
            if event.key == "escape":
                app._cancel_setup()
            elif app._menu_active and event.key == "up":
                app._menu_move(-1)
            elif app._menu_active and event.key == "down":
                app._menu_move(1)
            elif app._menu_active and event.key in ("enter", "tab"):
                app._menu_select()
            else:
                await super()._on_key(event)
                return
            event.prevent_default()
            event.stop()
            return
        if isinstance(app, ChatApp) and app._ac_visible:
            if event.key == "up":
                app._ac_move(-1)
            elif event.key == "down":
                app._ac_move(1)
            elif event.key in ("enter", "tab"):
                app._ac_accept()
            elif event.key == "escape":
                app._ac_hide()
            else:
                await super()._on_key(event)
                return
            event.prevent_default()
            event.stop()
            return
        if event.key == "enter" and isinstance(app, ChatApp):
            # Enter = 提交。TextArea 默认 enter 插换行，必须先拦且不调 super；
            # 直调鸭子类型 handler（event.value/event.input），不引入新 Message 类。
            event.prevent_default()
            event.stop()
            app.on_input_submitted(SimpleNamespace(value=self.text, input=self))
            return
        if event.key in ("shift+enter", "ctrl+j"):
            # Shift+Enter / Ctrl+J = 换行（终端能送出哪个就收哪个，两个都收）。
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)


async def _default_test_endpoint(ep: ModelEndpoint) -> tuple[bool, str]:
    """/model 引导配置的连接测试（发一条 hi；可 monkeypatch）。"""
    try:
        if ep.protocol == "anthropic":
            from .llm.anthropic_client import AnthropicClient

            client = AnthropicClient(ep)
        else:
            from .llm.client import LLMClient

            client = LLMClient(ep)
        await asyncio.wait_for(
            client.stream_chat([{"role": "user", "content": "hi"}], [], lambda _d: None),
            timeout=20,
        )
        return True, "连接正常"
    except Exception as exc:  # noqa: BLE001 - 测试结果只用于展示
        return False, f"{type(exc).__name__}: {exc}"


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _strip_leaked_mouse_reports(value: str, cursor: int) -> tuple[str, int]:
    """Remove complete orphaned SGR mouse reports without touching other CSI-like text."""
    matches = list(_LEAKED_SGR_MOUSE_RE.finditer(value))
    if not matches:
        return value, cursor
    removed_before_cursor = sum(
        max(0, min(match.end(), cursor) - match.start())
        for match in matches
        if match.start() < cursor
    )
    return _LEAKED_SGR_MOUSE_RE.sub("", value), max(0, cursor - removed_before_cursor)


class ChatApp(App):
    """foxdog 交互式会话前端（统一解题平台）。"""

    TITLE = "foxdog"
    ALLOW_SELECT = True  # 文本选择：拖选复制（Ctrl+C 复制）

    CSS = """
    #status { dock: top; height: 1; padding: 0 1; background: $panel; }
    #workspace, #boardfull { height: 1fr; }
    #timeline { width: 1fr; padding: 1 2 0 2; }
    #board {
        display: none;
        width: 46;
        min-width: 40;
        max-width: 34%;
        background: $surface;
        border-left: solid $primary-darken-2;
    }
    #board-title {
        height: 2;
        padding: 0 1;
        color: $primary;
        text-style: bold;
        background: $surface-lighten-1;
        content-align: left middle;
    }
    #board .board-section {
        height: auto;
        max-height: 33%;
        padding: 1 1 0 1;
        border-bottom: solid $primary-darken-3;
        overflow-y: auto;
    }
    #board .board-section:hover { background: $surface-lighten-1; }
    #boardfull { display: none; padding: 1 2; }
    #setting-full {
        display: none;
        height: 1fr;
        padding: 1 2;
        background: $background;
        overflow-y: auto;
    }
    #setting-full .setting-header {
        color: $primary;
        text-style: bold;
        padding: 0 0 1 0;
        border-bottom: solid $primary-darken-2;
        margin-bottom: 1;
    }
    #boardfull Static {
        height: auto;
    }
    #boardfull > Vertical {
        height: auto;
        padding: 1 1;
        border-bottom: solid $primary-darken-3;
    }
    #boardfull .board-section-title {
        color: $primary;
        text-style: bold;
        padding: 0 0 1 0;
    }
    #boardfull .board-group-title {
        color: $text-muted;
        text-style: bold;
        padding: 1 0 0 0;
    }
    #boardfull .board-more {
        color: $text-muted;
        padding: 0 0 0 1;
    }
    #bottom {
        dock: bottom;
        height: auto;
        max-height: 60%;
        background: $background;
    }
    #todos { display: none; height: auto; max-height: 9; padding: 1 2; border-bottom: solid $panel; }
    #resume-menu { display: none; height: auto; max-height: 12; border-bottom: solid $panel; }
    #ac { display: none; height: auto; max-height: 9; border-bottom: solid $panel; }
    #menu { display: none; height: auto; max-height: 9; border-bottom: solid $panel; }
    #activity { display: none; height: 1; margin: 0 3; color: $text-muted; }
    .jump { color: $primary; text-align: right; }
    .timeline-placeholder {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        text-style: italic;
        background: $surface;
    }
    #input-row {
        height: auto;
        max-height: 12;
        margin: 0 2;
        padding: 0 1;
        border: round $panel;
        background: $background;
        align: left middle;
    }
    #input-row:focus-within { border: round $primary; }
    #prompt-mark { width: 2; height: 1; color: $primary; text-style: bold; }
    CommandInput {
        width: 1fr;
        height: 1;
        max-height: 10;
        border: none;
        padding: 0;
        background: $background;
    }
    CommandInput:focus { border: none; background: $background; background-tint: $background 0%; }
    CommandInput > .text-area--cursor { background: $background; color: $primary; text-style: bold; }
    #shortcuts { height: 1; padding: 0 3; color: $text-disabled; }
    .user-msg { background: $surface-lighten-2; text-style: bold; width: 100%; padding: 0 1; margin: 1 0; }
    .gap-top { margin-top: 1; }
    """
    BINDINGS = [
        Binding("ctrl+c", "handle_ctrl_c", "复制/中断/双击退出", priority=True),
        ("escape", "interrupt_task", "中断/返回"),
        ("ctrl+b", "background_command", "命令转后台"),
        ("ctrl+t", "toggle_todos", "任务清单"),
        ("ctrl+end", "jump_bottom", "回到底部"),
        ("q", "quit", "退出"),
    ]

    def __init__(
        self,
        workspace: Path | None = None,
        overrides: list[str] | None = None,
        config_path: Path | None = None,  # 测试注入：配置写到临时文件
        resume_run_id: str | None = None,  # 非 None：进 TUI 后自动进入 resume 流程
    ) -> None:
        super().__init__(driver_class=get_tui_driver_class())
        self._workspace = workspace
        self._overrides = list(overrides or [])
        # resume 入口：""=打开运行选择器（列出历史 run 让用户挑）；具体 id=直接续该 run
        self._resume_run_id = resume_run_id
        self.store = ConfigStore(config_path)
        self.exit_code = 0
        self.session: SessionContext | None = None
        self.history: list[dict] = []  # 持续对话历史（跨任务延续）
        self.steer = SteerQueue()
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.current_run: SolveRun | None = None
        self.last_run: SolveRun | None = None
        self._solve_task: asyncio.Task | None = None
        self.busy = False
        self._setting_busy = False
        self._setting_history: list[dict] = []  # persistent /setting conversation
        self._setting_page = False  # /setting overlay visible
        # /setting 隐私金库：用户输入里的 API key 在进模型 history 前替换成
        # secret://setting/N 句柄，配置工具（set_model）执行时按句柄 resolve 回真实值落盘。
        # 真实 key 永不进模型消息、也不回显。session 级持久（跨多次 /setting）。
        from .tools.builtin.config_edit import SecretVault
        self._setting_secrets = SecretVault()
        # 会话级任务参数（斜杠命令设置）
        self.opt_target: str | None = None
        self.opt_format: str | None = None
        self.opt_workers = 1
        # 时间线状态
        self._stream_widgets: dict[tuple[str | None, str], AgentStreamBlock] = {}
        self._stream_raw: dict[tuple[str | None, str], str] = {}
        self._pending_stream_updates: set[tuple[str | None, str]] = set()
        self._reasoning_blocks: dict[tuple[str | None, str], ReasoningBlock] = {}
        self._rows: dict[str, ToolCallRow] = {}  # call_id → 工具调用行
        #: 相邻合并状态：同一逻辑消息跨多个 model request 的文本并到同一 ● 块
        self._last_agent_widget: AgentStreamBlock | None = None
        self._last_agent_key: tuple[str | None, str] | None = None
        self._last_agent_worker: str | None = None
        self._prev_event_kind: str | None = None
        self._last_kind: str | None = None  # 上一个时间线块类型（工具组连续不加间距）
        self._timeline_windowizer = None  # on_mount 后挂载：视口窗口化
        self._board_full = False
        self._todos_visible = False
        self._board_signature: str | None = None
        self._activity = ""  # spinner 状态行的当前活动描述
        self._activity_started_at = time.monotonic()
        self._activity_frame = 0
        self._activity_timer: Timer | None = None
        self._terminal_recovery_until = 0.0
        self._idle_ctrl_c_armed_at = 0.0
        # /model 引导式配置状态（None=不在配置流程中）
        self._setup: dict[str, Any] | None = None
        self._setup_models: list[str] = []  # /model 拉取到的候选 model id（tab 补全/校验）
        # 平台监控：poll_seconds>0 时定时轮询平台状态，缓存题目计数/列表
        self._platform_stats: dict[str, Any] = {"total": None, "solved": None, "challenges": []}
        self._platform_monitor = False  # 是否启用了平台监控面板
        # resume 选择器：由 `foxdog resume` 或空参数 `/resume` 打开。
        self._resume_picker_active = False
        self._resume_candidates: list[RunSummary] = []
        self._resume_from: Path | None = None
        #: /model 连接测试器（测试可 monkeypatch）
        self.test_endpoint: Callable[[ModelEndpoint], Any] = _default_test_endpoint
        self._ac_visible = False
        self._menu_active = False
        # 命令注册表（补全与执行共用一份定义）
        self._commands: list[SlashCommand] = [
            SlashCommand("/help", "命令清单", lambda a: self._write_line(HELP_TEXT)),
            SlashCommand("/model", "查看/配置模型", self._cmd_model),
            SlashCommand("/setting", "对话式配置与指挥", self._cmd_setting),
            SlashCommand("/budget", "查看/动态调整预算", self._cmd_budget),
            SlashCommand("/board", "全屏黑板视图切换", self._cmd_board),
            SlashCommand("/fetch", "拉取题目描述进输入框", self._cmd_fetch),
            SlashCommand("/mode", "切换权限模式 yolo|plan", self._cmd_mode),
            SlashCommand("/workers", "设置下一任务 worker 数", self._cmd_workers),
            SlashCommand("/target", "设置下一任务靶机地址", self._cmd_target),
            SlashCommand("/format", "设置下一任务 flag 格式正则", self._cmd_format),
            SlashCommand("/runs", "列出历史 run", lambda a: self._show_runs()),
            SlashCommand("/resume", "续跑上一任务（/resume [run_id]）", self._cmd_resume),
            SlashCommand("/new", "清空对话历史开新会话", self._cmd_new),
  # /cancel removed — Esc cancels setup and overlays
            SlashCommand("/quit", "退出", lambda a: asyncio.create_task(self.action_quit())),
        ]

    # ------------------------------------------------------------ 布局
    def compose(self) -> ComposeResult:
        yield Static("foxdog  正在启动…", id="status")
        # 纯展示容器统一不可聚焦：点击不抢焦点，键盘永远留在输入框（终端提示符语义）
        from .timeline_window import TimelineScroll
        timeline = TimelineScroll(id="timeline")
        timeline.can_focus = False
        boardfull = VerticalScroll(id="boardfull")
        boardfull.can_focus = False
        setting_full = VerticalScroll(id="setting-full")
        setting_full.can_focus = False
        ac_menu = OptionList(id="ac")
        ac_menu.can_focus = False  # 键盘导航由 CommandInput 代理，鼠标点只会抢焦点
        resume_menu = OptionList(id="resume-menu")
        resume_menu.can_focus = False  # 键盘导航由 CommandInput 代理
        setup_menu = OptionList(id="menu")
        setup_menu.can_focus = False
        with Horizontal(id="workspace"):
            with timeline:
                pass
            with Vertical(id="board"):
                yield Static("协作黑板", id="board-title")
                yield Static(render_facts([], compact=True), id="facts", classes="board-section")
                yield Static(render_deadends([], compact=True), id="deadends", classes="board-section")
                yield Static(render_intents([], compact=True), id="intents", classes="board-section")
                # 平台监控面板：仅启用平台监控（status_url+poll_seconds）时显示
                monitor = Static("", id="monitor", classes="board-section")
                monitor.styles.display = "none"
                yield monitor
        with boardfull:
            yield Vertical(id="bf-facts")
            yield Vertical(id="bf-deadends")
            yield Vertical(id="bf-intents")
        with setting_full:
            yield Static("foxdog 副驾（/setting）  esc/ctrl+c 返回", classes="setting-header")
            yield Static("", id="setting-log")
        with Vertical(id="bottom"):
            yield Static("", id="todos")
            yield resume_menu
            yield ac_menu
            yield setup_menu
            yield Static("", id="activity")
            with Horizontal(id="input-row"):
                yield Static("›", id="prompt-mark")
                yield CommandInput(placeholder="输入题目或指令")
            yield Static("/ 命令 · ctrl+t 任务 · ctrl+c 复制/双击退出 · q 退出", id="shortcuts")

    async def on_mount(self) -> None:
        self.set_interval(0.5, self._tick_half_second)
        self._activity_timer = self.set_interval(0.1, self._tick_activity_animation, pause=True)
        self.query_one("#timeline", VerticalScroll).anchor()  # 新内容自动锚底，上翻自动脱锚
        # 视口窗口化：卸载视口外旧 widget 换占位符，滚回重建（修滚动卡顿）
        from .timeline_window import TimelineWindowizer
        self._timeline_windowizer = TimelineWindowizer(
            self.query_one("#timeline", VerticalScroll), self
        )
        try:
            self.session = await build_session(overrides=self._overrides)
        except Exception as exc:  # noqa: BLE001 - 装配失败给指引不崩溃
            self._write_line(f"会话装配失败: {type(exc).__name__}: {exc}", "red")
            self._write_line(CONFIG_HINT, "yellow")
            self.query_one(CommandInput).focus()
            return
        model = self.session.cfg.solver.model or self.session.cfg.solver.provider or "未配置"
        if not self._provider_ready():
            self._write_line("首次使用请先 /model 配置模型。", "yellow")
            self._write_line(CONFIG_HINT, "yellow")
        # 平台默认：worker 数与 flag 格式
        platform = self.session.cfg.platform
        self.opt_workers = platform.default_workers if platform.default_workers >= 1 else 1
        self.opt_format = platform.flag_format or None
        self.set_interval(0.1, self._pump_events)
        self.set_interval(2.0, self._refresh_board)
        self.set_interval(1.0, self._refresh_status)
        # 平台监控：配置了 status_url + poll_seconds>0 则定时轮询题目状态
        if platform.status_url and platform.poll_seconds > 0:
            self._platform_monitor = True
            interval = max(5.0, platform.poll_seconds)
            self.set_interval(interval, self._tick_platform_poll)
            asyncio.create_task(self._poll_platform())  # 启动即拉一次
        self._refresh_status()
        self.query_one(CommandInput).focus()
        # resume 入口（foxdog resume）：进 TUI 后自动进入续跑/选择流程
        if self._resume_run_id is not None:
            self._enter_resume(self._resume_run_id)

    def _enter_resume(self, run_id: str) -> None:
        """foxdog resume 入口：具体 id 直接续；空串弹出 ↑↓ 选择菜单挑一个。"""
        if run_id:
            self._cmd_resume(run_id)
            return
        self._show_resume_picker()

    def _tick_platform_poll(self) -> None:
        asyncio.create_task(self._poll_platform())

    async def _poll_platform(self) -> None:
        """轮询平台状态一次，更新题目计数/列表缓存（失败静默，不炸 TUI）。"""
        if self.session is None:
            return
        from .platform import poll_platform_status
        try:
            stats = await poll_platform_status(
                self.session.cfg.platform,
                run_id=getattr(self.current_run, "ws", None).run_id if self.current_run else "",
            )
        except Exception as exc:  # noqa: BLE001 - 平台不可达不影响解题
            self._write_line(f"平台状态轮询失败（不致命）：{type(exc).__name__}", "dim")
            return
        self._platform_stats = stats
        self._refresh_status()
        if self._platform_monitor:
            self._refresh_monitor_panel()

    def _refresh_monitor_panel(self) -> None:
        """更新题目监控面板（复用黑板侧栏下方的 #monitor 区）。"""
        try:
            panel = self.query_one("#monitor", Static)
        except Exception:
            return
        stats = self._platform_stats
        lines = [Text("题目监控", style="bold")]
        total = stats.get("total")
        solved = stats.get("solved") or 0
        if total is not None:
            lines.append(Text(f"进度：{solved}/{total}", style="green"))
        for ch in (stats.get("challenges") or [])[:12]:
            mark = "✓" if ch.get("solved") else "○"
            style = "green" if ch.get("solved") else "dim"
            title = ch.get("title") or ch.get("id")
            lines.append(Text(f"{mark} {title}", style=style))
        body = Text("\n").join(lines)
        panel.update(body)
        panel.styles.display = "block"

    def _tick_half_second(self) -> None:
        """0.5s 一拍：spinner 状态行、快捷键栏、jump 提示、todo 区。"""
        self._refresh_activity()
        self._refresh_shortcuts()
        self._sync_board_sidebar()
        self._refresh_jumpbar()
        if self._todos_visible:
            self._refresh_todos()

    def _tick_activity_animation(self) -> None:
        """Fast refresh only while solving; idle sessions keep the timer paused."""
        if not self.busy or self.current_run is None:
            return
        self._activity_frame = (self._activity_frame + 1) % len(_ACTIVITY_SPINNER)
        self._refresh_activity()

    def _set_activity_animation(self, active: bool) -> None:
        if self._activity_timer is None:
            return
        if active:
            self._activity_frame = 0
            self._activity_timer.resume()
        else:
            self._activity_timer.pause()

    def _provider_ready(self) -> bool:
        """provider 未配置（无 key）时 TUI 照常启动，提交任务才报错。"""
        assert self.session is not None
        solver = self.session.cfg.solver
        if solver.provider == "fake":
            return True
        return bool(solver.base_url and solver.model and solver.resolved_key())

    # ------------------------------------------------------------ 任务生命周期
    async def _run_task(self, objective: str) -> None:
        assert self.session is not None
        self.busy = True
        resume_from = self._resume_from
        self._resume_from = None
        self._set_activity("准备任务")
        self._set_activity_animation(True)
        self._sync_board_sidebar()
        self._refresh_shortcuts()
        self._refresh_status()
        run: SolveRun | None = None
        try:
            run = await build_run(
                objective,
                self.opt_target,
                self.opt_format,
                [],
                "",
                None,
                None,
                None,
                self.opt_workers,
                None,
                self._overrides,
                emit=self._queue.put,
                steer=self.steer,
                workspace=self._workspace,
                session=self.session,
                history=self.history,
                resume_from=resume_from,
            )
            self.current_run = run
            code = await execute_run(run)
        except asyncio.CancelledError:
            if run is not None:
                self.last_run = run
                if run.interrupted:
                    self._queue.put(("done", EXIT_ERROR, run))
                    return
            elif self.busy:
                self.busy = False
                self.current_run = None
                self._set_activity_animation(False)
                self._sync_board_sidebar()
                self._refresh_shortcuts()
                self._refresh_status()
                return
            raise
        except Exception as exc:  # noqa: BLE001 - 单任务异常不炸会话
            self._write_line(f"任务装配/执行异常: {type(exc).__name__}: {exc}", "red")
            self.busy = False
            self.current_run = None
            self._set_activity_animation(False)
            self._sync_board_sidebar()
            self._refresh_shortcuts()
            self._refresh_status()
            return
        self.last_run = run
        # Keep the task busy until the FIFO event pump has rendered every run
        # event and the final summary. This is the user-visible completion point.
        self._queue.put(("done", code, run))

    def _task_summary(self, run: SolveRun, code: int) -> Text:
        elapsed = _fmt_duration(time.monotonic() - run.started)
        state = run.loop.budget_state
        if run.coordinator is not None and run.coordinator.factory.budget_state is not None:
            state = run.coordinator.factory.budget_state
        tokens = state.total_tokens if state is not None else 0
        writeup = f" writeup: {run.writeup_path}" if run.writeup_path else ""
        if run.interrupted:
            return Text(f"⏹ 已手动中断（用时 {elapsed}，{tokens} tokens）{writeup}", style="yellow")
        if code == EXIT_SOLVED:
            return Text(f"✔ 解题成功 flag={run.gate.flag}（用时 {elapsed}，{tokens} tokens）{writeup}", style="bold green")
        if code == EXIT_COMPLETE:
            return Text(f"✔ 调查完成（旁观者判定任务已收口，用时 {elapsed}，{tokens} tokens）{writeup}", style="bold cyan")
        if code == EXIT_BUDGET:
            return Text(f"⏱ 预算耗尽（用时 {elapsed}，{tokens} tokens）{writeup}", style="yellow")
        return Text(f"✗ 异常终止（用时 {elapsed}，{tokens} tokens）{writeup}", style="red")

    # ------------------------------------------------------------ 事件泵（VerticalScroll 时间线）
    async def _pump_events(self) -> None:
        processed = 0
        while processed < _EVENT_PUMP_LIMIT:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if item is None:
                continue
            if isinstance(item, tuple) and item and item[0] == "done":
                run = item[2] if len(item) > 2 else self.last_run
                assert run is not None
                await self._write(self._task_summary(run, item[1]))
                self.last_run = run
                if self.current_run is run:
                    self.current_run = None
                self.busy = False
                self._set_activity_animation(False)
                self._sync_board_sidebar()
                self._refresh_shortcuts()
                self._refresh_status()
                self.query_one(CommandInput).focus()  # 任务结束后落焦输入框
                continue
            await self._render_event(item)
        self._commit_stream_updates()

    async def _write(self, text: Text) -> None:
        tl = self.query_one("#timeline", VerticalScroll)
        # Preserve URLs: if the Text contains URL-like content, set no_wrap
        # so Rich does not break them mid-way.
        plain = text.plain if hasattr(text, "plain") else str(text)
        if "http" in plain or "https" in plain:
            text.no_wrap = True

        def rebuild():
            return Static(text, classes="gap-top")

        self._tl_append(rebuild, kind="static")
        self._last_kind = "other"
        self._maybe_scroll(tl)

    def _tl_append(self, rebuild: Callable[[], Any], *, kind: str, pinned: bool = False, widget: Any = None) -> tuple:
        """挂到 timeline 窗口器（窗口化）；setting overlay 不窗口化直接 mount。

        widget 非 None：流式块需用预建对象（后续 .append() 注入）时传入，避免 rebuild
        产生独立副本。rebuild 仍作为后续展开重建的工厂。返回 (mount_await, entry)：
        流式调用方可 await mount_await 确保子组件就绪再 set_body。
        """
        from .timeline_window import Entry
        if self._timeline_windowizer is not None:
            entry = Entry(kind=kind, rebuild=rebuild, pinned=pinned)
            mount_await, ent = self._timeline_windowizer.append(entry, widget=widget)
            return mount_await, ent
        # fallback（windowizer 未就绪）：直接 mount
        return self.query_one("#timeline", VerticalScroll).mount(widget if widget is not None else rebuild()), None

    def _maybe_scroll(self, tl: VerticalScroll) -> None:
        """Timeline anchoring owns follow mode; never fight an active user scroll."""
        # `anchor()` keeps new content at the bottom and automatically releases as
        # soon as wheel, keyboard, or scrollbar input moves away from the end.

    def _write_line(self, text: str, style: str = "") -> None:
        """同步上下文可用（mount 防火后由 textual 调度挂载）。"""
        import re as _re
        tl = self.query_one("#timeline", VerticalScroll)
        # Preserve URLs: Rich word-wrapping can break long URLs at character
        # boundaries on narrow terminals. Split on newlines and use no_wrap
        # for segments containing URLs so they stay intact.
        for line in text.splitlines() or [text]:
            has_url = bool(_re.search(r'https?://', line))
            captured = Text(line, style=style, no_wrap=has_url)

            def rebuild(c=captured):
                return Static(c, classes="gap-top")

            self._tl_append(rebuild, kind="static")
        self._last_kind = "other"
        self._maybe_scroll(tl)

    def _write_user(self, text: str) -> None:
        """用户消息：整行反色横条（与 agent 输出明显区分；上下各留一空行）。"""
        tl = self.query_one("#timeline", VerticalScroll)
        captured = f"> {text}"

        def rebuild():
            return Static(captured, classes="user-msg")

        self._tl_append(rebuild, kind="static")
        self._last_kind = "other"
        self._maybe_scroll(tl)

    def _write_agent_snapshot(self, text: str, worker: str | None = None) -> None:
        """Render a completed historical assistant message into the timeline."""
        if not text.strip():
            return
        block = AgentStreamBlock(worker)
        block.add_class("gap-top")
        block.set_body(text)

        def rebuild(raw=text, w=worker):
            restored = AgentStreamBlock(w)
            restored.add_class("gap-top")
            restored.set_body(raw)
            return restored

        self._tl_append(rebuild, kind="agent", widget=block)
        self._last_kind = "other"
        self._maybe_scroll(self.query_one("#timeline", VerticalScroll))

    def _write_tool_snapshot(self, row: ToolCallRow) -> None:
        """Render a historical tool row without registering it as live state."""
        if self._last_kind != "row":
            row.add_class("gap-top")
        self._last_kind = "row"

        def rebuild(src=row):
            restored = ToolCallRow(src.call_id, src.tool_name, src.arguments, src.worker)
            restored.ok = src.ok
            restored.duration_ms = src.duration_ms
            restored.error = src.error
            restored.handle = src.handle
            restored.preview = src.preview
            restored._diff_cache_key = src._diff_cache_key
            restored._diff_cache_lines = src._diff_cache_lines
            if src.has_class("gap-top"):
                restored.add_class("gap-top")
            return restored

        self._tl_append(rebuild, kind="tool", widget=row)
        self._maybe_scroll(self.query_one("#timeline", VerticalScroll))

    def _render_replayed_history(self, item: RunSummary) -> int:
        """Display replayed main-agent rollout records before the resumed run starts."""
        records = read_records(item.path / "rollout.jsonl")
        rows: dict[str, ToolCallRow] = {}
        rendered = 0
        self._write_line(f"── 续跑历史 {item.run_id} ──", "dim")
        for rec in records:
            if rec.get("worker"):
                continue
            rtype = rec.get("type")
            if rtype == "message":
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = str(msg.get("content") or "")
                    if content.strip():
                        self._write_agent_snapshot(content)
                        rendered += 1
            elif rtype == "tool_call":
                for call in rec.get("calls") or []:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    if not call_id:
                        continue
                    row = ToolCallRow(
                        call_id,
                        str(call.get("name") or "tool"),
                        str(call.get("arguments") or ""),
                    )
                    rows[call_id] = row
                    self._write_tool_snapshot(row)
                    rendered += 1
            elif rtype == "tool_result":
                call_id = str(rec.get("call_id") or "")
                row = rows.get(call_id)
                if row is not None:
                    row.ok = bool(rec.get("ok", True))
                    row.duration_ms = 0
                    row.preview = str(rec.get("content") or "")
                    row.error = str(rec.get("error") or "") or None
                    row.handle = rec.get("handle") if isinstance(rec.get("handle"), str) else None
                    if row.is_mounted:
                        row.set_result(row.ok, row.duration_ms, row.preview, row.error, row.handle)
                else:
                    mark = "✓" if rec.get("ok", True) else "✗"
                    self._write_line(
                        f"{mark} {rec.get('name') or 'tool'} {str(rec.get('content') or '')[:120]}",
                        "green" if rec.get("ok", True) else "red",
                    )
                rendered += 1
        return rendered

    def _target_scroll(self) -> VerticalScroll:
        """Route output to the setting overlay when it's open, else timeline."""
        if self._setting_page:
            return self.query_one("#setting-full", VerticalScroll)
        return self.query_one("#timeline", VerticalScroll)

    async def _render_event(self, event: Event) -> None:
        await self._render_event_inner(event)
        self._prev_event_kind = event.kind

    async def _render_event_inner(self, event: Event) -> None:
        kind = event.kind
        interrupted = bool(self.current_run and getattr(self.current_run, "interrupted", False))
        if interrupted and kind in {
            "agent_message_delta",
            "agent_reasoning_delta",
            "model_request_started",
            "turn_started",
            "tool_call_begin",
        }:
            # These may already be queued when Ctrl+C is pressed. Rendering them
            # after cancellation makes a stopped request look alive.
            return
        if kind == "agent_message_delta":
            self._set_activity("撰写回复中")
            delta = event.delta  # type: ignore[attr-defined]
            worker = getattr(event, "worker", None)
            key = self._stream_key(event)
            # 相邻合并：上一事件也是文本流（或刚完成的 model request）且同一 worker，
            # 说明是同一逻辑消息跨多个 model request（腰斩 bug 根因）——并入上一块，
            # 不再新开 ●（一个消息块一个 ●）
            if (
                self._prev_event_kind in ("agent_message_delta", "model_request_complete")
                and self._last_agent_widget is not None
                and self._last_agent_worker == worker
                and self._last_agent_key is not None
            ):
                key = self._last_agent_key
            self._stream_raw[key] = self._stream_raw.get(key, "") + delta
            widget = self._stream_widgets.get(key)
            if widget is None:
                # 流式中块创建：pinned=True 防止窗口化折叠流式中的块。
                # rebuild 闭包从 _stream_raw[key] 恢复正文（会话级存活，flush 才清）。
                stream_raw = self._stream_raw

                def rebuild(w=worker, k=key, sr=stream_raw):
                    blk = AgentStreamBlock(w)
                    blk.add_class("gap-top")
                    raw = sr.get(k)
                    if raw:
                        blk.set_body(raw)
                    return blk

                if self._setting_page:
                    # /setting overlay 不窗口化：直接 mount
                    widget = AgentStreamBlock(worker)
                    widget.add_class("gap-top")
                    self._stream_widgets[key] = widget
                    await self._target_scroll().mount(widget)
                else:
                    widget = AgentStreamBlock(worker)
                    widget.add_class("gap-top")
                    mount_await, _entry = self._tl_append(
                        rebuild, kind="agent", pinned=True, widget=widget
                    )
                    if mount_await is not None:
                        await mount_await  # 确保子组件就绪再 set_body（拆卸中跳过）
                    self._stream_widgets[key] = widget
                    self._last_kind = "other"
            self._pending_stream_updates.add(key)  # 泵尾统一提交一次（防抖）
            self._last_agent_widget = widget
            self._last_agent_key = key
            self._last_agent_worker = worker
            self._maybe_scroll(self._target_scroll())
            self._prev_event_kind = kind
            return
        if kind == "model_request_started":
            model_cfg = self.session.cfg.solver if self.session is not None else None
            if (
                self.session is not None
                and getattr(event, "worker", None) in {"planner", "compact"}
                and self.session.cfg.planner is not None
            ):
                model_cfg = self.session.cfg.planner
            model = model_cfg.model if model_cfg is not None else "模型"
            worker = f"[{event.worker}] " if getattr(event, "worker", None) else ""
            self._set_activity(f"{worker}{model or '模型'} thinking")
            return
        if kind == "agent_reasoning_delta":
            key = self._stream_key(event)
            request_id = event.request_id  # type: ignore[attr-defined]
            block = self._reasoning_blocks.get(key)
            if block is None:
                block = ReasoningBlock(request_id, getattr(event, "worker", None))
                self._reasoning_blocks[key] = block
                self._last_kind = "other"
                worker_ref = getattr(event, "worker", None)
                rid = request_id
                # 重建闭包：流式中从实时 block 取快照；finish 后从 entry.snapshot（终态缓存）恢复
                reasoning_blocks = self._reasoning_blocks

                def rebuild(w=worker_ref, r=rid, k=key, rb=reasoning_blocks):
                    cur = rb.get(k)
                    if cur is not None:
                        snap = cur.snapshot()
                    else:
                        snap = getattr(rebuild, "_final_snap", None) or {"buffer": "", "footer": ""}
                    return ReasoningBlock.restore(r, w, snap)

                self._tl_append(rebuild, kind="reasoning", pinned=True, widget=block)
            block.append(event.delta)  # type: ignore[attr-defined]
            self._maybe_scroll(self._target_scroll())
            return
        if kind == "model_request_complete":
            key = self._stream_key(event)
            self._commit_stream_update(key)  # 不弹出：相邻续写合并需要 raw/widget 还在
            block = self._reasoning_blocks.pop(key, None)
            if block is not None:
                block.finish(
                    event.duration_ms,  # type: ignore[attr-defined]
                    event.completion_tokens,  # type: ignore[attr-defined]
                    ok=event.ok,  # type: ignore[attr-defined]
                )
                # 终态落快照 + 解除 pinned：允许窗口化折叠已完成 reasoning 块
                if self._timeline_windowizer is not None:
                    entry = self._timeline_windowizer.find_entry_by_widget(block)
                    if entry is not None:
                        snap = block.snapshot()
                        entry.snapshot = snap
                        rb = entry.rebuild
                        if rb is not None:
                            rb._final_snap = snap  # type: ignore[attr-defined]
                        self._timeline_windowizer.mark_pinned(entry, False)
                self._maybe_scroll(self._target_scroll())
            if interrupted or (not event.ok and event.error == "cancelled"):  # type: ignore[attr-defined]
                self._set_activity("正在停止")
            else:
                self._set_activity("处理模型响应")
            return
        prefix = f"[{event.worker}] " if getattr(event, "worker", None) else ""
        if kind == "turn_started":
            self._set_activity("准备模型请求")
            await self._write(Text(f"── {prefix}turn {event.turn} ──", style="bold"))  # type: ignore[attr-defined]
        elif kind == "tool_call_begin":
            self._set_activity(f"{event.name} 执行中")  # type: ignore[attr-defined]
            row = ToolCallRow(event.call_id, event.name, event.arguments, getattr(event, "worker", None))  # type: ignore[attr-defined]
            if self._last_kind != "row":
                row.add_class("gap-top")  # 工具组首行留白；组内连续行紧凑
            self._last_kind = "row"
            self._rows[event.call_id] = row  # type: ignore[attr-defined]
            call_id = event.call_id  # type: ignore[attr-defined]
            tool_name = event.name  # type: ignore[attr-defined]
            tool_args = event.arguments  # type: ignore[attr-defined]
            tool_worker = getattr(event, "worker", None)  # type: ignore[attr-defined]
            rows_ref = self._rows
            # 重建闭包：恢复 ToolCallRow 并重放 set_result 终态（从 _rows 拿或从 snapshot）
            def rebuild(cid=call_id, tn=tool_name, ta=tool_args, tw=tool_worker, rr=rows_ref):
                r = ToolCallRow(cid, tn, ta, tw)
                cur = rr.get(cid)
                if cur is not None:
                    # 重放终态：从当前挂载 row 取状态
                    r.ok = cur.ok
                    r.duration_ms = cur.duration_ms
                    r.error = cur.error
                    r.handle = cur.handle
                    r.preview = cur.preview
                    r._diff_cache_key = cur._diff_cache_key
                    r._diff_cache_lines = cur._diff_cache_lines
                else:
                    snap = getattr(rebuild, "_final_snap", None) or {}
                    if snap:
                        r.ok = snap.get("ok")
                        r.duration_ms = snap.get("duration_ms", 0)
                        r.error = snap.get("error")
                        r.handle = snap.get("handle")
                        r.preview = snap.get("preview", "")
                return r

            self._tl_append(rebuild, kind="tool", pinned=True, widget=row)
            self._maybe_scroll(self.query_one("#timeline", VerticalScroll))
        elif kind == "tool_call_end":
            row = self._rows.get(event.call_id)  # type: ignore[attr-defined]
            if row is not None:
                row.set_result(
                    event.ok, event.duration_ms,  # type: ignore[attr-defined]
                    preview=event.output_preview or "",  # type: ignore[attr-defined]
                    error=event.error,  # type: ignore[attr-defined]
                    handle=getattr(event, "handle", None),
                )
                # 工具完成 → 解除 pinned，允许窗口化折叠（流式中才需 pinned 防折叠）
                if self._timeline_windowizer is not None:
                    entry = self._timeline_windowizer.find_entry_by_widget(row)
                    if entry is not None:
                        self._timeline_windowizer.mark_pinned(entry, False)
        elif kind == "flag_verified":
            if event.ok:  # type: ignore[attr-defined]
                await self._write(Text(f"{prefix}FLAG VERIFIED: {event.flag}", style="bold green"))  # type: ignore[attr-defined]
            else:
                await self._write(Text(f"{prefix}flag 被拒: {event.reason}", style="yellow"))  # type: ignore[attr-defined]
        elif kind == "turn_complete":
            if interrupted:
                await self._write(Text(f"{prefix}turn {event.turn} 已停止续跑", style="yellow"))  # type: ignore[attr-defined]
            elif event.stop_reason in ("completed", "truncated", "max_steps") and self.busy:  # type: ignore[attr-defined]
                waiting = bool(self.current_run and self.current_run.background.has_running_detached())
                if waiting:
                    suffix = "等待后台任务"
                elif event.stop_reason == "truncated":  # type: ignore[attr-defined]
                    suffix = "输出触顶，恢复推进"
                elif event.stop_reason == "max_steps":  # type: ignore[attr-defined]
                    suffix = "本轮达到步数上限，继续推进"
                else:
                    suffix = "本轮完成，准备下一轮"
                self._set_activity(suffix)
                await self._write(Text(f"{prefix}turn {event.turn} 完成 · {suffix}", style="dim"))  # type: ignore[attr-defined]
            else:
                await self._write(Text(f"{prefix}turn {event.turn} 结束: {event.stop_reason}", style="dim"))  # type: ignore[attr-defined]
        elif kind == "background_task_complete":
            mark = "✓" if event.ok else "✗"  # type: ignore[attr-defined]
            style = "green" if event.ok else "red"  # type: ignore[attr-defined]
            code = f" exit={event.exit_code}" if event.exit_code is not None else ""  # type: ignore[attr-defined]
            await self._write(Text(f"{prefix}{mark} 后台任务 {event.task_id} 完成{code}", style=style))  # type: ignore[attr-defined]
        elif kind == "swarm_note":
            msg = event.message  # type: ignore[attr-defined]
            if msg.startswith("planner "):
                self._set_activity("等待 planner 重新规划")
            # 旁观者消息用独立配色 + 眼睛图标，与 planner/worker 的 ⚑ 区分开
            low = msg.lower()
            if "spectator" in low or msg.startswith("旁观者") or "旁观者" in msg[:12]:
                await self._write(Text(f"👁 {prefix}{msg}", style="bold bright_magenta"))
            elif msg.startswith("planner ") or "planner" in msg[:12] or "规划" in msg[:8]:
                await self._write(Text(f"⚑ {prefix}{msg}", style="cyan"))
            else:
                await self._write(Text(f"⚑ {prefix}{msg}", style="magenta"))
        elif kind == "error":
            await self._write(Text(f"{prefix}error: {event.message}", style="red"))  # type: ignore[attr-defined]

    @staticmethod
    def _stream_key(event: Event) -> tuple[str | None, str]:
        return getattr(event, "worker", None), getattr(event, "request_id", None) or "legacy"

    def _flush_stream(self, key: tuple[str | None, str] | None = None) -> None:
        """Close one completed model stream, or all streams during session teardown."""
        if key is None:
            self._commit_stream_updates()
            self._stream_widgets.clear()
            self._stream_raw.clear()
            self._pending_stream_updates.clear()
            self._last_agent_widget = None
            self._last_agent_key = None
            self._last_agent_worker = None
            return
        self._commit_stream_update(key)
        self._stream_widgets.pop(key, None)
        self._stream_raw.pop(key, None)
        self._pending_stream_updates.discard(key)

    def _commit_stream_update(self, key: tuple[str | None, str]) -> None:
        widget = self._stream_widgets.get(key)
        raw = self._stream_raw.get(key)
        if widget is not None and raw is not None and key in self._pending_stream_updates:
            widget.set_body(raw)
        self._pending_stream_updates.discard(key)

    def _commit_stream_updates(self) -> None:
        for key in tuple(self._pending_stream_updates):
            self._commit_stream_update(key)

    def _agent_render(self, raw: str, worker: str | None = None) -> Text:
        """agent 文本：● 前缀 + 内联代码橙色 + 粗体（轻量着色，流式安全）。"""
        out = Text()
        if worker:
            import hashlib as _hl
            label = worker.rsplit('-', 1)[0] if '-' in worker else worker
            palette = ['cyan', 'yellow', 'green', 'blue', 'magenta', 'red', 'bright_blue', 'bright_green']
            _h = int(_hl.md5(label.encode()).hexdigest()[:8], 16)
            out.append(f"[{worker}] ", style=palette[_h % len(palette)])
        out.append("● ", style="cyan")
        out.append_text(agent_text(raw))
        return out

    def _set_activity(self, activity: str) -> None:
        """Switch phases without resetting the timer for repeated stream deltas."""
        if activity != self._activity:
            self._activity = activity
            self._activity_started_at = time.monotonic()
            if self.is_mounted:
                self._refresh_activity()

    # ------------------------------------------------------------ spinner 状态行 / 快捷键栏 / jump 提示 / todo 区
    def _refresh_activity(self) -> None:
        """按需显示活动行：上翻提示或解题状态；空闲时不占 composer 高度。"""
        try:
            w = self.query_one("#activity", Static)
            tl = self.query_one("#timeline", VerticalScroll)
        except Exception:
            return
        scrolled_up = tl.max_scroll_y > 0 and not tl.is_vertical_scroll_end
        if scrolled_up:
            w.styles.display = "block"
            w.set_class(True, "jump")
            w.update("Jump to bottom (ctrl+End) ↓", layout=False)
            return
        w.set_class(False, "jump")
        if self.busy and self.current_run is not None:
            run = self.current_run
            elapsed = _fmt_duration(time.monotonic() - getattr(run, "started", time.monotonic()))
            phase_elapsed = _fmt_duration(time.monotonic() - self._activity_started_at)
            state = getattr(getattr(run, "loop", None), "budget_state", None)
            tokens = state.total_tokens if state is not None else 0
            w.styles.display = "block"
            spinner = _ACTIVITY_SPINNER[self._activity_frame]
            w.update(
                f"{spinner} {self._activity or '思考中'} · {phase_elapsed} · 总用时 {elapsed} · ↓ {tokens} tokens",
                layout=False,
            )
        else:
            w.update("", layout=False)
            w.styles.display = "none"

    def _refresh_shortcuts(self) -> None:
        try:
            bar = self.query_one("#shortcuts", Static)
            inp = self.query_one(CommandInput)
        except Exception:
            return
        now = time.monotonic()
        if now < self._terminal_recovery_until:
            try:
                bar.update(Text("已过滤异常终端输入", style="yellow"), layout=False)
            except Exception:
                return
        else:
            if self._setting_page:
                inp.placeholder = "与副驾对话（esc/ctrl+c 返回）"
                text = "esc/ctrl+c 返回主界面 · /setting 继续追问"
                style = "cyan"
            elif self._setup is not None:
                inp.placeholder = "输入配置值（esc 取消）"
                text = "esc 取消配置 · 输入值继续"
                style = "yellow"
            elif self.busy:
                inp.placeholder = "输入补充指令"
                text = "enter 指挥 · ctrl+b 后台 · esc/ctrl+c 中断 · ctrl+t 任务"
                style = "dim"
            else:
                inp.placeholder = "输入题目或指令"
                armed = 0 < now - self._idle_ctrl_c_armed_at <= _CTRL_C_EXIT_WINDOW
                if armed:
                    text = "再次 ctrl+c 退出 · q 立即退出"
                    style = "yellow"
                else:
                    text = "/ 命令 · ctrl+t 任务 · ctrl+c 复制/双击退出 · q 退出"
                    style = "dim"
            try:
                bar.update(Text(text, style=style), layout=False)
            except Exception:
                return

    def _refresh_jumpbar(self) -> None:
        """jump 提示已并入 _refresh_activity（保留空实现兼容旧调用）。"""

    def action_jump_bottom(self) -> None:
        tl = self.query_one("#timeline", VerticalScroll)
        tl.scroll_end(animate=False)
        tl.anchor()  # 重新锚定
        self._refresh_activity()

    def on_click(self, event) -> None:
        """点 Jump to bottom 提示（状态行 jump 相位）回到底部。"""
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "activity" and self.query_one("#activity", Static).has_class("jump"):
            self.action_jump_bottom()
            return
        if widget_id in {"facts", "deadends", "intents"}:
            if not self._board_full:
                self._cmd_board("")
            target = self.query_one(f"#bf-{widget_id}", Vertical)
            self.call_after_refresh(target.scroll_visible, animate=False)

    def action_toggle_todos(self) -> None:
        self._todos_visible = not self._todos_visible
        self.query_one("#todos", Static).styles.display = "block" if self._todos_visible else "none"
        if self._todos_visible:
            self._refresh_todos()

    def _refresh_todos(self) -> None:
        items: list = []
        run = self.current_run or self.last_run
        if run is not None:
            tool = run.registry.get("todo_list")
            items = list(getattr(tool, "items", [])) if tool is not None else []
        try:
            self.query_one("#todos", Static).update(render_todos(items))
        except Exception:
            return

    # ------------------------------------------------------------ 面板刷新
    def _sync_board_sidebar(self) -> None:
        """宽屏 swarm 运行时显示侧栏；空闲和窄屏保持单列对话。"""
        show = self.busy and self.opt_workers > 1 and self.size.width >= 120 and not self._board_full
        try:
            self.query_one("#board").styles.display = "block" if show else "none"
        except Exception:
            return

    def _refresh_board(self) -> None:
        try:
            facts_widget = self.query_one("#facts", Static)
            deadends_widget = self.query_one("#deadends", Static)
            intents_widget = self.query_one("#intents", Static)
        except Exception:
            return
        run = self.current_run or self.last_run
        board = getattr(getattr(run, "ctx", None), "facts", None)
        if board is None:
            signature = f"empty:{self._board_full}"
            if signature == self._board_signature:
                return
            try:
                facts_widget.update(render_facts([], compact=True))
                deadends_widget.update(render_deadends([], compact=True))
                intents_widget.update(render_intents([], compact=True))
                if self._board_full:
                    self._sync_board_section(
                        "#bf-facts",
                        [("h:facts", "title", "已知事实  0"), ("e:facts", "more", "  暂无")],
                    )
                    self._sync_board_section(
                        "#bf-deadends",
                        [("h:deadends", "title", "已排除方向  0"), ("e:deadends", "more", "  暂无")],
                    )
                    self._sync_board_section(
                        "#bf-intents",
                        [("h:intents", "title", "Intents  0"), ("e:intents", "more", "  暂无")],
                    )
            except Exception:
                return
            self._board_signature = signature
            return
        try:
            facts = board.facts_view(100)
            deadends = board.deadends_view(100)
            intents = board.intents(limit=100)
        except Exception:  # noqa: BLE001 - 黑板已关闭（任务刚结束）
            return
        signature = json.dumps(
            [self._board_full, facts, deadends, intents],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if signature == self._board_signature:
            return
        try:
            facts_widget.update(render_facts(facts, limit=8, compact=True))
            deadends_widget.update(render_deadends(deadends, limit=6, compact=True))
            intents_widget.update(render_intents(intents, limit=10, compact=True))
            if self._board_full:
                self._sync_board_section("#bf-facts", self._facts_specs(facts))
                self._sync_board_section("#bf-deadends", self._deadends_specs(deadends))
                self._sync_board_section("#bf-intents", self._intents_specs(intents))
        except Exception:
            return
        self._board_signature = signature

    # ------------------------------------------------------------ 全屏黑板逐条 diff
    def _facts_specs(self, facts: list[dict]) -> list[tuple[str, str, Any]]:
        specs: list[tuple[str, str, Any]] = [("h:facts", "title", f"已知事实  {len(facts)}")]
        for f in facts[-100:]:
            specs.append((f"f:{f['id']}", "fact", f))
        extra = len(facts) - 100
        if extra > 0:
            specs.append(("more:facts", "more", f"… +{extra} more"))
        return specs

    def _deadends_specs(self, deadends: list[dict]) -> list[tuple[str, str, Any]]:
        specs: list[tuple[str, str, Any]] = [("h:deadends", "title", f"已排除方向  {len(deadends)}")]
        for d in deadends[-100:]:
            key = "d:" + hashlib.md5(
                (str(d.get("direction", "")) + "\x00" + str(d.get("reason", ""))).encode()
            ).hexdigest()[:10]
            specs.append((key, "deadend", d))
        extra = len(deadends) - 100
        if extra > 0:
            specs.append(("more:deadends", "more", f"… +{extra} more"))
        return specs

    def _intents_specs(self, intents: list[dict]) -> list[tuple[str, str, Any]]:
        specs: list[tuple[str, str, Any]] = [("h:intents", "title", f"Intents  {len(intents)}")]
        by_status: dict[str, list[dict]] = {k: [] for k in _INTENT_ORDER}
        for i in intents:
            s = i.get("status", "open")
            by_status[s if s in by_status else "open"].append(i)
        shown = 0
        for status in _INTENT_ORDER:
            group = by_status.get(status) or []
            if not group:
                continue
            specs.append((f"g:{status}", "group", f"{_INTENT_ICON[status]} {status}  {len(group)}"))
            for i in group:
                if shown >= 100:
                    break
                specs.append((f"i:{i['id']}", "intent", i))
                shown += 1
        extra = len(intents) - shown
        if extra > 0:
            specs.append(("more:intents", "more", f"… +{extra} more"))
        return specs

    def _build_board_widget(self, kind: str, data: Any):
        if kind == "title":
            return Static(data, classes="board-section-title")
        if kind == "group":
            return Static(data, classes="board-group-title")
        if kind == "more":
            return Static(data, classes="board-more")
        if kind == "fact":
            return BoardItem(*fact_item(data))
        if kind == "deadend":
            return BoardItem(*deadend_item(data))
        if kind == "intent":
            return BoardItem(*intent_item(data))
        return Static(str(data))

    def _update_board_widget(self, w, kind: str, data: Any) -> None:
        if kind in ("title", "group", "more"):
            w.update(data)
        elif kind == "fact":
            w.update_content(*fact_item(data)[:3])
        elif kind == "deadend":
            w.update_content(*deadend_item(data)[:3])
        elif kind == "intent":
            w.update_content(*intent_item(data)[:3])

    def _sync_board_section(self, container_id: str, specs: list[tuple[str, str, Any]]) -> None:
        """按 specs 有序增删改 + 重排全屏黑板节；BoardItem 原地 update 保留展开态。

        每条 spec = (key, kind, data)：key 稳定（fact/intent 用 id，deadend 用内容哈希，
        标题/分组/更多用固定 key）。已有 widget -> update_content 原地刷新（expanded class 不动）；
        新 key -> build + mount(before=idx)；缺失旧 key -> remove。intents 跨组状态变更靠 move_child 重排。
        """
        container = self.query_one(container_id, Vertical)
        by_key: dict[str, Any] = {}
        for w in list(container.children):
            k = getattr(w, "item_key", None)
            if k is not None:
                by_key[k] = w
        seen: set[str] = set()
        for idx, (key, kind, data) in enumerate(specs):
            seen.add(key)
            w = by_key.get(key)
            if w is None:
                w = self._build_board_widget(kind, data)
                w.item_key = key
                container.mount(w, before=idx)
            else:
                self._update_board_widget(w, kind, data)
                children = list(container.children)
                try:
                    cur = children.index(w)
                except ValueError:
                    cur = -1
                if cur != -1 and cur != idx:
                    container.move_child(w, before=idx)
        for key, w in by_key.items():
            if key not in seen:
                w.remove()

    def _refresh_status(self) -> None:
        if self.session is None:
            return
        cfg = self.session.cfg
        line = Text()
        line.append(f"foxdog {_VERSION}", style="bold")
        default_engines = (cfg.solver.model or cfg.solver.provider or "未配置",)
        engines = (
            getattr(self.current_run, "active_engine_labels", default_engines)
            if self.busy and self.current_run is not None
            else default_engines
        )
        line.append("  引擎 ", style="dim")
        for index, engine in enumerate(engines):
            if index:
                line.append(" + ", style="dim")
            line.append(engine, style="bold cyan" if self.busy else "cyan")
        line.append(
            f" · {self.session.permission.mode} · {cfg.runtime.kind} · {self.opt_workers} workers",
            style="dim",
        )
        # 平台监控：主界面显示总题目/已解决数
        stats = self._platform_stats
        if stats.get("total") is not None:
            solved = stats.get("solved") if stats.get("solved") is not None else 0
            line.append(f"  🏁 {solved}/{stats['total']} 已解决", style="green")
        if self.busy and self.current_run is not None:
            run = self.current_run
            elapsed = _fmt_duration(time.monotonic() - run.started)
            state = run.loop.budget_state
            tokens = state.total_tokens if state is not None else 0
            cap = f"{run.budget.max_minutes:g}min" if run.budget.max_minutes is not None else "∞"
            line.append(f"  ● 解题中 {elapsed}/{cap} · {tokens} tokens", style="yellow")
        elif self.busy:
            line.append("  ● 准备任务", style="yellow")
        elif self._setting_busy:
            line.append("  ● 配置中", style="yellow")
        else:
            line.append("  ○ 空闲", style="dim")
        try:
            self.query_one("#status", Static).update(line, layout=False)
        except Exception:
            return

    # ------------------------------------------------------------ 输入与斜杠命令
    def on_input_submitted(self, event) -> None:
        inp = event.input
        raw = event.value
        # 提交前展开长粘贴占位符：用户看不到大段粘贴，agent 拿到全文。
        text = (inp.expand_pastes(raw) if hasattr(inp, "expand_pastes") else raw).strip()
        inp.clear()
        self._sync_input_height()
        if not text:
            return
        # /model 引导式配置流程中：输入是答案而非任务/命令
        if self._setup is not None:
            self._setup_input(text)
            return
        # /setting 独立页中：输入是 copilot 需求（多轮连续对话）
        if self._setting_page and not text.startswith("/"):
            self._submit_setting_requirement(text)
            return
        if text.startswith("/"):
            self._dispatch_command(text)
            return
        self._write_user(text)
        if self.busy:
            self.steer.put(text)
            self._write_line("（已作为 steer 注入队列）", "magenta")
            return
        if self.session is None or not self._provider_ready():
            self._write_line(CONFIG_HINT, "yellow")
            return
        self._solve_task = asyncio.create_task(self._run_task(text))

    def _dispatch_command(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        for sc in self._commands:
            if sc.name == cmd:
                sc.handler(arg.strip())
                return
        self._write_line(f"未知命令 {cmd}，/help 查看命令清单。", "red")

    # ------------------------------------------------------------ 命令实现
    def _cmd_mode(self, arg: str) -> None:
        if arg in ("yolo", "plan") and self.session is not None:
            self.session.permission.set_mode(arg)
            self._refresh_status()
            self._write_line(f"权限模式切换为 {arg}", "magenta")
        else:
            self._write_line("用法: /mode yolo|plan", "red")

    def _cmd_workers(self, arg: str) -> None:
        if arg.isdigit() and int(arg) >= 1:
            self.opt_workers = int(arg)
            self._sync_board_sidebar()
            self._refresh_status()
            self._write_line(f"下一任务 worker 数设为 {self.opt_workers}", "magenta")
        else:
            self._write_line("用法: /workers N（≥1）", "red")

    def _cmd_target(self, arg: str) -> None:
        self.opt_target = arg or None
        self._write_line(f"下一任务靶机：{self.opt_target or '（未设置）'}", "magenta")

    def _cmd_format(self, arg: str) -> None:
        self.opt_format = arg or None
        self._write_line(f"下一任务 flag 格式：{self.opt_format or '（未设置）'}", "magenta")

    def _cmd_new(self, arg: str) -> None:
        # 运行中的任务不允清屏（会撕裂流式块）；空闲才真正清空 timeline + 窗口器
        if self.busy:
            self._write_line("任务运行中，/new 已忽略（完成或 Esc 中断后再开新会话）。", "yellow")
            return
        self.history = []
        self._flush_stream()  # 清流式状态
        self._rows.clear()
        self._reasoning_blocks.clear()
        self._stream_widgets.clear()
        self._stream_raw.clear()
        self._pending_stream_updates.clear()
        self._last_agent_widget = None
        self._last_agent_key = None
        self._last_agent_worker = None
        self._last_kind = None
        if self._timeline_windowizer is not None:
            self._timeline_windowizer.clear()
        self._write_line("已清空对话历史与时间线，新会话开始（会话设置保留）。", "magenta")

    def _cmd_cancel(self, arg: str) -> None:
        if self._setup is not None:
            self._cancel_setup()
        else:
            self._write_line("（没有在进行的配置流程）", "dim")

    def _cmd_board(self, arg: str) -> None:
        """全屏黑板视图切换（Esc 返回）。"""
        self._board_full = not self._board_full
        full = self.query_one("#boardfull")
        workspace = self.query_one("#workspace")
        full.styles.display = "block" if self._board_full else "none"
        workspace.styles.display = "none" if self._board_full else "block"
        self._sync_board_sidebar()
        if self._board_full:
            self._refresh_board()

    # ------------------------------------------------------------ /budget
    def _cmd_budget(self, arg: str) -> None:
        if self.session is None:
            return
        parts = arg.split()
        if not parts:
            self._write_line(f"当前预算：{self.session.cfg.budget.describe()}（None=不限）", "magenta")
            return
        if parts == ["off"]:
            self.apply_budget(off=True)
            self._write_line("预算已全部取消（无限）。", "magenta")
            return
        if len(parts) == 2 and parts[0] in ("minutes", "tokens", "turns"):
            try:
                value: float | int = float(parts[1]) if parts[0] == "minutes" else int(parts[1])
            except ValueError:
                self._write_line("数值非法。", "red")
                return
            self.apply_budget(**{parts[0]: value})
            self._write_line(f"预算已调整：{self.session.cfg.budget.describe()}（运行中立即生效）", "magenta")
            return
        self._write_line("用法: /budget | /budget minutes 30 | tokens 500000 | turns 20 | off", "red")

    def apply_budget(
        self,
        minutes: float | None = None,
        tokens: int | None = None,
        turns: int | None = None,
        off: bool = False,
    ) -> None:
        """改预算：会话配置 + 运行中任务的活 BudgetState（下次 check 即生效）。"""
        if self.session is None:
            return
        cfg_budget = self.session.cfg.budget
        if off:
            cfg_budget.max_minutes = cfg_budget.max_tokens = cfg_budget.max_turns = None
        else:
            if minutes is not None:
                cfg_budget.max_minutes = minutes
            if tokens is not None:
                cfg_budget.max_tokens = tokens
            if turns is not None:
                cfg_budget.max_turns = turns
        run = self.current_run
        if run is None:
            return
        # 活账本：goal 的 state 与 coordinator 的共享 state（Budget 对象就地改）
        targets = [run.budget]
        for st in (run.loop.budget_state, run.coordinator.factory.budget_state if run.coordinator else None):
            if st is not None:
                targets.append(st.budget)
        for b in targets:
            b.max_minutes = cfg_budget.max_minutes
            b.max_tokens = cfg_budget.max_tokens
            b.max_turns = cfg_budget.max_turns

    # ------------------------------------------------------------ /model
    def _masked_model_line(self) -> str:
        solver = self.store.section("model").get("solver", {})
        if not solver and self.session is not None:
            solver = self.session.cfg.solver.model_dump()
        shown = {k: ("***" if k == "api_key" and v else v) for k, v in solver.items() if v not in (None, "")}
        planner = self.store.section("model").get("planner", {})
        line = f"当前 solver：{shown or '（未配置）'}"
        if planner:
            line += f"\nplanner：{ {k: ('***' if k == 'api_key' and v else v) for k, v in planner.items() if v not in (None, '')} }"
        return line

    def _model_show(self) -> str:
        """已配置模型总览：solver/planner/spectator/extra 各端点 + 状态（key 脱敏）。"""
        model = self.store.section("model")
        lines: list[str] = ["已配置模型："]

        def _fmt(role: str, cfg: dict) -> str:
            if not cfg:
                return f"  {role}：（未配置）"
            proto = cfg.get("protocol", "?")
            mdl = cfg.get("model") or "（默认）"
            base = cfg.get("base_url") or "官方端点"
            has_key = "有 key" if cfg.get("api_key") else "无 key"
            return f"  {role}：{proto} · {mdl} · {base} · {has_key}"

        lines.append(_fmt("solver", model.get("solver", {})))
        if model.get("planner"):
            lines.append(_fmt("planner", model["planner"]))
        if model.get("spectator"):
            lines.append(_fmt("spectator（旁观者）", model["spectator"]))
        if model.get("backup"):
            lines.append(_fmt("backup", model["backup"]))
        extra = model.get("extra", {})
        if isinstance(extra, dict):
            for name, cfg in extra.items():
                lines.append(_fmt(f"extra:{name}", cfg if isinstance(cfg, dict) else {}))
        # 运行态：当前会话是否就绪、swarm worker 上限（实际数由 planner 动态收敛）
        if self.session is not None:
            ready = "就绪" if self._provider_ready() else "未就绪"
            lines.append(f"  会话状态：{ready}；swarm worker 上限 {self.opt_workers}（实际并发由 planner 每拍动态决定）")
        return "\n".join(lines)

    def _cmd_model(self, arg: str) -> None:
        if arg == "list":
            self._write_line(self._masked_model_line())
            return
        if arg == "show":
            self._write_line(self._model_show())
            return
        # 进入引导式配置：协议/预设走菜单，key/模型名走输入；Esc 任意步取消
        self._write_line(self._masked_model_line())
        self._setup = {"step": 0, "answers": {}}
        self._setup_models = []  # 拉取到的候选 model id（用于 tab 补全/校验）
        self._write_line("开始配置模型。", "magenta")
        self._show_setup_menu("选择协议", _PROTOCOL_OPTIONS)

    # ------------------------------------------------------------ /model 菜单与取消
    def _show_setup_menu(self, title: str, options: list[tuple[str, str]]) -> None:
        """显示 setup 菜单（OptionList；↑↓ 选择，回车确认，Esc 取消）。"""
        menu = self.query_one("#menu", OptionList)
        menu.clear_options()
        menu.add_options([Option(label, id=value) for value, label in options])
        menu.highlighted = 0
        menu.styles.display = "block"
        self._menu_active = True
        self._write_line(f"{title}（↑↓ 选择，回车确认，Esc 取消）", "magenta")

    def _hide_setup_menu(self) -> None:
        self.query_one("#menu", OptionList).styles.display = "none"
        self._menu_active = False

    def _menu_move(self, delta: int) -> None:
        menu = self.query_one("#menu", OptionList)
        if menu.options:
            menu.highlighted = ((menu.highlighted or 0) + delta) % len(menu.options)

    def _menu_select(self) -> None:
        """回车选中当前高亮项，推进 setup。"""
        menu = self.query_one("#menu", OptionList)
        if menu.highlighted is None or not menu.options:
            return
        value = str(menu.options[menu.highlighted].id)
        self._hide_setup_menu()
        self._advance_setup(value)

    def _cancel_setup(self) -> None:
        """Esc 取消整个 /model 流程（任意步生效）。"""
        if self._setup is not None:
            self._setup = None
        self._hide_setup_menu()
        self.query_one(CommandInput).password = False
        self._write_line("已取消配置流程。", "yellow")

    def _advance_setup(self, value: str) -> None:
        """菜单步推进：protocol → preset → (manual? base_url) → api_key → model。"""
        assert self._setup is not None
        step = _SETUP_STEPS[self._setup["step"]]
        if step == "protocol":
            self._setup["answers"]["protocol"] = value
            self._setup["step"] = 1
            self._show_setup_menu("选择预设", [(v, label) for v, label, _ in _PRESET_OPTIONS])
            return
        if step == "preset":
            if value != "manual":
                preset = next(p[2] for p in _PRESET_OPTIONS if p[0] == value)
                self._setup["answers"].update(preset)  # protocol/base_url/model 预设（protocol 覆盖第一步）
                self._write_line(
                    f"预设：{preset['protocol']} {preset['base_url']}（模型默认 {preset['model']}）", "dim"
                )
                self._setup["step"] = 3  # 跳到 api_key
                self._prompt_api_key()
                return
            self._setup["step"] = 2
            self._write_line("base_url（如 https://api.deepseek.com 或官方端点输 - 跳过）：", "magenta")
            return

    def _prompt_api_key(self) -> None:
        self.query_one(CommandInput).password = True  # key 输入不回显
        self._write_line("api_key（输入不回显）：", "magenta")

    def _setup_input(self, text: str) -> None:
        assert self._setup is not None
        step = _SETUP_STEPS[self._setup["step"]]
        value = text.strip()
        if step == "protocol":
            # 文本兜底：1/2/3、协议名或标签前缀都视作一次菜单选择
            idx_map = {"1": "openai", "2": "responses", "3": "anthropic"}
            choice = idx_map.get(value)
            if choice is None:
                low = value.lower()
                for v, label in _PROTOCOL_OPTIONS:
                    if low == v or low in label.lower():
                        choice = v
                        break
            if choice is None:
                self._write_line("这一步用 ↑↓ 选择（或直接输 1/2/3 或协议名，Esc 取消）。", "yellow")
                return
            self._hide_setup_menu()
            self._advance_setup(choice)
            return
        if step == "preset":
            # 文本兜底：preset id/序号/标签命中 → 选中；否则按全手动 base_url 处理
            low = value.lower()
            for i, (v, label, _p) in enumerate(_PRESET_OPTIONS):
                if low == v or value == str(i) or (low and low in label.lower()):
                    self._hide_setup_menu()
                    self._advance_setup(v)
                    return
            self._hide_setup_menu()
            self._setup["step"] = 2  # manual → 直接当 base_url 值
            self._setup["answers"]["base_url"] = value if value != "-" else ""
            self._write_line(f"> base_url: {self._setup['answers']['base_url'] or '（官方端点）'}", "dim")
            self._setup["step"] = 3
            self._prompt_api_key()
            return
        if step == "base_url":
            if value == "-":
                value = ""
            self._setup["answers"]["base_url"] = value
            self._write_line(f"> base_url: {value or '（官方端点）'}", "dim")
            self._setup["step"] = 3
            self._prompt_api_key()
            return
        if step == "api_key":
            self.query_one(CommandInput).password = False  # 恢复回显
            self._write_line("> api_key: ***", "dim")
            self._setup["answers"]["api_key"] = value
            self._setup["step"] = 4
            # 拉取一次可用模型（无感）：拉到则提示可选值 + tab 补全，拉不到静默跳过
            asyncio.create_task(self._prompt_model_step())
            return
        # model：最后一步
        if value:
            self._setup["answers"]["model"] = value
        chosen = self._setup["answers"].get("model", "")
        self._write_line(f"> model: {chosen}", "dim")
        # 拉取校验（无感）：拉到了非空列表且用户输入不在其中 → 提示但不强制（可能是新模型）
        if self._setup_models and chosen and chosen not in self._setup_models:
            near = [m for m in self._setup_models if chosen.lower() in m.lower()][:5]
            hint = f"，相近：{', '.join(near)}" if near else ""
            self._write_line(
                f"注意：'{chosen}' 不在该端点拉取到的模型列表里{hint}。仍按你输入的配置（如确是新模型可忽略）。",
                "yellow",
            )
        self._ac_hide()
        answers = self._setup["answers"]
        self._setup = None
        self._setup_models = []
        asyncio.create_task(self._finish_model_setup(answers))

    async def _prompt_model_step(self) -> None:
        """api_key 步之后：拉取一次可用模型（无感），拉到则提示 + 开 tab 补全。"""
        if self._setup is None:
            return
        answers = self._setup["answers"]
        default_model = answers.get("model", "")
        hint = f"（回车用预设默认 {default_model}）" if default_model else ""
        # 拉取（失败/不支持静默返回 []）
        self._setup_models = []
        try:
            from .llm.model_catalog import fetch_model_ids
            ep = ModelEndpoint(
                provider="openai-compatible",
                protocol=answers.get("protocol", "openai"),
                base_url=answers.get("base_url") or None,
                api_key=answers.get("api_key") or None,
                model=default_model or "",
            )
            ids = await fetch_model_ids(ep)
            if self._setup is None:  # 期间被 Esc 取消
                return
            self._setup_models = ids
        except Exception:  # noqa: BLE001 - 拉取失败无感，不影响手输
            self._setup_models = []
        if self._setup_models:
            preview = ", ".join(self._setup_models[:8])
            more = " …" if len(self._setup_models) > 8 else ""
            self._write_line(f"该端点可用模型（Tab 补全）：{preview}{more}", "dim")
        self._write_line(f"模型名{hint}：", "magenta")

    async def _finish_model_setup(self, answers: dict[str, str]) -> None:
        ep = ModelEndpoint(
            provider="openai-compatible",
            protocol=answers["protocol"],
            base_url=answers.get("base_url") or None,
            api_key=answers.get("api_key") or None,
            model=answers.get("model") or "",
        )
        self._write_line("测试连接中…", "dim")
        ok, msg = await self.test_endpoint(ep)
        if not ok:
            self._write_line(f"连接测试失败：{msg}。配置未保存，/model 重试（或 /setting 强制写入）。", "red")
            return
        self.store.set_model(
            "solver",
            provider="openai-compatible",
            protocol=ep.protocol,
            base_url=ep.base_url,
            api_key=ep.api_key,
            model=ep.model,
        )
        self.store.save()
        # 热切换会话 provider
        if self.session is not None:
            self.session.cfg.solver = ep
            from .config import build_provider

            self.session.provider = build_provider(self.session.cfg)
        self._write_line(f"✔ 模型已配置并保存：{ep.protocol} {ep.model}（{ep.base_url or '官方端点'}）", "green")

    # ------------------------------------------------------------ /setting（全局 copilot）
    def _cmd_setting(self, arg: str) -> None:
        if self.session is None or not self._provider_ready():
            self._write_line("/setting 依赖模型，请先 /model 配置模型。", "yellow")
            return
        self._open_setting_page()
        if arg:
            self._submit_setting_requirement(arg)
        else:
            self._setting_write("在下方输入框直接说需求（配置/指挥）；Esc 返回主界面。")

    def _submit_setting_requirement(self, text: str) -> None:
        """setting 页里提交一条 copilot 需求（多轮连续对话）。"""
        if self._setting_busy:
            self._setting_write("上一个 /setting 还在执行，稍等。")
            return
        # 隐私：脱敏一次，界面显示 *** 版，发给模型的是 secret://setting/N 句柄版
        safe, shown = self._protect_setting_secrets(text)
        self._setting_write(f"> {shown}", "user-msg")
        self._solve_task_copilot = asyncio.create_task(self._run_setting(safe))

    def _protect_setting_secrets(self, text: str) -> tuple[str, str]:
        """把用户 /setting 输入里的 API key 换成安全句柄再送模型。

        返回 (safe, shown)：
        - safe：给模型看的——每个疑似 key 替换成 secret://setting/N 句柄；配置工具
          （set_model）执行时用同一个金库按句柄 resolve 回真实 key 落盘。真实值永不进
          模型 history。
        - shown：给界面看的——key 替换成 ***（连界面日志也不回显真实 key）。

        识别两类：`api_key:/token=/password ...` 形式的赋值，以及裸的高熵 key（sk-/sk_
        前缀、或 20+ 位无空格 token）。宁可多脱敏几个（模型不需要真值），不可漏。
        """
        import re as _re

        vault = self._setting_secrets
        safe = text
        shown = text

        # 1) key: value / key = value （key、token、secret、password，可带引号）
        assign = _re.compile(
            r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|pwd|bearer)"
            r"(\s*[:=]\s*|\s+)"
            r"(['\"]?)([^\s'\"]{6,})(['\"]?)"
        )

        def _assign_safe(m: _re.Match) -> str:
            handle = vault.put(m.group(4))
            return f"{m.group(1)}{m.group(2)}{m.group(3)}{handle}{m.group(5)}"

        safe = assign.sub(_assign_safe, safe)
        shown = assign.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***{m.group(5)}", shown
        )

        # 2) 裸高熵 key：sk-xxx / sk_xxx / 其它 24+ 位无分隔 token（未被上面覆盖的）。
        #    safe 侧已含 secret://setting/N 句柄，跳过它们避免二次替换。
        bare = _re.compile(r"(?<![A-Za-z0-9/._-])(sk[-_][A-Za-z0-9\-_]{8,}|[A-Za-z0-9\-_]{24,})(?![A-Za-z0-9\-_])")

        def _bare_safe(m: _re.Match) -> str:
            tok = m.group(1)
            return vault.put(tok)

        # safe：只替换尚未句柄化的裸 token；secret://setting/N 里的数字/字母不再命中
        # （其前有 "/"，被 (?<![.../]) 负向后顾挡住）
        safe = bare.sub(_bare_safe, safe)
        shown = bare.sub("***", shown)
        return safe, shown

    async def _run_setting(self, requirement: str) -> None:
        """copilot：配置工具 + 指挥工具（无 shell/文件/任意网络，物理隔离）。

        使用持久 _setting_history 跨多次 /setting 保持连续对话。解题中走 planner
        模型（按协议构造，不再误走 OpenAI Chat）。

        注意：requirement 已由调用方 _submit_setting_requirement 经
        _protect_setting_secrets 脱敏（API key → secret://setting/N 句柄），此处直接入
        history 即可，真实 key 不会进模型消息。
        """
        assert self.session is not None
        self._setting_busy = True
        self._refresh_shortcuts()
        background = BackgroundRegistry()
        try:
            registry = ToolRegistry()
            register_config_tools(registry, self.store, secrets=self._setting_secrets)
            register_copilot_tools(registry, self)
            ctx = ToolContext(
                workspace=None,
                background=background,
                emit=self._queue.put,
                fetch_output=lambda h, o, l: "error: copilot 无归档",
            )
            orchestrator = ToolOrchestrator(registry, self.session.permission, ctx)
            provider = self.session.provider
            if self.busy and self.session.cfg.planner is not None:
                from .config import build_endpoint_provider
                provider = build_endpoint_provider(self.session.cfg.planner)
            loop = AgentLoop(
                provider=provider,
                registry=registry,
                orchestrator=orchestrator,
                emit=self._queue.put,
                max_steps=10,
            )
            # Persistent conversation: seed once, then append user message each call
            if not self._setting_history:
                self._setting_history = [system_message(SETTING_SYSTEM)]
            # requirement 已脱敏（secret://setting/N 句柄）；配置工具执行时用同一金库
            # resolve 回真实值落盘，真实 key 永不进模型 history。
            self._setting_history.append(user_message(requirement))
            await loop.run_turn(self._setting_history, turn_no=len(self._setting_history) // 2 + 1)
            self._flush_stream()
            self._reload_after_setting()
        except Exception as exc:  # noqa: BLE001 - 配置失败不炸会话
            self._setting_write(f"配置执行异常: {type(exc).__name__}: {exc}", "red")
        finally:
            await background.stop_all()
            self._setting_busy = False
            self._refresh_shortcuts()

    def _open_setting_page(self) -> None:
        """Open the /setting overlay: hide workspace, show setting-full；输入区保留。"""
        self._setting_page = True
        self.query_one("#setting-full").styles.display = "block"
        self.query_one("#workspace").styles.display = "none"
        self._refresh_shortcuts()
        self.query_one(CommandInput).focus()

    def _close_setting_page(self) -> None:
        """Close the /setting overlay and return to the main timeline."""
        self._setting_page = False
        self.query_one("#setting-full").styles.display = "none"
        self.query_one("#workspace").styles.display = "block"
        self.query_one("#bottom").styles.display = "block"
        self._refresh_shortcuts()
        self.query_one(CommandInput).focus()  # Esc 返回后落焦输入框

    def _setting_write(self, text: str, classes: str = "") -> None:
        """Write to the setting overlay log instead of the main timeline."""
        import re as _re
        log = self.query_one("#setting-log", Static)
        current = log.render()
        current_plain = current.plain if hasattr(current, "plain") else str(current)
        new_text = current_plain + "\n" + text if current_plain.strip() else text
        has_url = bool(_re.search(r"https?://", text))
        log.update(Text(new_text, no_wrap=has_url))
        sf = self.query_one("#setting-full", VerticalScroll)
        sf.scroll_end(animate=False)

    def _reload_after_setting(self) -> None:
        """/setting 落盘后热加载回会话配置（MCP 变更提示重启生效）。"""
        from .config import AgentConfig, BudgetConfig, McpServerConfig, PlatformConfig, RuntimeConfig

        assert self.session is not None
        cfg = self.session.cfg
        data = self.store.data
        before_solver = dict(cfg.solver.model_dump())
        if data.get("platform"):
            cfg.platform = PlatformConfig(**data["platform"])
        if data.get("agent"):
            cfg.agent = AgentConfig(**{**cfg.agent.model_dump(), **data["agent"]})
            self.session.permission.set_mode(cfg.agent.mode)
        if data.get("budget"):
            cfg.budget = BudgetConfig(**{**cfg.budget.model_dump(), **data["budget"]})
        if data.get("runtime"):
            cfg.runtime = RuntimeConfig(**{**cfg.runtime.model_dump(), **data["runtime"]})
        if "engines" in data:
            from .config import EnginesConfig

            cfg.engines = EnginesConfig(**{**cfg.engines.model_dump(), **data["engines"]})
        if "mcp_servers" in data:
            new_servers = {k: McpServerConfig(**v) for k, v in data["mcp_servers"].items()}
            if new_servers != cfg.mcp_servers:
                cfg.mcp_servers = new_servers
                self._write_line("MCP 配置已落盘，重启会话后生效。", "yellow")
        # Reload model config (legacy solver + new profiles/roles)
        model_data = data.get("model", {})
        solver_store = model_data.get("solver")
        if solver_store:
            merged = {**before_solver, **solver_store}
            if merged != before_solver:
                cfg.solver = ModelEndpoint(**merged)
                from .config import build_provider, build_role_provider

                self.session.provider = build_provider(cfg)
                self._write_line(f"模型已热切换：{cfg.solver.model}", "green")
        # Reload profiles/roles for multi-solver
        if model_data.get("profiles") or model_data.get("roles"):
            from .config import parse_model_config, build_role_provider, build_endpoint_provider

            parsed = parse_model_config(model_data)
            cfg.profiles = parsed["profiles"]
            cfg.roles = parsed["roles"]
            cfg.solver_endpoints = parsed["solver_endpoints"]
            cfg.planner_endpoints = parsed["planner_endpoints"]
            if cfg.solver_endpoints:
                first_ep = next(iter(cfg.solver_endpoints.values()))
                cfg.solver = first_ep
                self.session.provider = build_role_provider(cfg.solver_endpoints, first_ep)
            if cfg.planner_endpoints:
                cfg.planner = next(iter(cfg.planner_endpoints.values()))
        self._refresh_status()

    # ------------------------------------------------------------ /fetch
    def _cmd_fetch(self, arg: str) -> None:
        if self.session is None:
            self._write_line(CONFIG_HINT, "yellow")
            return
        asyncio.create_task(self._fetch_task(arg))

    async def _fetch_task(self, arg: str) -> None:
        assert self.session is not None
        platform = self.session.cfg.platform
        # 配置了平台 fetch_url：走 platform.fetch_challenge（解析 JSON 正文 + 下载附件）
        if platform.fetch_url and arg:
            from .platform import fetch_challenge
            dl_dir = (self._workspace or Path.cwd()) / ".foxdog-runs" / "inputs" / re.sub(r"[^A-Za-z0-9._-]", "_", arg)
            try:
                text, paths = await fetch_challenge(platform, arg, download_dir=dl_dir)
            except Exception as exc:  # noqa: BLE001 - 拉取失败只提示
                self._write_line(f"平台拉取失败: {type(exc).__name__}: {exc}", "red")
                return
            content = " ".join(str(text).split())[:4000]
            self.query_one(CommandInput).value = content
            if paths:
                names = ", ".join(p.name for p in paths)
                self._write_line(f"已下载 {len(paths)} 个附件到 {dl_dir}：{names}", "green")
            self._write_line(f"题目已拉取到输入框（id={arg}），确认后回车开任务。", "magenta")
            return
        # 未配置平台或直接给 URL：抓正文塞输入框
        if platform.fetch_url:
            url = platform.fetch_url.replace("{id}", arg) if arg else platform.fetch_url
        elif arg:
            url = arg
        else:
            self._write_line("用法: /fetch <题目id|URL>（未配置 platform.fetch_url 时参数当 URL）", "red")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True) as client:
                resp = await client.get(url)
            content = " ".join(resp.text.split())[:4000]
        except Exception as exc:  # noqa: BLE001 - 拉取失败只提示
            self._write_line(f"拉取失败: {type(exc).__name__}: {exc}", "red")
            return
        if not content:
            self._write_line(f"拉取为空（HTTP {resp.status_code}）：{url}", "yellow")
            return
        self.query_one(CommandInput).value = content
        self._write_line(f"题目已拉取到输入框（{url}），确认后回车开任务。", "magenta")

    # ------------------------------------------------------------ /runs
    def _show_runs(self) -> None:
        summaries = list_run_summaries(self._workspace, limit=15)
        if not summaries:
            self._write_line("（还没有任何 run）", "dim")
            return
        rows = [f"{item.run_id}  {item.objective[:30]}  {item.flag or '—'}" for item in summaries]
        self._write_line("历史 run（最近 15 个，最新在下；/resume 可无缝续跑）：\n" + "\n".join(rows))

    @staticmethod
    def _resume_run_label(item: RunSummary) -> str:
        objective = item.objective[:44] or "（未记录目标）"
        tag = f"✔ {item.flag[:20]}" if item.solved and item.flag else ("✔ solved" if item.solved else "…")
        when = time.strftime("%m-%d %H:%M", time.localtime(item.ts)) if item.ts else ""
        return f"{item.run_id}  {when}  {tag}  {objective}"

    def _show_resume_picker(self) -> None:
        """/resume 无参数：↑↓ 选择历史 run（codex/kimi-code 风格），回车无缝续跑。"""
        if self.busy:
            self._write_line("当前有任务在跑，先等它结束或 Esc 中断再 /resume。", "yellow")
            return
        if self.session is None or not self._provider_ready():
            self._write_line("/resume 依赖模型，请先 /model 配置模型。", "yellow")
            return
        summaries = list_run_summaries(self._workspace)
        if not summaries:
            self._write_line("没有任何 run 可续跑。", "yellow")
            return
        # list_run_summaries 按时间从旧到新；最新的排最上面，最多展示 20 个
        ordered = list(reversed(summaries))[:20]
        self._resume_candidates = ordered
        menu = self.query_one("#resume-menu", OptionList)
        menu.clear_options()
        menu.add_options([Option(self._resume_run_label(item), id=item.run_id) for item in ordered])
        menu.highlighted = 0
        menu.styles.display = "block"
        self._resume_picker_active = True
        self._write_line(f"续跑哪个任务？（↑↓ 选择，回车续跑，Esc 取消 · 共 {len(summaries)} 个）", "magenta")

    def _hide_resume_picker(self, *, cancelled: bool) -> None:
        self.query_one("#resume-menu", OptionList).styles.display = "none"
        self._resume_picker_active = False
        self._resume_candidates = []
        if cancelled:
            self._write_line("已取消续跑。", "yellow")

    def _resume_picker_move(self, delta: int) -> None:
        menu = self.query_one("#resume-menu", OptionList)
        if menu.options:
            menu.highlighted = ((menu.highlighted or 0) + delta) % len(menu.options)

    def _resume_picker_select(self) -> None:
        menu = self.query_one("#resume-menu", OptionList)
        if menu.highlighted is None or not menu.options:
            return
        run_id = str(menu.options[menu.highlighted].id)
        self._hide_resume_picker(cancelled=False)
        self._resume_run(run_id)

    def _cmd_resume(self, arg: str) -> None:
        """/resume [run_id]：无缝续跑上一任务。省略 run_id 弹 ↑↓ 选择菜单。"""
        if not arg.strip():
            self._show_resume_picker()
            return
        self._resume_run(arg.strip())

    def _resume_run(self, run_id: str) -> None:
        """重放指定 run 的历史（assistant/tool_result）后无缝继续解题。"""
        if self.busy:
            self._write_line("当前有任务在跑，先等它结束或 Esc 中断再 /resume。", "yellow")
            return
        if self.session is None or not self._provider_ready():
            self._write_line("/resume 依赖模型，请先 /model 配置模型。", "yellow")
            return
        item = find_run_summary(self._workspace, run_id)
        if item is None:
            self._write_line(f"找不到可续跑的 run：{run_id}。/runs 查看。", "red")
            return
        history = replay_history(item.path / "rollout.jsonl")
        if not item.objective:
            self._write_line(f"{item.run_id} 未记录目标，无法续跑。", "red")
            return
        # 注入重放历史 + 恢复靶机/flag 格式，然后像正常任务一样继续
        self.history = list(history)
        if item.target:
            self.opt_target = item.target
        if item.flag_format:
            self.opt_format = item.flag_format
        self._resume_from = item.path
        self._write_user(f"[续跑 {item.run_id}] {item.objective}")
        rendered = self._render_replayed_history(item)
        self._write_line(
            f"已重放 {len(history)} 条上下文历史，恢复工作区并继续解题（时间线显示 {rendered} 条主线记录）……",
            "cyan",
        )
        self._solve_task = asyncio.create_task(self._run_task(item.objective))

    # ------------------------------------------------------------ 自动补全
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        inp = getattr(event, "text_area", None)
        if not isinstance(inp, CommandInput):
            return
        self._sync_input_height()
        value, cursor = _strip_leaked_mouse_reports(inp.value, inp.cursor_position)
        if value != inp.value:
            inp.value = value
            inp.cursor_position = cursor
            self._terminal_recovery_until = time.monotonic() + 1.5
            self._refresh_shortcuts()
            return
        if value.startswith("/") and " " not in value and "\n" not in value:
            self._ac_show(value)
        elif self._in_model_name_step() and self._setup_models:
            self._ac_show_models(value)
        else:
            self._ac_hide()

    def _in_model_name_step(self) -> bool:
        return bool(self._setup is not None and _SETUP_STEPS[self._setup["step"]] == "model")

    def _ac_show_models(self, prefix: str) -> None:
        """/model 模型名步：从拉取到的候选里补全（拉不到则无补全）。"""
        p = prefix.strip().lower()
        matches = [m for m in self._setup_models if p in m.lower()] if p else list(self._setup_models)
        matches = matches[:12]
        if not matches:
            self._ac_hide()
            return
        ac = self.query_one("#ac", OptionList)
        ac.clear_options()
        ac.add_options([Option(m, id=m) for m in matches])
        ac.highlighted = 0
        ac.styles.display = "block"
        self._ac_visible = True

    def _sync_input_height(self) -> None:
        """输入框高度随行数自适应：每多一行 +1，上限 10 行（超出内部滚动）。

        用 wrapped_document.height（soft_wrap 后的视觉行数），超长折行单行
        也会撑高；CSS max-height:10 兜底，行内 styles.height 优先级更高。
        """
        inp = self.query_one(CommandInput)
        rows = inp.wrapped_document.height or 1
        inp.styles.height = max(1, min(10, rows))

    def _ac_show(self, prefix: str) -> None:
        matches = [sc for sc in self._commands if sc.name.startswith(prefix)]
        if not matches:
            self._ac_hide()
            return
        ac = self.query_one("#ac", OptionList)
        ac.clear_options()
        ac.add_options([Option(f"{sc.name}  {sc.desc}", id=sc.name) for sc in matches])
        ac.highlighted = 0
        ac.styles.display = "block"
        self._ac_visible = True

    def _ac_hide(self) -> None:
        self.query_one("#ac", OptionList).styles.display = "none"
        self._ac_visible = False

    def _ac_move(self, delta: int) -> None:
        ac = self.query_one("#ac", OptionList)
        count = len(ac.options)
        if count:
            ac.highlighted = ((ac.highlighted or 0) + delta) % count

    def _ac_accept(self) -> None:
        ac = self.query_one("#ac", OptionList)
        if ac.highlighted is None or not ac.options:
            return
        name = ac.options[ac.highlighted].id
        inp = self.query_one(CommandInput)
        inp.value = f"{name} "
        inp.cursor_position = len(inp.value)
        self._ac_hide()
        inp.focus()  # 补全选择后落焦输入框

    def _ac_names(self) -> list[str]:
        """测试用：当前补全候选。"""
        ac = self.query_one("#ac", OptionList)
        return [str(o.id) for o in ac.options] if self._ac_visible else []

    # ------------------------------------------------------------ Ctrl+C 上下文语义
    def copy_to_clipboard(self, text: str) -> None:
        """Mirror Textual's OSC52 copy to the local clipboard when available."""
        super().copy_to_clipboard(text)
        try:
            import pyperclip

            pyperclip.copy(text)
        except Exception:  # noqa: BLE001 - OSC52 remains available as the fallback
            pass

    def action_copy_selection(self) -> bool:
        """Copy a tracked selection and report whether anything was copied."""
        selected = self.screen.get_selected_text() or ""
        if not selected:
            focused = self.screen.focused
            if isinstance(focused, CommandInput):
                selected = focused.selected_text
        if selected:
            self.copy_to_clipboard(selected)
            return True
        return False

    def action_handle_ctrl_c(self) -> None:
        """Copy first; otherwise interrupt an active solve or cleanly exit when idle."""
        if self.action_copy_selection():
            return
        if self._board_full or self._setting_page or self._setup is not None:
            self._idle_ctrl_c_armed_at = 0.0
            self.action_interrupt_task()
            return
        if self.busy or self._setting_busy:
            self._idle_ctrl_c_armed_at = 0.0
            self.action_interrupt_task()
            return
        now = time.monotonic()
        elapsed = now - self._idle_ctrl_c_armed_at
        if _CTRL_C_DUPLICATE_WINDOW <= elapsed <= _CTRL_C_EXIT_WINDOW:
            self._idle_ctrl_c_armed_at = 0.0
            asyncio.create_task(self.action_quit())
            return
        if 0 < elapsed < _CTRL_C_DUPLICATE_WINDOW:
            return  # 同一次物理按键被终端重复上报，不算第二次确认
        self._idle_ctrl_c_armed_at = now
        self._refresh_shortcuts()

    # ------------------------------------------------------------ 中断与退出
    def action_interrupt_task(self) -> None:
        """Esc：/board 全屏或 /setting 页时返回；取消 /model 配置；否则中断当前任务。"""
        if self._board_full:
            self._cmd_board("")
            return
        if self._setting_page:
            self._close_setting_page()
            return
        if self._setup is not None:
            self._cancel_setup()
            self._refresh_shortcuts()
            return
        if self.busy and self.current_run is not None:
            run = self.current_run
            if run.interrupted:
                return
            run.interrupted = True
            run.loop.interrupt.set()
            if run.coordinator is not None:
                run.coordinator.interrupt.set()
            self._pending_stream_updates.clear()
            for row in self._rows.values():
                if row.ok is None:
                    if row.is_mounted:
                        row.set_result(False, 0, error="已中断")
                    else:
                        # Ctrl+C can race the await that mounts a just-started row.
                        row.ok = False
                        row.error = "已中断"
            self._set_activity("正在停止")
            self._write_line("正在停止当前任务。", "yellow")
            # The event is cooperative and is only polled between awaits. Cancel
            # the root solve task as well so request upload, SSE thinking,
            # compaction, planner calls and tool waits stop immediately.
            task = self._solve_task
            if task is not None and not task.done():
                task.cancel()
            return
        if self.busy:
            task = self._solve_task
            if task is not None and not task.done():
                self._set_activity("正在停止")
                self._write_line("正在停止当前任务。", "yellow")
                task.cancel()
            return
        if self._setting_busy:
            task = getattr(self, "_solve_task_copilot", None)
            if task is not None and not task.done():
                task.cancel()
                self._write_line("已请求中断 /setting。", "yellow")

    def action_focus_input(self) -> None:
        self.query_one(CommandInput).focus()

    def on_app_focus(self, _event: events.AppFocus) -> None:
        """窗口从失焦切回时自动落焦输入框：任何时候切回窗口直接打字。"""
        self.query_one(CommandInput).focus()

    def action_background_command(self) -> None:
        """Ctrl+B detaches the newest foreground shell wait without stopping its process."""
        if not self.busy or self.current_run is None:
            self.notify("当前没有运行中的解题任务")
            return
        task = self.current_run.background.detach_latest()
        if task is None:
            self.notify("当前没有等待中的 shell 命令")
            return
        self._set_activity(f"{task.task_id} 已转后台")
        self._write_line(f"↳ {task.task_id} 已转后台：{task.command}", "yellow")
        self._refresh_shortcuts()

    async def action_quit(self) -> None:
        """退出：解题中先取消（execute_run finally 清理），再关会话级资源。"""
        if self._solve_task is not None and not self._solve_task.done():
            self._solve_task.cancel()
            try:
                await asyncio.wait_for(self._solve_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        copilot_task = getattr(self, "_solve_task_copilot", None)
        if copilot_task is not None and not copilot_task.done():
            copilot_task.cancel()
            try:
                await asyncio.wait_for(copilot_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if self.session is not None and self.session.mcp_manager is not None:
            await self.session.mcp_manager.close()
        self.exit()

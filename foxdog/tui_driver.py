"""Terminal driver adjustments shared by both Textual frontends.

Windows Terminal sends keys and mouse reports as VT sequences. Textual's default
100 ms escape timeout can split a delayed arrow into ``Esc`` + ``[C`` under load,
and its 1003 mouse mode reports every pointer move. FoxDog keeps drag selection
but reduces input pressure with 1002 button-event tracking and a wider sequence
assembly window, matching the native-event behavior used by kimi-code more closely.
"""
from __future__ import annotations

import signal
import sys
import time
from typing import Any

from textual import constants, events
from textual.driver import Driver

WINDOWS_ESCAPE_DELAY = 0.35
# 粘贴去重窗口：conhost 把一次粘贴同时投递为 Paste 事件 + 逐字符 Key 事件，
# 收到 Paste 后此窗口内、与粘贴内容逐字符匹配的 Key 一律丢弃（防 a→aa）。
_PASTE_DEDUP_WINDOW = 0.30
TERMINAL_TITLE_SPINNER = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"


def _sanitize_terminal_title(title: str) -> str:
    """Strip control bytes before sending an OSC title update."""
    return "".join(ch for ch in str(title) if ord(ch) >= 32 and ord(ch) != 127)


def write_terminal_title(app: Any, title: str) -> None:
    """Set the terminal tab/window title when the active driver supports it."""
    clean = _sanitize_terminal_title(title) or "foxdog"
    try:
        app.title = clean
    except Exception:
        pass
    driver = getattr(app, "_driver", None)
    if driver is None:
        return
    try:
        driver.write(f"\x1b]0;{clean}\x07")
        flush = getattr(driver, "flush", None)
        if callable(flush):
            flush()
    except Exception:
        return


def _paste_key_character(message: events.Key) -> str | None:
    """Normalize terminal key events to the characters used by Paste text."""
    character = message.character
    if character is not None:
        return character
    key = getattr(message, "key", "")
    if key in {"enter", "return"}:
        return "\r"
    if key == "tab":
        return "\t"
    if key == "space":
        return " "
    if isinstance(key, str) and len(key) == 1:
        return key
    return None


class _PasteDedup:
    """conhost 粘贴双投递去重的纯状态机（平台无关，便于测试）。

    收到 Paste 记下文本与截止时刻；随后每个 Key 字符若按序匹配剩余粘贴内容，
    判为"粘贴内容被当按键重放"→ 丢弃；一旦不匹配或超时，清状态放行真实按键。
    """

    def __init__(self, window: float = _PASTE_DEDUP_WINDOW) -> None:
        self._window = window
        self._pending = ""
        self._deadline = 0.0
        self._last_paste = ""
        self._last_paste_deadline = 0.0
        self._skip_optional_lf = False

    def note_paste(self, text: str, now: float) -> None:
        value = text or ""
        self._pending = value
        self._deadline = now + self._window
        self._last_paste = value.replace("\r\n", "\n").replace("\r", "\n")
        self._last_paste_deadline = now + self._window

    def should_drop_paste(self, text: str | None, now: float) -> bool:
        """Drop a duplicate Paste event emitted for the same physical paste."""
        value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if value and value == self._last_paste and now <= self._last_paste_deadline:
            return True
        if now > self._last_paste_deadline:
            self._last_paste = ""
            self._last_paste_deadline = 0.0
        return False

    def should_drop_key(self, character: str | None, now: float) -> bool:
        if not self._pending or now > self._deadline:
            self._pending = ""
            self._skip_optional_lf = False
            return False
        if character is None:
            return False
        if self._skip_optional_lf:
            self._skip_optional_lf = False
            if character == "\n":
                return True
        if character in {"\r", "\n"}:
            if self._pending.startswith("\r\n"):
                self._pending = self._pending[2:]
                self._skip_optional_lf = character == "\r"
            elif self._pending.startswith(character):
                self._pending = self._pending[1:]
                self._skip_optional_lf = character == "\r"
            elif character == "\r" and self._pending.startswith("\n"):
                self._pending = self._pending[1:]
                self._skip_optional_lf = True
            else:
                self._pending = ""
                self._deadline = 0.0
                self._skip_optional_lf = False
                return False
            if not self._pending:
                self._deadline = 0.0
            return True
        if character and self._pending.startswith(character):
            self._pending = self._pending[len(character):]
            if not self._pending:
                self._deadline = 0.0
            return True
        # 不匹配：重放结束或真实按键，清状态放行
        self._pending = ""
        self._deadline = 0.0
        self._skip_optional_lf = False
        return False


if sys.platform == "win32":
    from textual.drivers.windows_driver import WindowsDriver

    class FoxdogWindowsDriver(WindowsDriver):
        """Windows driver with drag-only mouse tracking and paste dedup."""

        def _enable_mouse_support(self) -> None:
            if not self._mouse:
                return
            # Clear modes a previously interrupted TUI may have left behind.
            self.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1015l\x1b[?1006l")
            # 1002 reports clicks and button-held motion (selection), not hover motion.
            self.write("\x1b[?1002h\x1b[?1006h")
            self.flush()

        def _disable_mouse_support(self) -> None:
            if not self._mouse:
                return
            self.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1015l\x1b[?1006l")
            self.flush()

        @property
        def _paste_dedup(self) -> _PasteDedup:
            # 懒建：测试用 object.__new__ 绕过 __init__，这里兜底初始化
            dd = self.__dict__.get("_paste_dedup_state")
            if dd is None:
                dd = _PasteDedup()
                self.__dict__["_paste_dedup_state"] = dd
            return dd

        def process_message(self, message: Any) -> None:
            """去重 conhost 粘贴的 Paste + 重复 Key 双投递（Windows Terminal a→aa 根因）。"""
            now = time.monotonic()
            if isinstance(message, events.Paste):
                if self._paste_dedup.should_drop_paste(message.text, now):
                    return
                self._paste_dedup.note_paste(message.text or "", now)
                super().process_message(message)
                return
            if isinstance(message, events.Key):
                character = _paste_key_character(message)
                if self._paste_dedup.should_drop_key(character, now):
                    return
            super().process_message(message)

else:
    FoxdogWindowsDriver = None  # type: ignore[misc, assignment]


def get_tui_driver_class() -> type[Driver] | None:
    """Return the platform driver and configure robust Windows CSI assembly."""
    if sys.platform != "win32":
        return None
    constants.ESCAPE_DELAY = max(constants.ESCAPE_DELAY, WINDOWS_ESCAPE_DELAY)
    return FoxdogWindowsDriver


def run_tui_app(app: Any) -> Any:
    """Run a Textual app and route Windows SIGINT back to its Ctrl+C action.

    Windows normally delivers Ctrl+C as a VT key once Textual enters application
    mode. Some console hosts emit SIGINT instead, which would cancel asyncio before
    the binding runs. A custom handler forwards that alternate path to the same app
    action. Do not use SIG_IGN here: on Windows it may swallow the key entirely.
    """
    if sys.platform != "win32":
        return app.run()

    def forward_ctrl_c(_signum: int, _frame: Any) -> None:
        loop = getattr(app, "_loop", None)
        action = getattr(app, "action_handle_ctrl_c", None)
        if (
            getattr(app, "is_running", False)
            and loop is not None
            and loop.is_running()
            and callable(action)
        ):
            loop.call_soon_threadsafe(action)

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, forward_ctrl_c)
    try:
        return app.run()
    finally:
        signal.signal(signal.SIGINT, previous)

"""Windows terminal protocol hardening: CSI assembly and mouse tracking modes."""
from __future__ import annotations

import sys
import signal
import time

import pytest
from textual import constants
from textual._xterm_parser import XTermParser

from foxdog.tui_driver import (
    FoxdogWindowsDriver,
    WINDOWS_ESCAPE_DELAY,
    _PasteDedup,
    get_tui_driver_class,
    run_tui_app,
)
from foxdog.interactive import _strip_leaked_mouse_reports


def test_paste_dedup_drops_replayed_keys_after_paste():
    """conhost 把粘贴同时投递为 Paste + 逐字符 Key → 匹配的 Key 丢弃（a→aa 修复）。"""
    dd = _PasteDedup(window=0.30)
    # 粘贴 "abc"，随后 3 个重放按键应全部被吞
    dd.note_paste("abc", now=100.0)
    assert dd.should_drop_key("a", now=100.01) is True
    assert dd.should_drop_key("b", now=100.02) is True
    assert dd.should_drop_key("c", now=100.03) is True
    # 队列已空：后续真实按键放行
    assert dd.should_drop_key("d", now=100.04) is False


def test_paste_dedup_passes_unmatched_and_expired_keys():
    dd = _PasteDedup(window=0.30)
    # 无粘贴：普通按键放行
    assert dd.should_drop_key("x", now=1.0) is False
    # 粘贴后但字符不匹配（用户抢先手打）→ 放行且清状态
    dd.note_paste("abc", now=2.0)
    assert dd.should_drop_key("z", now=2.01) is False
    assert dd.should_drop_key("a", now=2.02) is False  # 状态已清，不再吞
    # 超时窗口外的重放不吞
    dd.note_paste("hi", now=5.0)
    assert dd.should_drop_key("h", now=5.5) is False  # 超过 0.30s 窗口


def test_paste_dedup_single_char_double():
    """最常见现象：粘贴单字符 'a' → 一个重放 Key 'a' 被吞，避免 aa。"""
    dd = _PasteDedup()
    dd.note_paste("a", now=10.0)
    assert dd.should_drop_key("a", now=10.05) is True
    assert dd.should_drop_key("a", now=10.10) is False  # 只吞一个


def test_paste_dedup_normalizes_replayed_enter():
    """多行粘贴的重复 Enter 不能穿透为提交，CR/LF 应视为同一个换行。"""
    dd = _PasteDedup(window=0.30)
    dd.note_paste("a\r\nb", now=20.0)
    assert dd.should_drop_key("a", now=20.01) is True
    assert dd.should_drop_key("\r", now=20.02) is True
    assert dd.should_drop_key("\n", now=20.03) is True
    assert dd.should_drop_key("b", now=20.04) is True
    assert dd.should_drop_key("c", now=20.05) is False


def test_paste_dedup_drops_duplicate_paste_event():
    """有些 Windows 终端会把同一次粘贴发成两个 Paste 事件。"""
    dd = _PasteDedup(window=0.30)
    assert dd.should_drop_paste("abc", now=1.0) is False
    dd.note_paste("abc", now=1.0)
    assert dd.should_drop_paste("abc", now=1.05) is True
    assert dd.should_drop_paste("xyz", now=1.06) is False
    assert dd.should_drop_paste("abc", now=1.5) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows driver only")
def test_windows_driver_uses_drag_only_mouse_tracking():
    driver = object.__new__(FoxdogWindowsDriver)
    driver._mouse = True
    writes: list[str] = []
    driver.write = writes.append
    driver.flush = lambda: None

    driver._enable_mouse_support()
    enabled = "".join(writes)
    assert "\x1b[?1002h" in enabled
    assert "\x1b[?1003h" not in enabled
    assert "\x1b[?1003l" in enabled  # clears stale any-event mode first

    writes.clear()
    driver._disable_mouse_support()
    disabled = "".join(writes)
    assert "\x1b[?1002l" in disabled and "\x1b[?1003l" in disabled


@pytest.mark.skipif(sys.platform != "win32", reason="Windows VT input only")
def test_windows_escape_window_keeps_split_arrow_atomic():
    assert get_tui_driver_class() is FoxdogWindowsDriver
    assert constants.ESCAPE_DELAY >= WINDOWS_ESCAPE_DELAY

    parser = XTermParser()
    assert list(parser.feed("\x1b")) == []
    time.sleep(0.12)  # longer than Textual's old 100 ms default
    assert list(parser.tick()) == []
    events = list(parser.feed("[C"))
    assert len(events) == 1 and getattr(events[0], "key", None) == "right"


def test_mouse_report_filter_is_precise_and_cursor_aware():
    dirty = "pre[<32;111;23Mmid\x1b[<0;4;2mpost"
    clean, cursor = _strip_leaked_mouse_reports(dirty, len(dirty))
    assert clean == "premidpost"
    assert cursor == len(clean)

    payload = "literal[C and [not-a-mouse-report"
    assert _strip_leaked_mouse_reports(payload, 7) == (payload, 7)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Ctrl+C guard only")
def test_run_tui_app_scopes_sigint_guard(monkeypatch):
    previous = object()
    changes: list[tuple[int, object]] = []

    monkeypatch.setattr(signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: changes.append((signum, handler)))

    class StubLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback):
            callback()

    class StubApp:
        is_running = True
        _loop = StubLoop()
        ctrl_c_calls = 0

        def action_handle_ctrl_c(self):
            self.ctrl_c_calls += 1

        def run(self):
            handler = changes[-1][1]
            assert callable(handler) and handler is not signal.SIG_IGN
            handler(signal.SIGINT, None)
            assert self.ctrl_c_calls == 1
            return 7

    assert run_tui_app(StubApp()) == 7
    assert len(changes) == 2
    assert changes[0][0] == signal.SIGINT and callable(changes[0][1])
    assert changes[1] == (signal.SIGINT, previous)

"""输入框四项修复：多行输入、自动变高、长粘贴折叠、自动聚焦。"""
from __future__ import annotations

from textual import events

from foxdog.interactive import ChatApp, CommandInput


def _submit(app: ChatApp, text: str) -> None:
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    return ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")


# ------------------------------------------------------------ 多行输入
async def test_enter_submits_and_preserves_newlines(tmp_path, monkeypatch):
    """Enter = 提交；多行内容连换行一起进提交流程（steer 路径观察）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        app.busy = True  # 走 steer 路径，不起任务
        inp.value = "第一行\n第二行"
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.steer.drain() == ["第一行\n第二行"]
        assert inp.value == ""  # 提交后清空
        app.busy = False
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_shift_enter_and_ctrl_j_insert_newline(tmp_path, monkeypatch):
    """Shift+Enter / Ctrl+J = 换行（不提交）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        inp.focus()
        await pilot.press("a", "ctrl+j", "b", "shift+enter", "c")
        await pilot.pause(0.2)
        assert inp.value == "a\nb\nc"
        assert app.steer.drain() == []  # 没有触发提交
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_multiline_render_keeps_bar_cursor_on_cursor_line(tmp_path, monkeypatch):
    """多行时 ▏ 只画在光标所在行。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        inp.value = "ab\ncd"
        inp.move_cursor((1, 1))
        inp.focus()
        await pilot.pause(0.3)
        line0 = "".join(seg.text for seg in inp.render_line(0))
        line1 = "".join(seg.text for seg in inp.render_line(1))
        assert "▏" not in line0
        assert "▏" in line1 and line1.index("▏") == 1  # c▏d
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 自动变高
async def test_height_grows_with_lines_and_caps_at_10(tmp_path, monkeypatch):
    """每多一行 +1，上限 10；soft_wrap 开启（无横向滚动条）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        assert inp.soft_wrap is True
        assert float(inp.styles.height.value) == 1  # 初始单行
        inp.value = "\n".join(f"line{i}" for i in range(3))
        await pilot.pause(0.3)
        assert float(inp.styles.height.value) == 3
        inp.value = "\n".join(f"line{i}" for i in range(12))
        await pilot.pause(0.3)
        assert float(inp.styles.height.value) == 10  # 封顶
        inp.clear()
        await pilot.pause(0.3)
        assert float(inp.styles.height.value) == 1  # 清空回 1
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_long_soft_wrapped_line_also_grows(tmp_path, monkeypatch):
    """超长单行 soft_wrap 折成多个视觉行时同样撑高（不横向滚动）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        inp.value = "x" * 200  # 60 列窗口下折成多行
        await pilot.pause(0.3)
        assert float(inp.styles.height.value) > 1
        assert inp.scroll_offset.x == 0
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 长粘贴折叠
async def test_long_paste_folds_to_token_and_expands_on_submit(tmp_path, monkeypatch):
    """>200 字符粘贴：输入框只见占位符，提交时展开还原全文。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        payload = "证明 " + "x" * 300
        await inp._on_paste(events.Paste(payload))
        await pilot.pause(0.2)
        assert inp.value == "[粘贴的文本#1]"
        assert inp._pastes[1] == payload
        # 占位符渲染成青色加粗（▏ 覆盖把 strip 拆成单字符 segment，按字符查样式）
        strip = inp.render_line(0)
        text = "".join(seg.text for seg in strip)
        assert "[粘贴的文本#1]" in text
        styles = {seg.text: seg.style for seg in strip if seg.text}
        token_style = styles["粘"]
        assert token_style and token_style.bold
        assert token_style.color and token_style.color.name == "cyan"
        # 提交展开还原
        app.busy = True
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.steer.drain() == [payload]
        assert inp._pastes == {}  # clear 时丢弃
        app.busy = False
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_short_multiline_paste_goes_inline(tmp_path, monkeypatch):
    """≤200 字符的多行粘贴直接进输入框（不折叠）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        await inp._on_paste(events.Paste("a\nb\nc"))
        await pilot.pause(0.2)
        assert inp.value == "a\nb\nc"
        assert inp._pastes == {}
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_duplicate_paste_event_is_suppressed(tmp_path, monkeypatch):
    """Some terminal frontends report the same paste twice; keep one copy."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        await inp._on_paste(events.Paste("abc\n"))
        await inp._on_paste(events.Paste("abc\n"))
        await pilot.pause(0.2)
        assert inp.value == "abc\n"
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_paste_replayed_keys_are_suppressed(tmp_path, monkeypatch):
    """Paste 后终端重放逐字符 Key（含 CR/LF）时，不应再次插入或触发提交。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        app.busy = True
        await inp._on_paste(events.Paste("a\nb"))
        for key, character in (
            ("a", "a"),
            ("enter", "\r"),
            ("b", "b"),
        ):
            await inp._on_key(events.Key(key, character))
        await pilot.pause(0.2)
        assert inp.value == "a\nb"
        assert app.steer.drain() == []
        app.busy = False
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_duplicate_submit_event_is_suppressed(tmp_path, monkeypatch):
    """Repeated Enter submit events for the same buffer should not double-steer."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.busy = True
        _submit(app, "同一条 steer")
        _submit(app, "同一条 steer")
        await pilot.pause(0.2)
        assert app.steer.drain() == ["同一条 steer"]
        app.busy = False
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_orphan_paste_token_submits_literally(tmp_path, monkeypatch):
    """占位符被手敲出来（store 无对应项）时按原文提交，不炸。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        app.busy = True
        inp.value = "看看 [粘贴的文本#9] 这个"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.steer.drain() == ["看看 [粘贴的文本#9] 这个"]
        app.busy = False
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 自动聚焦
async def test_input_focused_on_startup(tmp_path, monkeypatch):
    """启动后输入框直接聚焦。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app.focused is app.query_one(CommandInput)
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_app_focus_event_refocuses_input(tmp_path, monkeypatch):
    """窗口从失焦切回（AppFocus）焦点一定落回输入框。

    常驻焦点策略（_on_blur 自动拉回）下运行中焦点本就不离开输入框，
    AppFocus 是切窗兜底：无论当前焦点在哪，切回窗口直接打字落进输入框。
    """
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.on_app_focus(events.AppFocus())
        await pilot.pause(0.2)
        assert app.focused is app.query_one(CommandInput)
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_persistent_focus_returns_to_input_after_blur(tmp_path, monkeypatch):
    """常驻焦点：运行中焦点离开输入框（_on_blur）下一帧自动拉回。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        other = app.query_one("#status")
        other.can_focus = True
        other.focus()
        await pilot.pause(0.4)
        assert app.focused is inp
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_password_masking_still_works(tmp_path, monkeypatch):
    """兼容旧 Input 的 password 语义：掩码显示，值原样。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        inp.password = True
        inp.value = "secret"
        await pilot.pause(0.2)
        text = "".join(seg.text for seg in inp.render_line(0))
        assert "secret" not in text and "•" in text
        assert inp.value == "secret"  # 值不受影响
        _submit(app, "/quit")
        await pilot.pause(0.3)

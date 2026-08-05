"""视觉升级：用户消息反色条、agent ● 着色、spinner 状态行、快捷键栏 Ctrl+C 复制、
ctrl+t 任务清单、Jump to bottom、竖线光标注入、工具行 spinner。"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from rich.text import Text
from textual.widgets import Static

from foxdog.interactive import ChatApp, CommandInput
from foxdog.protocol.types import (
    AgentMessageDelta,
    AgentReasoningDelta,
    ModelRequestComplete,
    ModelRequestStarted,
    ToolCallBegin,
)
from foxdog.widgets import AgentStreamBlock, ReasoningBlock, ToolCallRow, agent_text, render_todos


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    return ChatApp(workspace=tmp_path, overrides=[], config_path=tmp_path / "c.toml")


def _submit(app: ChatApp, text: str) -> None:
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _timeline(app: ChatApp) -> str:
    parts = []
    for w in app.query("#timeline Static"):
        content = w.render()
        parts.append(content.plain if isinstance(content, Text) else str(content))
    return "\n".join(parts)


# ------------------------------------------------------------ 用户消息反色条 + agent ● 着色
async def test_user_msg_bar_and_agent_bullet(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "打一道题测试反色条")  # fake provider 直接解题
        # 用户消息横条：Static.user-msg 存在且内容带 > 前缀
        user_bars = app.query("Static.user-msg")
        assert user_bars and "> 打一道题测试反色条" in str(user_bars[0].render())
        # agent 文本块带 ● 头（flag 过闸即停，后续文本步不会跑，手工补一个 delta 验证渲染）
        app._queue.put(AgentMessageDelta(delta="agent 的分析回复"))
        await pilot.pause(0.5)
        from foxdog.widgets import AgentStreamBlock

        blocks = app.query(AgentStreamBlock)
        assert blocks, "agent 文本块缺失"
        body = blocks[0].query_one(".agent-text").render().plain
        assert "● agent 的分析回复" in body
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_swarm_streams_do_not_split_paragraphs_across_worker_events(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        # Worker-local request ids intentionally collide, as they do in real swarm loops.
        app._queue.put(ModelRequestStarted(request_id="1:0", turn=1, step=1, worker="worker-1"))
        app._queue.put(
            AgentMessageDelta(delta="The root page ", request_id="1:0", worker="worker-1")
        )
        app._queue.put(ModelRequestStarted(request_id="1:0", turn=1, step=1, worker="worker-2"))
        app._queue.put(AgentMessageDelta(delta="Inspect the ", request_id="1:0", worker="worker-2"))
        app._queue.put(
            AgentReasoningDelta(request_id="1:0", delta="checking attachment\n", worker="worker-2")
        )
        app._queue.put(
            AgentMessageDelta(delta="already leaks the full source.", request_id="1:0", worker="worker-1")
        )
        app._queue.put(AgentMessageDelta(delta="local attachment.", request_id="1:0", worker="worker-2"))
        app._queue.put(ModelRequestComplete(request_id="1:0", duration_ms=50, worker="worker-1"))
        app._queue.put(ModelRequestComplete(request_id="1:0", duration_ms=60, worker="worker-2"))
        await pilot.pause(0.7)

        streams = list(app.query(AgentStreamBlock))
        assert len(streams) == 2
        rendered = [
            streams[i].query_one(".agent-text").render().plain
            for i in range(2)
        ]
        assert "[worker-1] ● The root page already leaks the full source." in rendered[0]
        assert "[worker-2] ● Inspect the local attachment." in rendered[1]
        assert all(text.count("●") == 1 for text in rendered)
        assert len(app.query(ReasoningBlock)) == 1
        _submit(app, "/quit")
        await pilot.pause(0.3)


def test_agent_text_inline_code_and_bold():
    t = agent_text("试 `curl -s x` 然后 **重点** 看")
    plain = t.plain
    assert "curl -s x" in plain and "重点" in plain and "`" not in plain
    span_map = {s: st for s, st in _spans_of(t)}
    assert span_map.get("curl -s x") == "dark_orange"
    assert span_map.get("重点") == "bold"


def test_agent_stream_block_strips_leading_newlines_after_bullet():
    block = AgentStreamBlock()
    rendered = block._render_text("\n\n正文从同一行开始").plain
    assert rendered.startswith("● 正文")
    assert not rendered.startswith("● \n")


def _spans_of(t: Text):
    """从 Text 提取 (文本, style) 对（render 后的 cell 反推太绕，直接读 spans）。"""
    out = []
    for span in t.spans:
        out.append((t.plain[span.start : span.end], str(span.style)))
    return out


# ------------------------------------------------------------ spinner 状态行
async def test_activity_line(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        w = app.query_one("#activity", Static)
        assert (w.render().plain if hasattr(w.render(), "plain") else str(w.render())) == ""  # 空闲视觉隐藏（空文本）
        # 伪造运行中状态
        app.busy = True
        app.current_run = SimpleNamespace(
            started=time.monotonic(),
            loop=SimpleNamespace(budget_state=SimpleNamespace(total_tokens=123)),
            coordinator=None,
            gate=SimpleNamespace(flag=None),
            budget=SimpleNamespace(max_minutes=None),
        )
        app._activity = "shell_exec 执行中"
        app._set_activity_animation(True)
        app._refresh_activity()
        first_frame = w.render().plain
        await pilot.pause(0.3)
        next_frame = w.render().plain
        assert first_frame != next_frame
        assert "shell_exec 执行中" in next_frame and "↓ 123 tokens" in next_frame
        app.busy = False
        app._set_activity_animation(False)
        paused_frame = app._activity_frame
        await pilot.pause(0.15)
        assert app._activity_frame == paused_frame
        app._refresh_activity()
        assert w.render().plain == ""
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_inline_reasoning_is_five_line_dim_italic_tail(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause(0.5)
        app.busy = True
        app.current_run = SimpleNamespace(
            started=time.monotonic(),
            loop=SimpleNamespace(budget_state=SimpleNamespace(total_tokens=123)),
            coordinator=None,
            gate=SimpleNamespace(flag=None),
            budget=SimpleNamespace(max_minutes=None),
        )
        app._queue.put(ToolCallBegin(call_id="http-1", name="http_request", arguments='{"url":"http://x"}'))
        app._queue.put(ModelRequestStarted(request_id="1:1", turn=1, step=2))
        await pilot.pause(0.35)
        activity = app.query_one("#activity", Static).render().plain
        assert "thinking" in activity and "http_request 执行中" not in activity

        for number in range(12):
            app._queue.put(AgentReasoningDelta(request_id="1:1", delta=f"reason-{number}\n"))
        await pilot.pause(0.5)
        block = app.query_one(ReasoningBlock)
        assert block.size.height == 5
        reasoning_widget = block.query_one(".reasoning-text", Static)
        reasoning_text = reasoning_widget.render()
        assert "reason-11" in reasoning_text.plain
        assert isinstance(reasoning_widget.content, Text)
        assert "dim" in str(reasoning_widget.content.style) and "italic" in str(reasoning_widget.content.style)
        assert block.is_vertical_scroll_end
        assert block.max_scroll_y == 0
        assert str(block.styles.overflow_y) == "hidden"

        app._queue.put(
            ModelRequestComplete(
                request_id="1:1",
                duration_ms=117000,
                reasoning_chars=120,
                completion_tokens=13410,
            )
        )
        await pilot.pause(0.35)
        footer = block.query_one(".reasoning-text", Static).render().plain
        assert "thinking 完成 · 1m57s · 13,410 tokens" in footer
        app.busy = False
        app.current_run = None
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_input_recovers_mouse_reports_without_touching_payload(tmp_path, monkeypatch):
    """Orphaned SGR mouse reports are removed, but ordinary bracket text is data."""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        inp.value = "abc[<32;111;23M[<35;112;23Mdef"
        inp.cursor_position = len(inp.value)
        await pilot.pause(0.2)
        assert inp.value == "abcdef"
        assert inp.cursor_position == len("abcdef")
        assert "已过滤异常终端输入" in app.query_one("#shortcuts", Static).render().plain

        inp.value = "payload[C"
        inp.cursor_position = len(inp.value)
        await pilot.pause(0.2)
        assert inp.value == "payload[C"
        assert inp.cursor_position == len("payload[C")
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 快捷键栏 + Ctrl+C 安全复制
async def test_ctrl_c_with_selection_copies_and_never_quits(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        bar = app.query_one("#shortcuts", Static)
        assert "ctrl+c 复制/双击退出" in bar.render().plain  # 常驻快捷键栏
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected evidence")
        await pilot.press("ctrl+c", "ctrl+c", "ctrl+c")
        await pilot.pause(0.3)
        assert app.is_running
        assert app.clipboard == "selected evidence"
        assert "再按一次" not in bar.render().plain
        assert "ctrl+c 复制/双击退出" in bar.render().plain
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ ctrl+t 任务清单
async def test_todos_region(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        region = app.query_one("#todos", Static)
        assert region.styles.display == "none"
        # 伪造带 todo 的 run
        items = [
            SimpleNamespace(status="done", content="侦察"),
            SimpleNamespace(status="in_progress", content="利用"),
            SimpleNamespace(status="pending", content="拿 flag"),
        ]
        tool = SimpleNamespace(items=items)
        app.last_run = SimpleNamespace(registry=SimpleNamespace(get=lambda name: tool if name == "todo_list" else None))
        app.current_run = None
        app.action_toggle_todos()
        await pilot.pause(0.3)
        assert region.styles.display == "block"
        text = region.render().plain
        assert "✓ 侦察" in text and "■ 利用" in text and "□ 拿 flag" in text
        app.action_toggle_todos()
        await pilot.pause(0.2)
        assert region.styles.display == "none"
        _submit(app, "/quit")
        await pilot.pause(0.3)


def test_render_todos_styles():
    t = render_todos([SimpleNamespace(status="done", content="A"), SimpleNamespace(status="in_progress", content="B")])
    assert "✓ A" in t.plain and "■ B" in t.plain


# ------------------------------------------------------------ Jump to bottom（并入状态行）
async def test_jump_to_bottom_hint(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.5)
        tl = app.query_one("#timeline")
        activity = app.query_one("#activity", Static)
        # 灌 100 行使时间线溢出
        for i in range(100):
            app._write_line(f"行 {i}")
        await pilot.pause(0.5)
        assert tl.max_scroll_y > 0
        assert not activity.has_class("jump")  # 锚定在底部：无 jump 提示
        tl.scroll_to(y=0, animate=False)  # 用户上翻
        await pilot.pause(0.8)  # 等 0.5s 定时器
        assert activity.has_class("jump")
        assert "Jump to bottom" in activity.render().plain
        scroll_before = tl.scroll_y
        app._write_line("用户阅读期间抵达的新事件")
        await pilot.pause(0.3)
        assert tl.scroll_y == scroll_before  # 新事件不抢走用户的阅读位置
        tl.scroll_end(animate=False)  # 用户自己滚回底部
        app._refresh_activity()
        await pilot.pause(0.3)
        assert not activity.has_class("jump")
        assert tl.is_vertical_scroll_end
        app._write_line("回到底部后的新事件")
        await pilot.pause(0.3)
        assert tl.is_vertical_scroll_end
        app.action_jump_bottom()  # Ctrl+End 仍可显式回底并重新锚定
        await pilot.pause(0.2)
        assert tl.is_vertical_scroll_end
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_stream_deltas_commit_once_per_event_pump(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._queue.put(AgentMessageDelta(delta="start", request_id="batched"))
        await app._pump_events()
        block = app.query_one(AgentStreamBlock)
        body = block.query_one(".agent-text", Static)
        original_update = body.update
        updates = 0

        def counted_update(*args, **kwargs):
            nonlocal updates
            updates += 1
            return original_update(*args, **kwargs)

        monkeypatch.setattr(body, "update", counted_update)
        for _ in range(100):
            app._queue.put(AgentMessageDelta(delta=" x", request_id="batched"))
        await app._pump_events()
        assert updates == 1
        assert body.render().plain.endswith("start" + " x" * 100)
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 竖线光标注入
async def test_cursor_bar_is_software_glyph(tmp_path, monkeypatch):
    """竖线光标根因修法：Input 软件光标位置渲染 ▏（非硬件 DECSCUSR，反色已去）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        inp.value = "abc"
        inp.cursor_position = 2
        inp.focus()
        await pilot.pause(0.3)
        strip = inp.render_line(0)
        text = "".join(seg.text for seg in strip)
        assert "▏" in text
        # ▏ 落在光标位（index 2）：ab▏c
        assert text.index("▏") == 2
        # 覆盖格样式不带 reverse（根因：软件反色块已移除）
        for seg in strip:
            if "▏" in seg.text:
                assert not (seg.style and seg.style.reverse)
                # 强调色竖线沿用输入区背景，不是 Textual 默认的反色块。
                assert seg.style and seg.style.bgcolor == inp.rich_style.bgcolor
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_empty_placeholder_position_stable(tmp_path, monkeypatch):
    """空输入：placeholder 头部预留格，光标替换式——闪烁/刷新相位下文本位置恒定。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        assert inp.value == ""
        inp.focus()
        await pilot.pause(0.3)

        def strip_text(strip):
            return "".join(seg.text for seg in strip)

        s1 = inp.render_line(0)  # 光标可见相：▏ + placeholder
        assert "▏" in strip_text(s1)
        inp._cursor_visible = False  # 闪烁关相：预留空格（无 ▏）
        s2 = inp.render_line(0)
        t1, t2 = strip_text(s1), strip_text(s2)
        assert "▏" not in t2
        # placeholder 首字位置两相完全一致（不跳）
        assert t1.index("输") == t2.index("输")
        # 除预留格（▏ ↔ 空格）外逐字相同
        assert t1.replace("▏", " ") == t2
        # placeholder 首字不再残留默认块状光标的前景色/背景色。
        bar = next(seg for seg in s1 if "▏" in seg.text)
        first = next(seg for seg in s1 if "输" in seg.text)
        assert first.style and bar.style
        assert first.style.bgcolor == inp.rich_style.bgcolor == bar.style.bgcolor
        assert first.style.color != bar.style.color
        inp._cursor_visible = True
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_bottom_composer_is_single_non_overlapping_flow(tmp_path, monkeypatch):
    """活动行在三行线框外，底部仍是一个 flow，不会被多个 dock 相互覆盖。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one(CommandInput)
        bottom = app.query_one("#bottom")
        row = app.query_one("#input-row")
        shortcuts = app.query_one("#shortcuts")
        activity = app.query_one("#activity")
        assert bottom.styles.dock == "bottom"
        assert inp.styles.dock == "none" and inp.styles.margin.top == inp.styles.margin.bottom == 0
        assert activity.styles.display == "none"  # 空闲时不白占一行
        assert row.region.height == 3
        assert str(row.styles.border_top[0]) == "round"
        assert row.region.x < inp.region.x and inp.region.right < row.region.right
        assert row.region.y + row.region.height <= shortcuts.region.y
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_board_sidebar_is_contextual_and_responsive(tmp_path, monkeypatch):
    """空闲保持单列；宽屏 swarm 运行显示侧栏；窄屏不挤压主时间线。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.5)
        board = app.query_one("#board")
        assert board.styles.display == "none"
        app.busy = True
        app.opt_workers = 3
        app._sync_board_sidebar()
        await pilot.pause(0.2)
        assert board.styles.display == "block"
        app.busy = False
        app._sync_board_sidebar()
        assert board.styles.display == "none"
        _submit(app, "/quit")
        await pilot.pause(0.3)

    narrow = _app(tmp_path, monkeypatch)
    async with narrow.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        narrow.busy = True
        narrow.opt_workers = 3
        narrow._sync_board_sidebar()
        assert narrow.query_one("#board").styles.display == "none"
        _submit(narrow, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 工具行 spinner
async def test_toolcall_row_spinner_and_done(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._queue.put(ToolCallBegin(call_id="c1", name="shell_exec", arguments='{"command":"nmap -sV x"}'))
        await pilot.pause(0.5)  # 泵 + 挂载 + spinner 启动
        row = app.query_one(ToolCallRow)
        await pilot.pause(0.3)  # 转几帧
        summary_running = row.query_one(".tc-summary").render().plain
        assert "✓" not in summary_running and "nmap" in summary_running.replace("\\", "/")  # 进行中无 ✓
        row.set_result(ok=True, duration_ms=88, preview="o")
        assert row.has_class("succeeded")
        await pilot.pause(0.55)
        summary_done = row.query_one(".tc-summary").render().plain
        assert "✓" in summary_done and "88ms" in summary_done
        assert row.has_class("succeeded")  # success color is persistent history state
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_write_file_shows_path_and_ten_line_preview(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        content = "\n".join(f"line-{number}" for number in range(1, 13))
        arguments = json.dumps({"path": "exploits/run.py", "content": content})
        app._queue.put(ToolCallBegin(call_id="write-1", name="write_file", arguments=arguments))
        await pilot.pause(0.4)
        row = app.query_one(ToolCallRow)
        detail = row.query_one(".tc-detail", Static)
        preview = detail.render().plain
        assert detail.styles.display == "block"
        assert "path: exploits/run.py" in preview
        assert "line-10" in preview and "line-11" not in preview and "… +2 lines" in preview

        await pilot.click("ToolCallRow")
        assert row.expanded
        assert "line-12" in detail.render().plain and "+2 lines" not in detail.render().plain
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_edit_file_shows_diff_and_failure_color_persists(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        arguments = json.dumps(
            {"path": "exploit.py", "old": "host = 'old'\nport = 80", "new": "host = 'target'\nport = 8080"}
        )
        app._queue.put(ToolCallBegin(call_id="edit-1", name="edit_file", arguments=arguments))
        await pilot.pause(0.4)
        row = app.query_one(ToolCallRow)
        diff = row.query_one(".tc-detail", Static).render().plain
        assert "--- a/exploit.py" in diff and "+++ b/exploit.py" in diff
        assert "-host = 'old'" in diff and "+host = 'target'" in diff

        row.set_result(ok=False, duration_ms=12, error="old 未在文件中出现")
        await pilot.pause(0.55)
        assert row.has_class("failed")
        assert "✗" in row.query_one(".tc-summary", Static).render().plain
        assert "old 未在文件中出现" in row.query_one(".tc-detail", Static).render().plain
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 腰斩回归：跨 request 的相邻文本合并成一块
async def test_adjacent_requests_merge_into_one_agent_block(tmp_path, monkeypatch):
    """DeepSeek 被腰斩成两条 ● 的根因：同一逻辑消息跨两个 model request（request_id
    不同 → 两块）。修复后：相邻（仅 model_request_complete 间隔）续写并入上一块。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._queue.put(AgentMessageDelta(delta="Deep", request_id="req-1"))
        app._queue.put(ModelRequestComplete(request_id="req-1", duration_ms=10))
        app._queue.put(AgentMessageDelta(delta="Seek", request_id="req-2"))
        app._queue.put(ModelRequestComplete(request_id="req-2", duration_ms=10))
        await pilot.pause(0.7)
        blocks = list(app.query(AgentStreamBlock))
        assert len(blocks) == 1, f"应合并为 1 块，实际 {len(blocks)} 块"
        body = blocks[0].query_one(".agent-text").render().plain
        assert "DeepSeek" in body
        assert body.count("●") == 1
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_tool_event_breaks_merge_into_new_block(tmp_path, monkeypatch):
    """工具事件介入后，新的模型文本应另起一块（正常多步 turn）。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._queue.put(AgentMessageDelta(delta="第一步分析", request_id="r1"))
        app._queue.put(ModelRequestComplete(request_id="r1", duration_ms=5))
        app._queue.put(ToolCallBegin(call_id="c1", name="shell_exec", arguments="{}"))
        app._queue.put(AgentMessageDelta(delta="第二步结论", request_id="r2"))
        await pilot.pause(0.7)
        blocks = list(app.query(AgentStreamBlock))
        assert len(blocks) == 2, f"工具介入后应为 2 块，实际 {len(blocks)}"
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ ReasoningBlock rewrap 缓存
async def test_reasoning_block_rewrap_cache_on_unchanged_width(tmp_path, monkeypatch):
    """同 width 且 buffer/footer 未变时，_refresh_content 不重跑 textwrap.wrap（resize 扇出优化）。"""
    import foxdog.widgets as W
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause(0.4)
        block = ReasoningBlock("req-1", None)
        app.query_one("#timeline").mount(block)
        for i in range(20):
            block.append(f"reasoning line {i} " + "x" * 30 + "\n")
        await pilot.pause(0.3)
        block.finish(1500, 100)
        await pilot.pause(0.3)
        # 第一次 refresh 已发生，缓存填充
        assert block._wrapped_width is not None
        wrap_calls = [0]
        orig = W.textwrap.wrap

        def counting_wrap(*a, **k):
            wrap_calls[0] += 1
            return orig(*a, **k)

        monkeypatch.setattr(W.textwrap, "wrap", counting_wrap)
        # 同 width 再触发 refresh：buffer/footer 未变 → 应复用缓存，wrap 调用 0 次
        block._refresh_scheduled = True
        block.call_after_refresh(block._refresh_content)
        await pilot.pause(0.3)
        assert wrap_calls[0] == 0, f"同 width 未变时不应重 wrap，实际 {wrap_calls[0]} 次"
        # buffer 变化（append）→ 应触发 wrap
        block.append("new tail content here\n")
        await pilot.pause(0.3)
        assert wrap_calls[0] >= 1
        _submit(app, "/quit")
        await pilot.pause(0.3)

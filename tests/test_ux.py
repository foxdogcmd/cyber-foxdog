"""UX 大修：ToolCallRow 折叠、黑板渲染器、/board 全屏、Ctrl+C 语义、预算、copilot 指挥。"""
from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from foxdog.interactive import ChatApp, CommandInput
from foxdog.llm.client import FakeProvider
from foxdog.protocol.types import ToolCallBegin, ToolCallEnd
from foxdog.widgets import render_deadends, render_facts, render_intents, tail_fold, ToolCallRow
from foxdog import runner as runner_mod

FLAG = "flag{smoke_test_flag_123}"
READ_CMD = (
    "Get-Content -Raw -LiteralPath 'fake_target_flag.txt'"
    if sys.platform == "win32"
    else "cat fake_target_flag.txt"
)
SLEEP_CMD = f'{sys.executable} -c "import time;time.sleep(2)"'


def _submit(app: ChatApp, text: str) -> None:
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    return ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")


# ------------------------------------------------------------ 黑板渲染器（纯函数）
def test_render_intents_grouped_one_line():
    intents = [
        {"id": "I1-aaa111", "goal": "对 8080 做目录爆破", "status": "claimed", "claimed_by": "worker-2"},
        {"id": "I2-bbb222", "goal": "试 SQL 注入", "status": "open", "claimed_by": None},
        {"id": "I3-ccc333", "goal": "读源码泄露", "status": "done", "claimed_by": "worker-1", "result": "x"},
        {"id": "I4-ddd444", "goal": "爆破后台密码", "status": "barren", "claimed_by": None},
    ]
    text = render_intents(intents).plain
    # 分组图标齐全、每条一行、claimed 带 by
    assert "⧗ claimed" in text and "○ open" in text and "✓ done" in text and "✗ barren" in text
    assert "I1-aaa111 [by worker-2] 对 8080 做目录爆破" in text
    # claimed 组在 open 组前
    assert text.index("⧗") < text.index("○")

    styled = render_intents(intents)
    styles = [str(span.style) for span in styled.spans]
    assert any("yellow" in style for style in styles)
    assert any("cyan" in style for style in styles)
    assert any("green" in style for style in styles)
    assert any("red" in style for style in styles)
    assert any("magenta" in style for style in styles)


def test_render_tail_fold_and_more():
    facts = [{"fact": "x" * 500, "evidence": None}]  # 超长折叠
    text = render_facts(facts).plain
    assert "…" in text and len(text) < 500
    many = [{"fact": f"事实{i}", "evidence": None} for i in range(25)]
    text2 = render_facts(many).plain
    assert "+5 more" in text2
    assert tail_fold("短") == "短"
    # deadends 同理
    d = [{"direction": "dir", "reason": "r"} for _ in range(22)]
    assert "+2 more" in render_deadends(d).plain


# ------------------------------------------------------------ ToolCallRow 折叠
async def test_toolcall_row_toggle(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._queue.put(ToolCallBegin(call_id="c1", name="shell_exec", arguments='{"command": "curl -s http://x"}'))
        app._queue.put(ToolCallEnd(call_id="c1", name="shell_exec", ok=True, output_preview="out", duration_ms=42, handle="h123"))
        await pilot.pause(0.5)
        row = app.query_one(ToolCallRow)
        assert not row.expanded
        summary = row.query_one(".tc-summary").render().plain
        assert "✓" in summary and "42ms" in summary and "shell_exec" in summary
        await pilot.click("ToolCallRow")  # 点击展开
        assert row.expanded
        detail = row.query_one(".tc-detail").render().plain
        assert "curl -s http://x" in detail and "h123" in detail
        await pilot.click("ToolCallRow")  # 再点折叠
        assert not row.expanded
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ /board 全屏
async def test_board_fullscreen_toggle(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert not app._board_full
        _submit(app, "/board")
        await pilot.pause(0.3)
        assert app._board_full
        assert app.query_one("#boardfull").styles.display == "block"
        await pilot.press("escape")  # Esc 返回（不是中断任务）
        await pilot.pause(0.3)
        assert not app._board_full
        assert app.query_one("#boardfull").styles.display == "none"
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_clicking_sidebar_section_opens_full_board(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.5)
        app.query_one("#board").styles.display = "block"
        await pilot.click("#intents")
        await pilot.pause(0.3)
        assert app._board_full
        assert app.query_one("#boardfull").styles.display == "block"
        await pilot.press("escape")  # Esc 返回（不是中断任务）
        await pilot.pause(0.3)
        assert not app._board_full
        assert app.query_one("#boardfull").styles.display == "none"
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 黑板全屏逐条展开
from foxdog.widgets import BoardItem, fact_item  # noqa: E402


async def test_board_item_toggle(tmp_path, monkeypatch):
    """BoardItem 单条：长条默认折叠含 [展开]，点击展开见全文+evidence，再点折叠。"""
    app = _app(tmp_path, monkeypatch)
    long_fact = {
        "id": 1,
        "fact": "SQL 盲注 payload ' OR 1=1-- - " + "z" * 260,
        "evidence": "cas://abc123",
        "status": "unresolved",
    }
    item = BoardItem(*fact_item(long_fact))
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        app.query_one("#timeline").mount(item)
        await pilot.pause(0.2)
        assert item.foldable
        assert not item.expanded
        summary = item.query_one(".bi-summary").render().plain
        assert "[展开]" in summary and "…" in summary
        assert item.query_one(".bi-detail").styles.display == "none"  # 折叠态隐藏
        await pilot.click(item)
        await pilot.pause(0.2)
        assert item.expanded
        assert "z" * 260 in item.query_one(".bi-summary").render().plain  # 全文未截断
        assert "cas://abc123" in item.query_one(".bi-detail").render().plain
        assert item.query_one(".bi-detail").styles.display == "block"  # 展开态显示
        await pilot.click(item)  # 再点折叠
        await pilot.pause(0.2)
        assert not item.expanded
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_board_fullscreen_click_expand(tmp_path, monkeypatch):
    """全屏黑板里长 fact 折叠 + [展开]，点击见全文+evidence；短 fact 不可展开。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [("题", [("tool", "shell_exec", {"command": SLEEP_CMD}), ("text", "done")])]
    real = runner_mod.build_session

    async def patched(**kw):
        s = await real(**kw)
        s.provider = FakeProvider(routes=routes, script=[("text", "x")])
        return s

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "题 smoke")
        deadline = time.monotonic() + 10
        while app.current_run is None and time.monotonic() < deadline:
            await pilot.pause(0.1)
        assert app.current_run is not None
        fb = app.current_run.ctx.facts
        long_text = "SQL 注入点 /login 布尔盲注 payload ' OR 1=1-- - " + "z" * 260
        fb.post_fact(long_text, evidence_handle="cas://abc123")
        fb.post_fact("端口 80 开放 nginx")
        _submit(app, "/board")
        await pilot.pause(0.4)
        items = list(app.query("#bf-facts BoardItem"))
        assert len(items) == 2, [str(w) for w in items]
        foldable = [w for w in items if w.foldable]
        assert len(foldable) == 1
        long_item = foldable[0]
        assert "[展开]" in long_item.query_one(".bi-summary").render().plain
        await pilot.click(long_item)
        await pilot.pause(0.2)
        assert long_item.expanded
        assert "z" * 260 in long_item.query_one(".bi-summary").render().plain
        assert "cas://abc123" in long_item.query_one(".bi-detail").render().plain
        # 短 fact 不可展开：点击无反应
        short_item = [w for w in items if not w.foldable][0]
        await pilot.click(short_item)
        await pilot.pause(0.1)
        assert not short_item.expanded
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_board_refresh_preserves_expand(tmp_path, monkeypatch):
    """展开一条 fact 后黑板刷新（签名变化）仍保持展开态（diff 原地 update）。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [("题", [("tool", "shell_exec", {"command": SLEEP_CMD}), ("text", "done")])]
    real = runner_mod.build_session

    async def patched(**kw):
        s = await real(**kw)
        s.provider = FakeProvider(routes=routes, script=[("text", "x")])
        return s

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "题 smoke")
        deadline = time.monotonic() + 10
        while app.current_run is None and time.monotonic() < deadline:
            await pilot.pause(0.1)
        assert app.current_run is not None
        fb = app.current_run.ctx.facts
        long_text = "长事实 " + "z" * 260
        fb.post_fact(long_text, evidence_handle="cas://ev1")
        _submit(app, "/board")
        await pilot.pause(0.4)
        long_item = next(w for w in app.query("#bf-facts BoardItem") if w.foldable)
        await pilot.click(long_item)
        await pilot.pause(0.2)
        assert long_item.expanded
        # 灌新 fact 改变签名 -> 触发刷新
        fb.post_fact("另一条短事实")
        app._refresh_board()
        await pilot.pause(0.3)
        # 同一 widget 对象仍展开
        assert long_item.expanded
        assert len(list(app.query("#bf-facts BoardItem"))) == 2
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ Ctrl+C 语义
async def test_ctrl_c_semantics(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    binding = next(binding for binding in app.BINDINGS if binding.key == "ctrl+c")
    assert binding.action == "handle_ctrl_c" and binding.priority
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected evidence")
        await pilot.press("ctrl+c")
        await pilot.pause(0.2)
        assert app.clipboard == "selected evidence"
        assert app.is_running
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_ctrl_c_never_quits_when_busy(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        interrupt = asyncio.Event()
        run = SimpleNamespace(
            interrupted=False,
            started=time.monotonic(),
            loop=SimpleNamespace(interrupt=interrupt, budget_state=None),
            coordinator=None,
            budget=SimpleNamespace(max_minutes=None),
        )
        app.busy = True
        app.current_run = run
        await pilot.press("ctrl+c", "ctrl+c")
        await pilot.pause(0.3)
        assert app.is_running
        assert run.interrupted and interrupt.is_set()
        app.busy = False
        app.current_run = None
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_ctrl_c_cancels_inflight_thinking_immediately(tmp_path, monkeypatch):
    """Regression: setting Event alone used to keep receiving thinking until EOF."""

    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.reasoning_callbacks = 0

        async def stream_chat(
            self, _messages, _tools, _on_delta, *, require_tool=False, on_reasoning_delta=None
        ):
            del require_tool
            self.started.set()
            try:
                while True:
                    if on_reasoning_delta is not None:
                        self.reasoning_callbacks += 1
                        on_reasoning_delta("仍在思考\n")
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        provider = BlockingProvider()
        assert app.session is not None
        app.session.provider = provider
        app.opt_workers = 1
        _submit(app, "运行一个慢请求")
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await pilot.pause(0.05)
        before = provider.reasoning_callbacks

        await pilot.press("ctrl+c")
        await asyncio.wait_for(provider.cancelled.wait(), timeout=0.5)
        for _ in range(20):
            if not app.busy:
                break
            await pilot.pause(0.05)

        after = provider.reasoning_callbacks
        await pilot.pause(0.1)
        assert provider.reasoning_callbacks == after
        assert after <= before + 1
        assert not app.busy
        assert app.current_run is None
        assert app.last_run is not None and app.last_run.interrupted
        assert app.is_running
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_ctrl_c_requires_double_press_to_exit_when_idle(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app.is_running
        await pilot.press("ctrl+c")
        await pilot.pause(0.15)
        assert app.is_running
        assert "再次 ctrl+c 退出" in app.query_one("#shortcuts").render().plain
        await pilot.press("ctrl+c")
        await pilot.pause(0.3)
        assert not app.is_running


async def test_idle_ctrl_c_confirmation_expires(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+c")
        app._idle_ctrl_c_armed_at = time.monotonic() - 3
        await pilot.press("ctrl+c")
        await pilot.pause(0.2)
        assert app.is_running
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ 预算内部入口（用户侧走 /setting）
async def test_budget_internal_entrypoint(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._cmd_budget("")
        await pilot.pause(0.2)
        assert "∞" in app.query_one("#timeline").export_text() if hasattr(app.query_one("#timeline"), "export_text") else "∞" in str(app.session.cfg.budget.describe())
        assert app.session.cfg.budget.max_minutes is None  # 默认无限
        app._cmd_budget("minutes 30")
        await pilot.pause(0.2)
        assert app.session.cfg.budget.max_minutes == 30.0
        app._cmd_budget("tokens 500000")
        await pilot.pause(0.2)
        assert app.session.cfg.budget.max_tokens == 500000
        app._cmd_budget("off")
        await pilot.pause(0.2)
        assert app.session.cfg.budget.max_minutes is None and app.session.cfg.budget.max_tokens is None
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ copilot steer_team（解题中 /setting）
async def test_setting_copilot_steer_team_while_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [
        # 任务角色：先睡 2s（保证 busy 窗口），再读 flag 提交
        ("打一道题", [
            ("tool", "shell_exec", {"command": SLEEP_CMD}),
            ("tool", "shell_exec", {"command": READ_CMD}),
            ("tool", "submit_flag", {"flag": FLAG}),
            ("text", "done"),
        ]),
        # copilot 角色：调 steer_team
        ("指挥方向", [
            ("tool", "steer_team", {"text": "全员转向 session 竞争"}),
            ("text", "已指挥"),
        ]),
    ]
    real_build_session = runner_mod.build_session

    async def patched(**kwargs):
        session = await real_build_session(**kwargs)
        session.provider = FakeProvider(routes=routes, script=[("text", "无匹配")])
        return session

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "打一道题 smoke")
        # 等 current_run 落地（build_run 完成）——否则 /setting 的 steer_team 会在
        # current_run=None 时降级为"只入队不上板"（docker 模式 build_run 慢时必现的竞态）
        deadline = time.monotonic() + 10
        while app.current_run is None and time.monotonic() < deadline:
            await pilot.pause(0.1)
        assert app.current_run is not None and app.busy
        _submit(app, "/setting 指挥方向：全员转向 session 竞争")
        # 等 copilot task 完成（_setting_busy 可能还没拉起，直接等 task 对象落地）
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await pilot.pause(0.2)
            t = getattr(app, "_solve_task_copilot", None)
            if t is not None and t.done():
                break
        # 任务黑板出现 operator directive，不污染已知事实
        board = app.current_run.ctx.facts
        directives = [d["content"] for d in board.directives()]
        assert any("操作员指令" in d and "session 竞争" in d for d in directives), directives
        facts = [f["content"] for f in board.facts()]
        assert not any("操作员指令" in f for f in facts), facts
        # 等任务正常跑完
        deadline = time.monotonic() + 15
        while app.busy and time.monotonic() < deadline:
            await pilot.pause(0.3)
        assert app.last_run.gate.solved
        _submit(app, "/quit")
        await pilot.pause(0.3)


# ------------------------------------------------------------ timeline 窗口化
async def test_timeline_windowizer_trims_to_history(tmp_path, monkeypatch):
    """长任务时间线：超 _MAX_MOUNTED 的旧条目单向裁剪进只读历史快照（不卡顿核心）。

    单向裁剪（2026-08-05 重写）：裁剪后挂载数有界，被裁条目并入顶部单个历史 Static；
    滚动不再触发任何 mount/unmount（旧 fold/unfold 双向路径是卡死根因，已删除）。
    """
    from foxdog.timeline_window import _MAX_MOUNTED
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [("题", [("tool", "shell_exec", {"command": SLEEP_CMD}), ("text", "done")])]
    real = runner_mod.build_session

    async def patched(**kw):
        s = await real(**kw)
        s.provider = FakeProvider(routes=routes, script=[("text", "x")])
        return s

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        _submit(app, "题 smoke")
        deadline = time.monotonic() + 10
        while app.current_run is None and time.monotonic() < deadline:
            await pilot.pause(0.1)
        assert app.current_run is not None
        w = app._timeline_windowizer
        # 灌入远超 _MAX_MOUNTED 的事件（纯 turn_started 标记，走 _write 静态条目）
        from foxdog.protocol.types import TurnStarted
        for i in range(_MAX_MOUNTED + 60):
            app._queue.put(TurnStarted(turn=i, worker=None))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            await pilot.pause(0.3)
            if w._history is not None and sum(1 for e in w._entries if e.is_mounted) <= _MAX_MOUNTED:
                break
        mounted = sum(1 for e in w._entries if e.is_mounted)
        total = len(w._entries)
        # 挂载数有界（不随事件数线性增长）：远小于总条目数
        assert mounted <= _MAX_MOUNTED, f"挂载 {mounted} 未有界（总 {total}）"
        assert mounted < total, f"挂载 {mounted} 应 < 总 {total}（未裁剪）"
        assert w._history is not None, "应已生成历史快照 Static"
        assert w._history_text.strip(), "历史快照应含被裁剪条目的文本"
        # 滚动不改变挂载数（单向：滚动不触发挂载操作，这是不卡死的核心保证）
        tl = app.query_one("#timeline")
        tl.scroll_relative(y=-10000, animate=False)
        await pilot.pause(0.5)
        mounted2 = sum(1 for e in w._entries if e.is_mounted)
        assert mounted2 == mounted, f"滚动不应改变挂载数 {mounted} → {mounted2}（单向裁剪）"
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_timeline_windowizer_preserves_tool_row_state(tmp_path, monkeypatch):
    """被裁剪的 ToolCallRow：state 对象（_rows["c1"]）保留，文本进历史快照。"""
    from foxdog.timeline_window import _MAX_MOUNTED
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [("题", [("tool", "shell_exec", {"command": SLEEP_CMD}), ("text", "done")])]
    real = runner_mod.build_session

    async def patched(**kw):
        s = await real(**kw)
        s.provider = FakeProvider(routes=routes, script=[("text", "x")])
        return s

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    app = _app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        _submit(app, "题 smoke")
        deadline = time.monotonic() + 10
        while app.current_run is None and time.monotonic() < deadline:
            await pilot.pause(0.1)
        assert app.current_run is not None
        # 放一个成功的工具行，再灌大量事件把它挤出视口裁剪
        app._queue.put(ToolCallBegin(call_id="c1", name="shell_exec", arguments='{"command":"echo hi"}'))
        app._queue.put(ToolCallEnd(call_id="c1", name="shell_exec", ok=True, output_preview="hi", duration_ms=5, handle="h1"))
        await pilot.pause(0.5)
        from foxdog.protocol.types import TurnStarted
        for i in range(_MAX_MOUNTED + 60):
            app._queue.put(TurnStarted(turn=i, worker=None))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            await pilot.pause(0.3)
            w = app._timeline_windowizer
            ent = w.find_entry_by_widget(app._rows.get("c1"))
            if ent is None or ent.trimmed:
                break
        w = app._timeline_windowizer
        # _rows 里仍是原对象（state 在，可供 /board 等查询），即使 widget 已卸载
        row = app._rows["c1"]
        assert row.ok is True
        _submit(app, "/quit")
        await pilot.pause(0.3)

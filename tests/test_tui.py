"""TUI smoke：run_test pilot 启动 app、事件泵渲染、按 q 退出不炸。"""
from __future__ import annotations

import queue

from foxdog.protocol.types import BackgroundTaskComplete, SwarmNote, ToolCallBegin, ToolCallEnd
from foxdog.tui import FoxdogApp


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    return FoxdogApp(
        objective="smoke",
        target=None,
        flag_format=r"flag\{[^{}]*\}",
        attach=[],
        mode="yolo",
        max_minutes=1,
        max_tokens=None,
        max_turns=None,
        workers=1,
        planner_model=None,
        workspace=tmp_path,
        overrides=["runtime.kind=local"],
    )


async def test_tui_launch_and_quit(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        # solve 装配完成（run 已建），时间线/状态行存在
        assert app.run is not None
        app._refresh_status()
        assert "引擎" in app.query_one("#status").render().plain
        app.query_one("#timeline")
        await pilot.press("q")  # 退出（solve task 被取消，execute_run finally 清理）
    # 不炸即可；退出后清理已执行（rollout 落盘）
    assert (app.run.ws.dir / "rollout.jsonl").is_file()


async def test_tui_event_pump(tmp_path, monkeypatch):
    """queue 泵：事件进 SimpleQueue → _pump_events 渲染进 RichLog。"""
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert app.run is not None
        # 手工塞事件进泵
        app._queue.put(ToolCallBegin(call_id="c1", name="shell_exec", arguments='{"command":"id"}', worker="worker-1"))
        app._queue.put(ToolCallEnd(call_id="c1", name="shell_exec", ok=True, duration_ms=12, worker="worker-1"))
        app._queue.put(BackgroundTaskComplete(task_id="task-1", command="scan", ok=True, exit_code=0))
        app._queue.put(SwarmNote(message="planner 新派 intent"))
        await pilot.pause(0.5)  # 等定时器泵
        text = "\n".join(str(line.text) for line in app.query_one("#timeline").lines)
        assert "shell_exec" in text and "worker-1" in text
        assert "后台任务 task-1 完成" in text
        assert "planner 新派 intent" in text
        # steer 输入框：提交文本进 steer 队列
        app.on_input_submitted(type("E", (), {"value": "先试 XX 方向", "input": app.query_one("Input")})())
        assert app.steer.drain() == ["先试 XX 方向"]
        await pilot.press("q")


async def test_tui_ctrl_c_copies_and_never_quits(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected evidence")
        await pilot.press("ctrl+c", "ctrl+c")
        await pilot.pause(0.2)
        assert app.clipboard == "selected evidence"
        assert app.is_running
        await pilot.press("q")


def test_tui_queue_sentinel():
    """泵逻辑不依赖 app 也能验证：None 哨兵语义。"""
    q: queue.SimpleQueue = queue.SimpleQueue()
    q.put("event")
    q.put(None)
    assert q.get_nowait() == "event"
    assert q.get_nowait() is None

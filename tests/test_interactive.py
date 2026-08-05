"""交互式会话 TUI：启动欢迎、连续两题（history 延续 + run 隔离）、斜杠命令、steer、退出。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

from foxdog.executor.background import BackgroundRegistry
from foxdog.executor.process import ExecResult
from foxdog.interactive import ChatApp, CommandInput
from foxdog.llm.client import FakeProvider
from foxdog import runner as runner_mod

FLAG = "flag{smoke_test_flag_123}"
READ_CMD = (
    "Get-Content -Raw -LiteralPath 'fake_target_flag.txt'"
    if sys.platform == "win32"
    else "cat fake_target_flag.txt"
)


def _submit(app: ChatApp, text: str) -> None:
    """模拟输入框提交。"""
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _timeline(app: ChatApp) -> str:
    """时间线文本聚合（VerticalScroll 里的 Static 内容，含 ToolCallRow 内部行）。"""
    from rich.text import Text

    parts = []
    for w in app.query("#timeline Static"):
        content = w.render()
        parts.append(content.plain if isinstance(content, Text) else str(content))
    return "\n".join(parts)


async def _wait_done(app: ChatApp, pilot, timeout: float = 30.0) -> None:
    """等当前任务跑完（last_run 落地且不忙）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.2)
        if app.last_run is not None and not app.busy:
            return
    raise AssertionError("任务超时未完成")


async def test_launch_welcome_and_quit(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        # 单层状态栏钉在顶部；不再用两个 dock:top 组件互相覆盖。
        status = app.query_one("#status")
        status_text = status.render().plain
        assert "foxdog" in status_text and "引擎" in status_text and "workers" in status_text and "空闲" in status_text
        assert app.session is not None
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_two_tasks_history_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    # 会话级共享 provider：每题重放同一解题剧本（真实场景是同一会话持续打多题）
    real_build_session = runner_mod.build_session

    async def patched_build_session(**kwargs):
        session = await real_build_session(**kwargs)
        session.provider = FakeProvider(repeat=True)
        return session

    monkeypatch.setattr("foxdog.interactive.build_session", patched_build_session)
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "第一题 smoke")
        await _wait_done(app, pilot)
        text = _timeline(app)
        assert "FLAG VERIFIED" in text and "✔ 解题成功" in text
        first_history_len = len(app.history)
        assert first_history_len > 2  # system+user+assistant/tool 若干
        first_run_dir = app.last_run.ws.dir

        _submit(app, "第二题 smoke")
        await _wait_done(app, pilot)
        # 对话历史延续（两个任务的 user prompt 都在）
        task_prompts = [m for m in app.history if m.get("role") == "user" and "题目：" in str(m.get("content"))]
        assert len(task_prompts) == 2
        assert "第一题" in str(task_prompts[0]["content"]) and "第二题" in str(task_prompts[1]["content"])
        # 每题独立 run（Workspace/FlagGate/黑板隔离）
        assert app.last_run.ws.dir != first_run_dir
        assert app.last_run.gate.solved
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_slash_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "/help")
        await pilot.pause(0.2)
        help_text = _timeline(app)
        assert "任务：" in help_text and "配置：" in help_text and "控制：" in help_text  # 分组重写后的 /help
        command_names = {sc.name for sc in app._commands}
        assert not {"/runs", "/resume", "/target", "/fetch", "/budget"} & command_names
        assert "/runs" not in help_text and "/resume" not in help_text and "/target" not in help_text

        _submit(app, "/mode plan")
        await pilot.pause(0.2)
        assert app.session.permission.mode == "plan"
        _submit(app, "/mode yolo")

        _submit(app, "/workers 3")
        await pilot.pause(0.2)
        assert app.opt_workers == 3

        _submit(app, "/format flag\\{.*\\}")
        await pilot.pause(0.2)
        assert app.opt_format == "flag\\{.*\\}"

        # /new 清空对话历史
        app.history.append({"role": "user", "content": "旧内容"})
        _submit(app, "/new")
        await pilot.pause(0.2)
        assert app.history == []

        # 未知命令不崩溃
        _submit(app, "/foobar")
        await pilot.pause(0.2)
        assert "未知命令 /foobar" in _timeline(app)
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_workers_command_dynamically_updates_active_swarm(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=[], config_path=tmp_path / "c.toml")

    class DynamicCoordinator:
        workers = 2
        desired_workers = 2

        def set_worker_limit(self, workers: int) -> int:
            previous = self.workers
            self.workers = workers
            self.desired_workers = workers
            return previous

    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        coordinator = DynamicCoordinator()
        app.busy = True
        app.current_run = SimpleNamespace(
            coordinator=coordinator,
            started=time.monotonic(),
            loop=SimpleNamespace(budget_state=None),
            budget=SimpleNamespace(max_minutes=None),
        )
        _submit(app, "/workers 5")
        await pilot.pause(0.2)
        assert app.opt_workers == 5
        assert coordinator.workers == 5 and coordinator.desired_workers == 5
        assert "当前任务 worker 上限已从 2 调整为 5" in _timeline(app)

        _submit(app, "/worker 1")
        await pilot.pause(0.2)
        assert app.opt_workers == 1
        assert coordinator.workers == 1 and coordinator.desired_workers == 1
        assert "已在运行的 worker 不会被强制终止" in _timeline(app)

        app.busy = False
        app.current_run = None
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_steer_during_solve(tmp_path, monkeypatch):
    """解题中提交 steer：drain 进下一轮 history（rollout 有 steer 记录）。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    # 多 turn 脚本：turn1 纯文本自然停 → turn2 读 flag 提交（steer 在 turn 间注入）
    script = [
        ("text", "先看看情况"),
        ("tool", "shell_exec", {"command": READ_CMD}),
        ("tool", "submit_flag", {"flag": FLAG}),
        ("text", "done"),
    ]
    real_build_session = runner_mod.build_session

    async def patched_build_session(**kwargs):
        session = await real_build_session(**kwargs)
        session.provider = FakeProvider(script=script)
        return session

    monkeypatch.setattr("foxdog.interactive.build_session", patched_build_session)
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        _submit(app, "打这道题")
        app.steer.put("先试读 fake_target_flag.txt")  # 同 tick 放入，赶在 turn 间 drain 前
        await _wait_done(app, pilot)
        # history 里出现 steer user message
        steer_msgs = [
            m for m in app.history
            if m.get("role") == "user" and "操作员 steer 指令" in str(m.get("content"))
        ]
        assert steer_msgs and "先试读 fake_target_flag.txt" in str(steer_msgs[0]["content"])
        # rollout 记了 steer
        records = [
            json.loads(l)
            for l in (app.last_run.ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert any(r.get("type") == "steer" for r in records)
        assert app.last_run.gate.solved
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_busy_submit_goes_to_steer_queue(tmp_path, monkeypatch):
    """UI 契约：busy 时提交的文本进 steer 队列而不是开新任务。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.busy = True  # 模拟解题中
        _submit(app, "换 SQL 注入方向试试")
        await pilot.pause(0.2)
        assert app.steer.drain() == ["换 SQL 注入方向试试"]
        assert app._solve_task is None  # 没有开新任务
        app.busy = False
        _submit(app, "/quit")
        await pilot.pause(0.5)


async def test_ctrl_b_detaches_current_foreground_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    app = ChatApp(workspace=tmp_path, overrides=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        release = asyncio.Event()
        background = BackgroundRegistry()

        async def runner(on_output):
            await release.wait()
            return ExecResult(exit_code=0, output="done")

        task = background.start("slow command", runner)
        task.foreground_waiting = True
        app.busy = True
        app.current_run = SimpleNamespace(background=background)
        await pilot.press("ctrl+b")
        app.busy = False
        app.current_run = None

        assert task.detached and task.detach_requested.is_set()
        assert "已转后台" in _timeline(app) and "slow command" in _timeline(app)
        release.set()
        await task.done.wait()
        await background.stop_all()
        _submit(app, "/quit")
        await pilot.pause(0.3)

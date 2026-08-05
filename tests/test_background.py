"""Foreground shell detaching and one-shot background completion delivery."""
from __future__ import annotations

import asyncio
import sys
import time

from foxdog.core.budget import Budget, BudgetState
from foxdog.core.goal import GoalMode
from foxdog.core.loop import AgentLoop
from foxdog.core.orchestrator import ToolOrchestrator
from foxdog.core.permission import PermissionChain
from foxdog.core.steer import SteerQueue
from foxdog.executor.background import BackgroundRegistry
from foxdog.executor.process import ExecResult
from foxdog.llm.client import FakeProvider
from foxdog.llm.messages import system_message, user_message
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext, ToolRegistry
from foxdog.tools.builtin.shell import ShellExecTool

PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable


async def test_running_shell_can_detach_without_stopping_process(tmp_path):
    workspace = Workspace(base=tmp_path, run_id="detach-shell")
    background = BackgroundRegistry()
    ctx = ToolContext(workspace, background, emit=lambda event: None, fetch_output=lambda h, o, l: "")
    command = (
        f'{PYTHON} -c "import time;print(\'started\',flush=True);'
        "time.sleep(0.6);print('finished',flush=True)\""
    )
    invocation = asyncio.create_task(
        ShellExecTool(promote_after=30).run({"command": command}, ctx)
    )
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if any(task.foreground_waiting for task in background._tasks.values()):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("shell command never entered foreground wait")

        detached = background.detach_latest()
        assert detached is not None and detached.detached
        tool_result = await asyncio.wait_for(invocation, timeout=1)
        assert detached.task_id in tool_result.output and "Ctrl+B" in tool_result.output
        assert detached.running

        await asyncio.wait_for(detached.done.wait(), timeout=2)
        assert detached.result is not None and detached.result.exit_code == 0
        assert "finished" in detached.tail()
        assert background.drain_completed() == [detached]
        assert background.drain_completed() == []
    finally:
        await background.stop_all()


async def test_background_completion_is_archived_and_injected_once(tmp_path):
    workspace = Workspace(base=tmp_path, run_id="background-notify")
    background = BackgroundRegistry()

    async def runner(on_output):
        on_output("background evidence\n")
        return ExecResult(exit_code=0, output="background evidence\n")

    task = background.start("long-scan", runner)
    task.detached = True
    await task.done.wait()

    events = []
    registry = ToolRegistry()
    ctx = ToolContext(workspace, background, emit=events.append, fetch_output=lambda h, o, l: "")
    orchestrator = ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx)
    loop = AgentLoop(
        provider=FakeProvider(script=[("text", "收到后台结果")]),
        registry=registry,
        orchestrator=orchestrator,
        emit=events.append,
        max_steps=2,
    )
    history = [system_message("test"), user_message("run")]
    await loop.run_turn(history, turn_no=1)

    notices = [str(message.get("content")) for message in history if "后台任务完成通知" in str(message.get("content"))]
    assert len(notices) == 1
    assert "task-1" in notices[0] and "background evidence" in notices[0]
    completed = [event for event in events if event.kind == "background_task_complete"]
    assert len(completed) == 1 and completed[0].ok and completed[0].handle
    assert workspace.cas_fetch_text(completed[0].handle) == "background evidence\n"

    loop._drain_background_completions(history)
    assert len([message for message in history if "后台任务完成通知" in str(message.get("content"))]) == 1
    await background.stop_all()


async def test_goal_waits_for_background_instead_of_polling_model(tmp_path):
    workspace = Workspace(base=tmp_path, run_id="background-wait")
    background = BackgroundRegistry()

    async def runner(on_output):
        await asyncio.sleep(0.25)
        return ExecResult(exit_code=0, output="ready")

    task = background.start("delayed-scan", runner)
    task.detached = True
    registry = ToolRegistry()
    ctx = ToolContext(workspace, background, emit=lambda event: None, fetch_output=lambda h, o, l: "")
    loop = AgentLoop(
        provider=FakeProvider(script=[("text", "unused")]),
        registry=registry,
        orchestrator=ToolOrchestrator(registry, PermissionChain(mode="yolo"), ctx),
        emit=lambda event: None,
    )
    goal = GoalMode(loop, Budget(), steer=SteerQueue())

    started = time.monotonic()
    await goal._wait_for_background(BudgetState(Budget()))
    elapsed = time.monotonic() - started
    assert elapsed >= 0.2
    assert task.done.is_set() and background.has_completed_detached()
    assert loop.provider.calls == 0
    await background.stop_all()

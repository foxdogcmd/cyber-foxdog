"""Resume：从 rollout 无缝续跑上一任务。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from foxdog.interactive import ChatApp, CommandInput
from foxdog.llm.client import FakeProvider
from foxdog import runner as runner_mod
from foxdog.store.rollout import replay_history, replay_timeline, read_records, valid_run_id

FLAG = "flag{smoke_test_flag_123}"
READ_CMD = (
    "Get-Content -Raw -LiteralPath 'fake_target_flag.txt'"
    if sys.platform == "win32"
    else "cat fake_target_flag.txt"
)


def _submit(app: ChatApp, text: str) -> None:
    app.on_input_submitted(type("E", (), {"value": text, "input": app.query_one(CommandInput)})())


def _write_rollout(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "work").mkdir(parents=True, exist_ok=True)
    (path.parent / "work" / "resume_marker.txt").write_text("kept from old run\n", encoding="utf-8")
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "objective": "旧任务：读 flag", "run_id": "old1", "target": "http://t", "flag_format": r"flag\{.*\}"}) + "\n")
        f.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": "先侦察", "tool_calls": [{"id": "t1", "name": "shell_exec", "arguments": "{}"}]}}) + "\n")
        f.write(json.dumps({"type": "tool_result", "call_id": "t1", "name": "shell_exec", "content": "80/tcp open"}) + "\n")
        # worker 子历史应被跳过
        f.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": "worker 支线"}, "worker": "worker-2"}) + "\n")
        f.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": "继续分析"}}) + "\n")


def test_replay_history_skips_worker_and_keeps_adjacency(tmp_path):
    p = tmp_path / "rollout.jsonl"
    _write_rollout(p)
    h = replay_history(p)
    # 主 agent：assistant(tool_call) + tool_result + assistant，共 3 条；worker 支线跳过
    assert len(h) == 3
    assert h[0]["role"] == "assistant" and h[0].get("tool_calls")
    assert h[1]["role"] == "tool" and h[1]["tool_call_id"] == "t1"
    assert h[2]["role"] == "assistant" and h[2]["content"] == "继续分析"
    # meta 不混入
    assert all(m.get("type") != "session_meta" for m in h)


def test_replay_timeline_materializes_main_agent_tool_rows(tmp_path):
    p = tmp_path / "rollout.jsonl"
    _write_rollout(p)
    items = replay_timeline(p)
    assert [item.kind for item in items] == ["assistant", "tool_call", "tool_result", "assistant"]
    assert items[0].content == "先侦察"
    assert items[1].call_id == "t1" and items[1].name == "shell_exec"
    assert items[2].content == "80/tcp open"
    assert all("worker 支线" not in item.content for item in items)


def test_read_records_skips_meta(tmp_path):
    p = tmp_path / "rollout.jsonl"
    _write_rollout(p)
    recs = read_records(p)
    assert all(r.get("type") != "session_meta" for r in recs)
    assert any(r.get("type") == "tool_result" for r in recs)


def test_run_id_validation_rejects_path_components():
    assert valid_run_id("20260101-000000-old1")
    assert valid_run_id("old_1.2")
    assert not valid_run_id("../old")
    assert not valid_run_id("..")
    assert not valid_run_id("old/run")


async def test_resume_picker_opens_then_selects(tmp_path, monkeypatch):
    """续跑选择器：↑↓选择菜单，回车确认后重放历史继续解题，新 run 拿到 flag。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [("读 flag", [("tool", "shell_exec", {"command": READ_CMD}), ("tool", "submit_flag", {"flag": FLAG})])]
    real = runner_mod.build_session

    async def patched(**kw):
        s = await real(**kw)
        s.provider = FakeProvider(routes=routes, script=[("tool", "shell_exec", {"command": READ_CMD}), ("tool", "submit_flag", {"flag": FLAG})])
        return s

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    # 预置一个历史 run 的 rollout
    _write_rollout(tmp_path / ".foxdog-runs" / "20260101-000000-old1" / "rollout.jsonl")

    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._show_resume_picker()
        await pilot.pause(0.3)
        # codex/kimi-code 风格：↑↓ 选择菜单已弹出，唯一候选已高亮
        assert app._resume_picker_active
        assert len(app._resume_candidates) == 1 and app._resume_candidates[0].run_id == "20260101-000000-old1"
        app._resume_picker_select()  # 相当于回车确认
        await pilot.pause(0.2)
        assert not app._resume_picker_active  # 选中后菜单收起
        tl = _timeline_text(app)
        assert "续跑历史 20260101-000000-old1" in tl
        assert "先侦察" in tl and "80/tcp open" in tl
        assert "worker 支线" not in tl
        # 等任务起来并跑完
        deadline = time.monotonic() + 30
        while (app.busy or app._solve_task is None or not app._solve_task.done()) and time.monotonic() < deadline:
            await pilot.pause(0.2)
        # 历史被重放（继续解题前 history 非空且含旧 assistant）
        assert app.opt_target == "http://t"  # meta 恢复了靶机
        assert app.last_run is not None
        assert app.last_run.gate.solved
        assert (app.last_run.ws.work_dir / "resume_marker.txt").read_text(encoding="utf-8") == "kept from old run\n"
        meta = json.loads((app.last_run.ws.dir / "rollout.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert meta["source_run_id"] == "20260101-000000-old1"
        assert meta["resume_from"].endswith("20260101-000000-old1")
        assert Path(meta["work_dir"]) == app.last_run.ws.work_dir
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_resume_picker_escape_cancels(tmp_path, monkeypatch):
    """Esc 取消 resume 选择菜单：不开始任何任务。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    _write_rollout(tmp_path / ".foxdog-runs" / "20260101-000000-old1" / "rollout.jsonl")
    app = ChatApp(workspace=tmp_path, overrides=["runtime.kind=local"], config_path=tmp_path / "c.toml")
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._show_resume_picker()
        await pilot.pause(0.3)
        assert app._resume_picker_active
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not app._resume_picker_active
        assert not app.busy
        _submit(app, "/quit")
        await pilot.pause(0.3)


def _timeline_text(app: ChatApp) -> str:
    from rich.text import Text
    parts = []
    for w in app.query("#timeline Static"):
        c = w.render()
        parts.append(c.plain if isinstance(c, Text) else str(c))
    return "\n".join(parts)


async def test_resume_tui_picker_lists_runs(tmp_path, monkeypatch):
    """foxdog resume 无 run_id：TUI 打开后弹出 ↑↓ 选择菜单（不自动续，等用户选）。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    _write_rollout(tmp_path / ".foxdog-runs" / "20260101-000000-old1" / "rollout.jsonl")
    app = ChatApp(
        workspace=tmp_path, overrides=["runtime.kind=local"],
        config_path=tmp_path / "c.toml", resume_run_id="",  # 空串 = 选择器模式
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.6)
        assert app._resume_picker_active
        assert len(app._resume_candidates) == 1
        assert app._resume_candidates[0].run_id == "20260101-000000-old1"
        tl = _timeline_text(app)
        assert "续跑哪个任务" in tl
        # 选择器模式不自动开始任务
        assert not app.busy
        _submit(app, "/quit")
        await pilot.pause(0.3)


async def test_resume_tui_specific_id_autoresumes(tmp_path, monkeypatch):
    """foxdog resume <id>：TUI 打开后自动续该 run（恢复 target）。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    routes = [("读 flag", [("tool", "shell_exec", {"command": READ_CMD}), ("tool", "submit_flag", {"flag": FLAG})])]
    real = runner_mod.build_session

    async def patched(**kw):
        s = await real(**kw)
        s.provider = FakeProvider(routes=routes, script=[("tool", "shell_exec", {"command": READ_CMD}), ("tool", "submit_flag", {"flag": FLAG})])
        return s

    monkeypatch.setattr("foxdog.interactive.build_session", patched)
    _write_rollout(tmp_path / ".foxdog-runs" / "20260101-000000-old1" / "rollout.jsonl")
    app = ChatApp(
        workspace=tmp_path, overrides=["runtime.kind=local"],
        config_path=tmp_path / "c.toml", resume_run_id="20260101-000000-old1",
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        deadline = time.monotonic() + 30
        while (app.busy or app._solve_task is None or not app._solve_task.done()) and time.monotonic() < deadline:
            await pilot.pause(0.2)
        assert app.opt_target == "http://t"  # 自动续了指定 run，恢复靶机
        assert app.last_run is not None
        assert app.last_run.gate.solved
        assert (app.last_run.ws.work_dir / "resume_marker.txt").is_file()
        _submit(app, "/quit")
        await pilot.pause(0.3)

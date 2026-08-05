"""Run assembly uses one selected runtime for the main loop and every worker."""
from __future__ import annotations

from foxdog import runner as runner_mod
from foxdog.executor.process import ExecResult


class FakeDockerRuntime:
    kind = "docker"
    workdir = "/workspace/work"

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.shell_calls: list[str] = []
        self.argv_calls: list[tuple[list[str], dict]] = []
        self.which_calls: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def which(self, executable: str) -> str | None:
        self.which_calls.append(executable)
        if executable in {"python", "python3", "bash", "nmap", "claude"}:
            return f"/usr/bin/{executable}"
        return None

    async def run_shell(self, command: str, **kwargs) -> ExecResult:
        self.shell_calls.append(command)
        output = (
            "congrads! flag{smoke_test_flag_123}\n"
            if "fake_target_flag.txt" in command
            else "ok\n"
        )
        callback = kwargs.get("on_output")
        if callback:
            callback(output)
        return ExecResult(exit_code=0, output=output)

    async def run_argv(self, argv, **kwargs) -> ExecResult:
        self.argv_calls.append((list(argv), kwargs))
        if argv and str(argv[0]).endswith("claude") and kwargs.get("on_output"):
            kwargs["on_output"]("FOUND_FLAG=flag{smoke_test_flag_123}\n")
        return ExecResult(exit_code=0, output="ok\n")


async def test_build_run_routes_main_and_worker_tools_through_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    runtime = FakeDockerRuntime()
    monkeypatch.setattr(runner_mod, "create_runtime", lambda *args, **kwargs: runtime)

    run = await runner_mod.build_run(
        "runtime smoke",
        None,
        r"flag\{[^{}]*\}",
        [],
        "",
        None,
        None,
        None,
        3,
        None,
        ["runtime.kind=docker"],
        emit=lambda _event: None,
        workspace=tmp_path,
        engines_enabled=False,
    )

    assert run.runtime is runtime and run.ctx.runtime is runtime
    assert run.coordinator is not None
    assert run.coordinator.race_engines == []
    worker_loop = run.coordinator.factory.make_loop("runtime-worker")
    assert worker_loop.orchestrator.ctx.runtime is runtime
    assert "Docker Linux" in str(run.history[0]["content"])
    assert "nmap_scan" in run.registry.get("select_tools").manager.recipes

    code = await runner_mod.execute_run(run)

    assert code == 0
    assert runtime.started and runtime.closed
    assert any("fake_target_flag.txt" in command for command in runtime.shell_calls)


async def test_build_run_wires_spectator_in_swarm_mode(tmp_path, monkeypatch):
    """swarm 模式默认接线旁观者（无专用端点则复用 planner 模型）。"""
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    runtime = FakeDockerRuntime()
    monkeypatch.setattr(runner_mod, "create_runtime", lambda *args, **kwargs: runtime)

    run = await runner_mod.build_run(
        "spectator wiring",
        None,
        r"flag\{[^{}]*\}",
        [],
        "",
        None,
        None,
        None,
        3,
        None,
        ["runtime.kind=docker"],
        emit=lambda _event: None,
        workspace=tmp_path,
        engines_enabled=False,
    )
    assert run.coordinator is not None
    assert run.coordinator.spectator is not None, "swarm 模式应实例化 spectator"
    from foxdog.swarm.spectator import Spectator
    assert isinstance(run.coordinator.spectator, Spectator)
    await runner_mod.execute_run(run)


async def test_build_run_does_not_probe_claude_until_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    runtime = FakeDockerRuntime()
    monkeypatch.setattr(runner_mod, "create_runtime", lambda *args, **kwargs: runtime)

    run = await runner_mod.build_run(
        "runtime without claude",
        None,
        r"flag\{[^{}]*\}",
        [],
        "",
        None,
        None,
        None,
        3,
        None,
        ["runtime.kind=docker", "engines.enabled=false"],
        emit=lambda _event: None,
        workspace=tmp_path,
    )

    assert run.coordinator is not None
    assert run.coordinator.race_engines == []
    assert "claude" not in runtime.which_calls
    await runner_mod.execute_run(run)


async def test_build_run_routes_claude_and_host_config_through_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXDOG_PROVIDER", "fake")
    runtime = FakeDockerRuntime()
    events = []
    monkeypatch.setattr(runner_mod, "create_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(
        "foxdog.swarm.claude_config.load_claude_code_env",
        lambda: {
            "ANTHROPIC_AUTH_TOKEN": "runtime-secret",
            "ANTHROPIC_BASE_URL": "https://gateway.example",
        },
    )

    run = await runner_mod.build_run(
        "runtime claude smoke",
        None,
        r"flag\{[^{}]*\}",
        [],
        "",
        None,
        None,
        None,
        3,
        None,
        [
            "runtime.kind=docker",
            "engines.claude_code.enabled=true",
            "engines.claude_code.import_host_config=true",
            "model.extra={}",
        ],
        emit=events.append,
        workspace=tmp_path,
    )

    assert run.coordinator is not None
    assert "claude" in runtime.which_calls
    assert [name for name, _fn, _goal in run.coordinator.race_engines] == ["engine-claude"]
    assert run.engine_labels["engine-claude"] == "Claude Code"
    code = await runner_mod.execute_run(run)

    assert code == 0
    claude_calls = [(argv, kwargs) for argv, kwargs in runtime.argv_calls if argv and argv[0].endswith("claude")]
    assert len(claude_calls) == 1
    argv, kwargs = claude_calls[0]
    assert "--verbose" in argv
    assert "--safe-mode" in argv
    assert "--no-session-persistence" in argv
    assert "--dangerously-skip-permissions" in argv
    assert kwargs["env"] == {
        "ANTHROPIC_AUTH_TOKEN": "runtime-secret",
        "ANTHROPIC_BASE_URL": "https://gateway.example",
    }
    rendered_events = "\n".join(str(getattr(event, "message", "")) for event in events)
    assert "ANTHROPIC_AUTH_TOKEN" in rendered_events
    assert "runtime-secret" not in rendered_events

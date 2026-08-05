"""Execution runtime lifecycle and Docker argv construction."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from foxdog.executor import runtime as runtime_mod
from foxdog.executor.process import ExecResult
from foxdog.executor.runtime import (
    DockerRuntime,
    LocalRuntime,
    build_runtime_image,
    bundled_image_dockerfile,
    bundled_image_context,
    create_runtime,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    async def __call__(self, argv, **kwargs) -> ExecResult:
        command = list(argv)
        self.calls.append((command, kwargs))
        if command[-5:] == ["sh", "-c", 'command -v "$1"', "foxdog-which", "python3"]:
            return ExecResult(exit_code=0, output="/usr/bin/python3\n")
        return ExecResult(exit_code=0, output="container-id\n")


async def test_runtime_build_uses_full_dockerfile_for_latest_image():
    calls: list[list[str]] = []

    async def runner(argv, **_kwargs):
        calls.append(list(argv))
        return ExecResult(exit_code=0, output="built")

    await build_runtime_image(command_runner=runner)

    assert len(calls) == 1
    command = calls[0]
    assert command[:3] == ["docker", "build", "--progress=plain"]
    assert command[command.index("--file") + 1] == str(bundled_image_dockerfile())
    assert command[command.index("--tag") + 1] == "foxdog-runtime:full"


def test_bundled_runtime_guide_is_packaged():
    context = bundled_image_context()
    guide = context / "AGENTS.md"
    assert guide.is_file()
    dockerfile = bundled_image_dockerfile().read_text(encoding="utf-8")
    assert "COPY AGENTS.md /opt/foxdog/AGENTS.md" in dockerfile


async def test_local_runtime_delegates_to_existing_process_functions(tmp_path, monkeypatch):
    calls: list[tuple[str, object, dict]] = []

    async def fake_shell(command, **kwargs):
        calls.append(("shell", command, kwargs))
        return ExecResult(exit_code=0, output="shell")

    async def fake_argv(argv, **kwargs):
        calls.append(("argv", argv, kwargs))
        return ExecResult(exit_code=0, output="argv")

    monkeypatch.setattr(runtime_mod.process, "run_shell", fake_shell)
    monkeypatch.setattr(runtime_mod.process, "run_argv", fake_argv)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda name: f"/bin/{name}")

    runtime = LocalRuntime(tmp_path / "run")
    shell_result = await runtime.run_shell("echo hi", env={"A": "1"})
    argv_result = await runtime.run_argv(("python", "-V"), total_timeout=12)

    assert runtime.started
    assert runtime.workdir == (tmp_path / "run" / "work").resolve()
    assert runtime.workdir.is_dir()
    assert shell_result.output == "shell" and argv_result.output == "argv"
    assert calls[0][2]["cwd"] == runtime.workdir and calls[0][2]["env"] == {"A": "1"}
    assert calls[1][1] == ["python", "-V"] and calls[1][2]["total_timeout"] == 12
    assert await runtime.which("python") == "/bin/python"

    await runtime.close()
    await runtime.close()
    assert not runtime.started
    with pytest.raises(RuntimeError, match="already closed"):
        await runtime.run_shell("echo no")


async def test_docker_runtime_uses_one_container_and_exec_argv(tmp_path):
    runner = RecordingRunner()
    workspace = tmp_path / "run"
    runtime = DockerRuntime(
        workspace,
        image="registry/foxdog:pwn",
        docker_binary="podman",
        network="host",
        mounts=["./cache:/cache"],
        container_name="foxdog-test",
        command_runner=runner,
    )

    await runtime.start()
    await runtime.start()
    await runtime.run_shell("printf '%s' hello", env={"TOKEN": "a b"})
    await runtime.run_argv(["python3", "-c", "print(1)"])
    assert await runtime.which("python3") == "/usr/bin/python3"
    await runtime.close()
    await runtime.close()

    commands = [call[0] for call in runner.calls]
    starts = [command for command in commands if command[1] == "run"]
    execs = [command for command in commands if command[1] == "exec"]
    removals = [command for command in commands if command[1] == "rm"]
    assert len(starts) == 1 and len(removals) == 1

    start = starts[0]
    assert start[:3] == ["podman", "run", "--detach"]
    assert "--rm" in start and start[start.index("--name") + 1] == "foxdog-test"
    assert start[start.index("--network") + 1] == "host"
    assert start[start.index("--workdir") + 1] == "/workspace/work"
    bind = start[start.index("--mount") + 1]
    assert bind == f"type=bind,source={workspace.resolve()},target=/workspace"
    assert start[start.index("--volume") + 1] == "./cache:/cache"
    assert start[-4:] == ["--entrypoint", "sleep", "registry/foxdog:pwn", "infinity"]
    assert "--init" in start and "--tmpfs" in start

    shell = execs[0]
    assert shell[:5] == ["podman", "exec", "--workdir", "/workspace/work", "--env"]
    assert shell[5:8] == ["TOKEN=a b", "foxdog-test", "setsid"]
    assert "foxdog-exec" in shell
    assert shell[-3:] == ["sh", "-c", "printf '%s' hello"]
    assert execs[1][-3:] == ["python3", "-c", "print(1)"]
    assert removals[0] == ["podman", "rm", "--force", "foxdog-test"]


async def test_docker_runtime_maps_host_and_relative_workdirs(tmp_path):
    runner = RecordingRunner()
    workspace = (tmp_path / "run").resolve()
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True)
    runtime = DockerRuntime(workspace, container_name="foxdog-paths", command_runner=runner)

    await runtime.run_argv(["pwd"], cwd=inputs)
    await runtime.run_argv(["pwd"], cwd="scratch")
    await runtime.run_argv(["pwd"], cwd="/tmp")
    await runtime.close()

    execs = [command for command, _kwargs in runner.calls if command[1] == "exec"]
    assert [command[command.index("--workdir") + 1] for command in execs] == [
        "/workspace/inputs",
        "/workspace/work/scratch",
        "/tmp",
    ]
    with pytest.raises(ValueError, match="escapes"):
        runtime._container_cwd("../../../outside")


def test_docker_file_tools_lock_down_to_workspace(tmp_path):
    """docker 模式文件工具可见范围 == shell 可见范围：工作区之外读写都拒绝。"""
    workspace = (tmp_path / "run").resolve()
    (workspace / "work").mkdir(parents=True)
    runtime = DockerRuntime(workspace, container_name="foxdog-lock", command_runner=RecordingRunner())

    # 相对路径基于 work 目录；/workspace 路径映射回宿主挂载目录
    p, hint = runtime.resolve_fs_path("x.py", base_dir=workspace / "work", write=True)
    assert p == workspace / "work" / "x.py" and hint is None
    p, hint = runtime.resolve_fs_path("/workspace/inputs/c.txt", base_dir=workspace / "work", write=False)
    assert p == workspace / "inputs" / "c.txt" and hint is None

    # 宿主界外路径：读、写都拒绝（唯一入口 import_file；旧行为是读放行带提示）
    outside = str(tmp_path / "outside" / "chall.txt")
    for write in (False, True):
        with pytest.raises(PermissionError, match="import_file"):
            runtime.resolve_fs_path(outside, base_dir=workspace / "work", write=write)

    # /workspace 之外的容器内路径：拒绝并指向 shell_exec
    with pytest.raises(PermissionError, match="shell_exec"):
        runtime.resolve_fs_path("/tmp/payload", base_dir=workspace / "work", write=False)


async def test_docker_runner_cancellation_propagates(tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    killed = asyncio.Event()

    async def runner(argv, **_kwargs):
        if argv[1:3] == ["image", "inspect"]:
            return ExecResult(exit_code=0, output="image")
        if argv[1] == "run":
            return ExecResult(exit_code=0, output="id")
        if argv[1] == "rm":
            return ExecResult(exit_code=0, output="")
        if "foxdog-kill" in argv:
            killed.set()
            return ExecResult(exit_code=0, output="")
        assert "foxdog-exec" in argv
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runtime = DockerRuntime(tmp_path, container_name="foxdog-cancel", command_runner=runner)
    task = asyncio.create_task(runtime.run_shell("sleep 999"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and killed.is_set()
    await runtime.close()


async def test_docker_start_failure_and_cleanup_are_deterministic(tmp_path):
    calls: list[list[str]] = []

    async def runner(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["image", "inspect"]:
            return ExecResult(exit_code=0, output="image")
        if argv[1] == "run":
            return ExecResult(exit_code=125, output="image missing")
        return ExecResult(exit_code=0, output="removed")

    runtime = DockerRuntime(tmp_path, container_name="foxdog-fail", command_runner=runner)
    with pytest.raises(RuntimeError, match="image missing"):
        await runtime.start()
    await runtime.close()
    await runtime.close()
    assert [command[1] for command in calls].count("run") == 1
    assert [command[1] for command in calls].count("rm") >= 1


async def test_docker_runtime_auto_builds_missing_bundled_image(tmp_path):
    calls: list[list[str]] = []
    notes: list[str] = []

    async def runner(argv, **_kwargs):
        command = list(argv)
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return ExecResult(exit_code=1, output="missing")
        return ExecResult(exit_code=0, output="ok")

    runtime = DockerRuntime(
        tmp_path,
        container_name="foxdog-build",
        command_runner=runner,
        status_callback=notes.append,
    )
    await runtime.start()
    await runtime.close()

    assert any(command[1] == "build" for command in calls)
    assert any("首次构建" in note for note in notes)
    assert any("构建完成" in note for note in notes)


def test_runtime_factory_selects_backend(tmp_path):
    assert isinstance(create_runtime("local", workspace=tmp_path), LocalRuntime)
    assert isinstance(create_runtime("DOCKER", workspace=tmp_path), DockerRuntime)
    with pytest.raises(ValueError, match="requires a workspace"):
        create_runtime("docker")
    with pytest.raises(ValueError, match="unsupported"):
        create_runtime("remote", workspace=tmp_path)


def test_kill_wrapper_uses_procps_not_dash_kill():
    """dash 的 kill 内建不支持 "-- -pgid"（Illegal number），"kill -TERM -pgid"
    静默退化成只杀 leader——旧实现只杀 wrapper sh，长命令残留（2026-08-03 smoke）。
    锁定：杀进程组必须走 procps 的 pkill/pgrep -g。"""
    assert "pkill -TERM -g" in runtime_mod._KILL_WRAPPER
    assert "pkill -KILL -g" in runtime_mod._KILL_WRAPPER
    assert "pgrep -g" in runtime_mod._KILL_WRAPPER
    assert '"-$pid"' not in runtime_mod._KILL_WRAPPER  # dash kill -- -pgid 语法禁止回归


@pytest.mark.skipif(
    os.environ.get("FOXDOG_TEST_DOCKER") != "1",
    reason="真实 docker 集成测试（FOXDOG_TEST_DOCKER=1 且本机有 foxdog-runtime 镜像时运行）",
)
async def test_docker_cancel_leaves_no_orphan_processes(tmp_path):
    """真容器回归：取消长命令后，容器内不得残留目标进程（含进程组子进程）。"""
    from foxdog.executor.runtime import DockerRuntime

    marker = f"foxdog_cancel_marker_{uuid.uuid4().hex[:8]}"
    runtime = DockerRuntime(tmp_path / "run", container_name=f"foxdog-test-cancel-{uuid.uuid4().hex[:6]}")
    await runtime.start()
    try:
        task = asyncio.create_task(
            runtime.run_shell(f"python3 -c \"import time; print('{marker}'); time.sleep(999)\"")
        )
        await asyncio.sleep(3)  # 等命令确实跑起来
        probe = await runtime.run_shell(f"pgrep -af '{marker}' || true")
        assert marker in probe.output, f"长命令未启动: {probe.output}"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.5)  # 给 _terminate_exec 收尾
        probe2 = await runtime.run_shell(f"pgrep -af '[f]oxdog_cancel_marker' || true")
        assert probe2.output.strip() == "", f"取消后容器内仍有残留进程: {probe2.output}"
    finally:
        await runtime.close()

"""持久 Kali 模式（DockerPersistentRuntime）：复用不重建、close 不停容器、per-run cwd、reset 流程。"""
from __future__ import annotations

from foxdog.executor.runtime import (
    CONTAINER_WORKSPACE,
    DockerPersistentRuntime,
    create_runtime,
)
from foxdog.executor.process import ExecResult


class PersistentRunner:
    """模拟 docker CLI：跟踪一个 foxdog-kali 容器的状态机。"""

    def __init__(self, state: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.state = state  # None=不存在 / running / exited

    async def __call__(self, argv, **kwargs) -> ExecResult:
        command = list(argv)
        self.calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return ExecResult(exit_code=0, output="image")
        if command[1:2] == ["ps"]:
            return ExecResult(exit_code=0, output=(self.state or "") + "\n")
        if command[1] == "run":
            self.state = "running"
            return ExecResult(exit_code=0, output="container-id\n")
        if command[1] == "start":
            self.state = "running"
            return ExecResult(exit_code=0, output="foxdog-kali\n")
        if command[1] == "stop":
            self.state = "exited"
            return ExecResult(exit_code=0, output="")
        if command[1] == "rm":
            self.state = None
            return ExecResult(exit_code=0, output="")
        return ExecResult(exit_code=0, output="ok")


async def test_start_reuses_running_container_without_recreate(tmp_path):
    runner = PersistentRunner(state="running")
    runtime = DockerPersistentRuntime(tmp_path / "run-x", command_runner=runner)
    await runtime.start()
    assert runtime.started
    # 已 running：没有 docker run / docker start，不重建
    verbs = [c[1] for c in runner.calls]
    assert "run" not in verbs and "start" not in verbs and "rm" not in verbs
    # 挂载的是 runs 根目录（run 目录父级）
    assert runtime.workspace == (tmp_path).resolve()
    assert runtime.workdir == f"{CONTAINER_WORKSPACE}/run-x/work"


async def test_start_starts_stopped_container(tmp_path):
    runner = PersistentRunner(state="exited")
    runtime = DockerPersistentRuntime(tmp_path / "run-x", command_runner=runner)
    await runtime.start()
    verbs = [c[1] for c in runner.calls]
    assert "start" in verbs and "run" not in verbs


async def test_start_creates_when_missing(tmp_path):
    runner = PersistentRunner(state=None)
    runtime = DockerPersistentRuntime(tmp_path / "run-x", command_runner=runner)
    await runtime.start()
    runs = [c for c in runner.calls if c[1] == "run"]
    assert len(runs) == 1
    start = runs[0]
    assert "--rm" not in start  # 常驻不加 --rm
    assert start[start.index("--name") + 1] == "foxdog-kali"
    assert "foxdog.kali=true" in start
    bind = start[start.index("--mount") + 1]
    assert bind == f"type=bind,source={tmp_path.resolve()},target={CONTAINER_WORKSPACE}"


async def test_close_does_not_stop_container(tmp_path):
    runner = PersistentRunner(state="running")
    runtime = DockerPersistentRuntime(tmp_path / "run-x", command_runner=runner)
    await runtime.start()
    await runtime.close()
    verbs = [c[1] for c in runner.calls]
    assert "stop" not in verbs and "rm" not in verbs
    assert runner.state == "running"  # 容器继续跑


async def test_per_run_workdir_mapping(tmp_path):
    runner = PersistentRunner(state="running")
    runtime = DockerPersistentRuntime(tmp_path / "run-42", command_runner=runner)
    # 宿主 run 目录内路径 → 容器 /workspace/<run-id>/...
    mapped = runtime._container_cwd(tmp_path / "run-42" / "work")
    assert mapped == f"{CONTAINER_WORKSPACE}/run-42/work"
    mapped_inputs = runtime._container_cwd(tmp_path / "run-42" / "inputs")
    assert mapped_inputs == f"{CONTAINER_WORKSPACE}/run-42/inputs"


def test_factory_selects_persistent_backend(tmp_path):
    runtime = create_runtime("docker-persistent", workspace=tmp_path / "run")
    assert isinstance(runtime, DockerPersistentRuntime)
    assert runtime.kind == "docker-persistent"


async def test_reset_flow_rm_then_run(tmp_path):
    """reset 语义：rm -f 后重新 run（kali reset 命令的底层动作）。"""
    runner = PersistentRunner(state="running")
    runtime = DockerPersistentRuntime(tmp_path / "run-x", command_runner=runner)
    # 模拟 reset：先 rm --force，再 start（走创建路径）
    await runtime._run_cli([runtime.docker_binary, "rm", "--force", runtime.container_name])
    await runtime.start()
    verbs = [c[1] for c in runner.calls]
    assert "rm" in verbs and "run" in verbs
    assert verbs.index("rm") < verbs.index("run")

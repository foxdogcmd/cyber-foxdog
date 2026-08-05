"""执行面统一：runtime.spawn（local/docker argv）、python_eval 容器分支、import_file 两端策略。"""
from __future__ import annotations

import sys
from pathlib import Path

from foxdog.executor.process import ExecResult
from foxdog.executor.runtime import DockerRuntime, LocalRuntime
from foxdog.store.workspace import Workspace
from foxdog.tools import ToolContext
from foxdog.executor.background import BackgroundRegistry
from foxdog.tools.builtin.files import ReadFileTool, WriteFileTool
from foxdog.tools.builtin.import_file import ImportFileTool
from foxdog.tools.builtin.python_repl import PythonEvalTool, _SpawnRepl
from foxdog.tools.builtin.shell import ShellExecTool


def _ctx(ws: Workspace, runtime=None) -> ToolContext:
    return ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
        runtime=runtime,
    )


# ------------------------------------------------------------ import_file
async def test_import_file_copies_file(tmp_path):
    src_dir = tmp_path / "Downloads"
    src_dir.mkdir()
    attachment = src_dir / "chall.zip"
    attachment.write_bytes(b"PK fake zip content")
    ws = Workspace(base=tmp_path, run_id="import-test")
    ctx = _ctx(ws)

    res = await ImportFileTool().run({"path": str(attachment)}, ctx)
    assert res.ok, res.error
    assert (ws.inputs_dir / "chall.zip").read_bytes() == b"PK fake zip content"
    assert res.data["relative"] == "inputs/chall.zip"
    assert res.data["exec_path"] == str(ws.inputs_dir / "chall.zip")  # local 宿主路径
    assert "别猜两端路径" not in res.output  # 输出是给模型的路径，不含纪律文案


async def test_import_file_copies_directory(tmp_path):
    src = tmp_path / "chall_dir"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A", encoding="utf-8")
    (src / "sub" / "b.txt").write_text("B", encoding="utf-8")
    ws = Workspace(base=tmp_path, run_id="import-dir")
    ctx = _ctx(ws)

    res = await ImportFileTool().run({"path": str(src)}, ctx)
    assert res.ok, res.error
    assert (ws.inputs_dir / "chall_dir" / "a.txt").read_text() == "A"
    assert (ws.inputs_dir / "chall_dir" / "sub" / "b.txt").read_text() == "B"  # 结构保持
    assert "目录" in res.output


async def test_import_file_missing_path(tmp_path):
    ws = Workspace(base=tmp_path, run_id="import-missing")
    ctx = _ctx(ws)
    res = await ImportFileTool().run({"path": str(tmp_path / "ghost.bin")}, ctx)
    assert not res.ok and "不存在" in res.error


async def test_import_file_container_path_mapping(tmp_path):
    """docker 模式：返回值里给容器 /workspace 路径。"""
    src = tmp_path / "note.txt"
    src.write_text("x", encoding="utf-8")

    async def runner(argv, **_kw):
        return ExecResult(exit_code=0, output="ok")

    ws = Workspace(base=tmp_path, run_id="run-c")
    # runtime 的 workspace 必须与 ws.dir 一致（_container_cwd 只映射该根下路径）
    runtime = DockerRuntime(ws.dir, container_name="foxdog-import", command_runner=runner)
    ctx = _ctx(ws, runtime)

    res = await ImportFileTool().run({"path": str(src)}, ctx)
    assert res.ok, res.error
    assert res.data["exec_path"] == "/workspace/inputs/note.txt"
    assert "容器内绝对路径" in res.output


# ------------------------------------------------------------ runtime.spawn
async def test_local_spawn_runs_subprocess(tmp_path):
    runtime = LocalRuntime(tmp_path / "run")
    proc = await runtime.spawn(
        [sys.executable, "-u", "-c", "import sys; data=sys.stdin.readline(); print('echo:'+data.strip())"],
        cwd=tmp_path,
    )
    proc.stdin.write(b"hello-spawn\n")
    await proc.stdin.drain()
    line = await proc.stdout.readline()
    assert b"echo:hello-spawn" in line
    proc.kill()
    await proc.wait()


async def test_docker_spawn_wraps_exec_i(tmp_path):
    calls: list[list[str]] = []

    async def runner(argv, **_kw):
        return ExecResult(exit_code=0, output="ok")

    runtime = DockerRuntime(tmp_path / "run", container_name="foxdog-spawn", command_runner=runner)
    # 不真起子进程（宿主机上没 docker 也得能测）：替换 create_subprocess_exec
    import asyncio as _a

    captured: list[list[str]] = []
    real = _a.create_subprocess_exec

    class _FakeProc:
        pass

    async def fake_exec(*argv, **_kw):
        captured.append(list(argv))
        return _FakeProc()

    _a.create_subprocess_exec = fake_exec
    try:
        await runtime.spawn(["python3", "-u", "-i", "-q"], cwd="/workspace/work")
    finally:
        _a.create_subprocess_exec = real
    assert captured, "spawn 未走 create_subprocess_exec"
    argv = captured[0]
    assert argv[0] == "docker" and argv[1] == "exec" and argv[2] == "-i"
    assert "foxdog-spawn" in argv
    assert argv[-4:] == ["python3", "-u", "-i", "-q"]


# ------------------------------------------------------------ python_eval 容器分支
async def test_python_eval_docker_uses_spawn_repl(tmp_path):
    """docker 模式 python_eval 经 runtime.spawn 起容器内 python3，sentinel 同步输出。"""
    writes: list[bytes] = []
    lines: list[bytes] = []

    class FakeProc:
        returncode = None
        stdin = None
        stdout = None

    class FakeStdin:
        async def drain(self):
            pass

        def write(self, data):
            writes.append(data)

    class FakeStdout:
        async def readline(self):
            await asyncio_sleep0()
            return lines.pop(0) if lines else b""

    import asyncio

    async def asyncio_sleep0():
        await asyncio.sleep(0)

    class FakeRepl:
        def __init__(self, runtime, cwd=None):
            self.calls: list[str] = []

        async def run(self, src, timeout=180.0):
            self.calls.append(src)
            if "find_spec" in src:
                return "pwn|z3"
            return "42"

    async def runner(argv, **_kw):
        return ExecResult(exit_code=0, output="ok")

    runtime = DockerRuntime(tmp_path / "run", container_name="foxdog-repl", command_runner=runner)
    ws = Workspace(base=tmp_path, run_id="repl-test")
    ctx = _ctx(ws, runtime)

    tool = PythonEvalTool()
    tool._repl = FakeRepl(runtime)  # 注入替身 REPL（验证分流，不真起容器进程）
    res = await tool.run({"code": "print(6*7)"}, ctx)
    assert res.ok, res.error
    assert "42" in res.output
    assert "pwn" in res.output and "z3" in res.output  # 预装探测进了容器分支
    assert tool._repl.calls[-1] == "print(6*7)"


async def test_python_eval_local_unchanged(tmp_path):
    """local 回归：进程内 REPL，变量跨调用保留。"""
    ws = Workspace(base=tmp_path, run_id="repl-local")
    ctx = _ctx(ws, LocalRuntime(tmp_path / "run"))
    tool = PythonEvalTool()
    res1 = await tool.run({"code": "x = 41"}, ctx)
    assert res1.ok
    res2 = await tool.run({"code": "print(x + 1)"}, ctx)
    assert res2.ok and "42" in res2.output


async def test_worker_context_scopes_shell_files_and_python(tmp_path):
    ws = Workspace(base=tmp_path, run_id="worker-scope")
    worker_dir = ws.work_dir / ".workers" / "worker-1"

    class RecordingRuntime(LocalRuntime):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.shell_cwds: list[Path] = []

        async def run_shell(self, command: str, **kwargs) -> ExecResult:
            self.shell_cwds.append(Path(kwargs["cwd"]).resolve())
            return ExecResult(exit_code=0, output="ok")

    runtime = RecordingRuntime(ws.dir)
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
        runtime=runtime,
        work_dir=worker_dir,
        worker_id="worker-1",
    )

    res = await ShellExecTool(promote_after=1).run({"command": "pwd"}, ctx)
    assert res.ok, res.error
    assert runtime.shell_cwds == [worker_dir.resolve()]

    res = await WriteFileTool().run({"path": "note.txt", "content": "private"}, ctx)
    assert res.ok, res.error
    assert (worker_dir / "note.txt").read_text(encoding="utf-8") == "private"
    res = await ReadFileTool().run({"path": "note.txt"}, ctx)
    assert res.ok and "private" in res.output

    res = await WriteFileTool().run({"path": "../../shared.txt", "content": "shared"}, ctx)
    assert res.ok, res.error
    assert (ws.work_dir / "shared.txt").read_text(encoding="utf-8") == "shared"

    tool = PythonEvalTool()
    res = await tool.run({"code": "x = 41"}, ctx)
    assert res.ok, res.error
    other = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
        runtime=runtime,
        facts=ctx.facts,
        work_dir=ws.work_dir / ".workers" / "worker-2",
        worker_id="worker-2",
    )
    res = await tool.run({"code": "print('x' in globals())"}, other)
    assert res.ok and "False" in res.output
    res = await tool.run({"code": "print(x + 1)"}, ctx)
    assert res.ok and "42" in res.output
    ctx.facts.close()


def test_worker_context_docker_env_uses_private_tmp(tmp_path):
    ws = Workspace(base=tmp_path, run_id="worker-docker-env")

    async def runner(argv, **_kw):
        return ExecResult(exit_code=0, output="ok")

    runtime = DockerRuntime(ws.dir, container_name="foxdog-env", command_runner=runner)
    ctx = ToolContext(
        workspace=ws,
        background=BackgroundRegistry(),
        emit=lambda e: None,
        fetch_output=lambda h, o, l: "",
        runtime=runtime,
        work_dir=ws.work_dir / ".workers" / "worker-1",
        worker_id="worker-1",
    )

    env = ctx.execution_env()
    assert env is not None
    assert env["FOXDOG_WORKER_ID"] == "worker-1"
    assert env["FOXDOG_CWD"] == "/workspace/work/.workers/worker-1"
    assert env["FOXDOG_SHARED_WORK"] == "/workspace/work"
    assert env["TMPDIR"] == "/workspace/work/.workers/worker-1/.tmp"
    assert env["TMP"] == env["TMPDIR"]
    ctx.facts.close()

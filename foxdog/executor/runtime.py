"""Execution runtime boundary for host and Docker-backed tools.

The agent stays on the host.  A runtime only owns command execution and its
lifecycle, so tools can switch between the current host process model and one
long-lived Linux container without knowing how either is implemented.
"""
from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from . import process
from .process import (
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_OUTPUT,
    DEFAULT_TOTAL_TIMEOUT,
    ExecResult,
)

CasStore = Callable[[bytes], str]
OutputCallback = Callable[[str], None]
StatusCallback = Callable[[str], None]
CommandRunner = Callable[..., Awaitable[ExecResult]]

CONTAINER_WORKSPACE = "/workspace"
DEFAULT_CONTAINER_WORKDIR = "/workspace/work"
DEFAULT_RUNTIME_IMAGE = "foxdog-runtime:full"

_EXEC_WRAPPER = """\
pidfile="$1"
shift
echo "$$" > "$pidfile"
cleanup() { rm -f "$pidfile"; }
trap cleanup EXIT HUP INT TERM
"$@"
"""

# 取消语义：杀死命令所在的整个容器内进程组。
# 注意：容器 /bin/sh 是 dash，其 kill 内建不支持 "-- -pgid"（报 Illegal number），
# "kill -TERM -pgid" 静默退化成只杀 leader——旧实现因此只杀死 wrapper sh，
# 真正的长命令（python3 sleep 999 等）全部残留（2026-08-03 smoke 翻车）。
# procps 的 pkill/pgrep -g 是唯一可靠路径（镜像已装 procps）。
_KILL_WRAPPER = """\
pid="$(cat "$1" 2>/dev/null || true)"
if [ -n "$pid" ]; then
    pkill -TERM -g "$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    n=0
    while pgrep -g "$pid" >/dev/null 2>&1 && [ "$n" -lt 20 ]; do
        sleep 0.05
        n=$((n + 1))
    done
    pkill -KILL -g "$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
fi
rm -f "$1"
"""


def bundled_image_context() -> Path:
    """Return the packaged build context for the default tool image."""
    return Path(__file__).resolve().parents[1] / "runtime_image"


def bundled_image_dockerfile() -> Path:
    """Return the single supported full-runtime Dockerfile."""
    return bundled_image_context() / "Dockerfile.full"


def _docker_context_locked(result: ExecResult) -> bool:
    detail = f"{result.error or ''}\n{result.output}".lower()
    return (
        "failed to read metadata" in detail
        and "being used by another process" in detail
    )


async def _run_docker_cli(
    runner: CommandRunner,
    argv: Sequence[str],
    **kwargs: Any,
) -> ExecResult:
    """Retry Docker Desktop's transient Windows context metadata lock."""
    result = await runner(argv, **kwargs)
    for delay in (0.5, 1.0, 2.0):
        if not _docker_context_locked(result):
            break
        await asyncio.sleep(delay)
        result = await runner(argv, **kwargs)
    return result


async def build_runtime_image(
    image: str = DEFAULT_RUNTIME_IMAGE,
    *,
    docker_binary: str = "docker",
    command_runner: CommandRunner | None = None,
    on_output: OutputCallback | None = None,
) -> ExecResult:
    """Build the bundled, credential-free CTF tool image."""
    context = bundled_image_context()
    dockerfile = bundled_image_dockerfile()
    if not dockerfile.is_file():
        raise RuntimeError(f"Docker runtime image context is missing: {dockerfile}")
    runner = command_runner or process.run_argv
    result = await _run_docker_cli(
        runner,
        [
            docker_binary,
            "build",
            "--progress=plain",
            "--file",
            str(dockerfile),
            "--tag",
            image,
            str(context),
        ],
        idle_timeout=600.0,
        total_timeout=3600.0,
        max_output=DEFAULT_MAX_OUTPUT,
        on_output=on_output,
    )
    if result.error is not None or result.exit_code != 0:
        detail = result.error or result.output.strip() or f"exit code {result.exit_code}"
        raise RuntimeError(f"failed to build Docker runtime image {image!r}: {detail}")
    return result


class ExecutionRuntime(ABC):
    """Common asynchronous lifecycle and command execution contract."""

    kind: str

    @property
    @abstractmethod
    def started(self) -> bool:
        """Whether the runtime is ready to execute commands."""

    @abstractmethod
    async def start(self) -> None:
        """Start the runtime. Repeated calls before close are harmless."""

    @abstractmethod
    async def close(self) -> None:
        """Release runtime resources. Implementations must be idempotent."""

    @abstractmethod
    async def run_shell(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        cas_store: CasStore | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        """Run one shell command in this runtime."""

    @abstractmethod
    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        cas_store: CasStore | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        """Run an argv vector without joining or shell quoting it."""

    @abstractmethod
    async def which(self, executable: str) -> str | None:
        """Resolve an executable inside this runtime."""

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """启动长生命周期子进程（stdin/stdout 管道，REPL/交互会话用）。

        local=宿主直接子进程；docker=`docker exec -i` 包装（容器内进程，
        stdin/stdout 经 docker client 管道透传）。默认不支持。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 spawn")

    def host_to_container(self, path: str | os.PathLike[str]) -> str:
        """宿主路径 → 执行环境路径（local 原样返回；docker 映射到 /workspace）。"""
        return str(path)

    def resolve_fs_path(
        self,
        raw: str,
        *,
        base_dir: str | os.PathLike[str],
        write: bool,
    ) -> tuple[Path, str | None]:
        """模型给的路径 → 文件工具可直接读写的宿主 Path + 可选提示文案。

        语义：文件工具与 shell 共用同一个文件系统视图。local 即宿主本身：
        相对路径基于 base_dir，绝对路径原样放行。docker 在子类覆写为
        /workspace ↔ 宿主挂载目录双向映射，界外宿主路径读写都拒绝（唯一入口 import_file）。
        """
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path(base_dir) / p
        return p.resolve(), None

    def display_path(self, host_path: str | os.PathLike[str]) -> str:
        """宿主路径 → 工具输出里展示给模型的路径（local 原样）。"""
        return str(host_path)

    async def __aenter__(self) -> "ExecutionRuntime":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


class LocalRuntime(ExecutionRuntime):
    """Host execution backed by :mod:`foxdog.executor.process`."""

    kind = "local"

    def __init__(self, workspace: str | os.PathLike[str] | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.workdir = self.workspace / "work" if self.workspace is not None else None
        self._started = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("runtime is already closed")
            if self._started:
                return
            if self.workdir is not None:
                self.workdir.mkdir(parents=True, exist_ok=True)
            self._started = True

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._started = False
            self._closed = True

    async def _prepare(self) -> None:
        if not self.started:
            await self.start()

    def _cwd(self, cwd: str | os.PathLike[str] | None) -> str | os.PathLike[str] | None:
        return cwd if cwd is not None else self.workdir

    async def run_shell(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        cas_store: CasStore | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        await self._prepare()
        return await process.run_shell(
            command,
            cwd=self._cwd(cwd),
            env=dict(env) if env is not None else None,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            max_output=max_output,
            cas_store=cas_store,
            on_output=on_output,
        )

    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        cas_store: CasStore | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must not be empty")
        await self._prepare()
        return await process.run_argv(
            list(argv),
            cwd=self._cwd(cwd),
            env=dict(env) if env is not None else None,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            max_output=max_output,
            cas_store=cas_store,
            on_output=on_output,
        )

    async def which(self, executable: str) -> str | None:
        await self._prepare()
        return shutil.which(executable)

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """宿主直接子进程（stdin/stdout 管道）。"""
        if not argv:
            raise ValueError("argv must not be empty")
        await self._prepare()
        kwargs: dict = {}
        if not process.IS_WINDOWS:
            kwargs["start_new_session"] = True  # 便于 killpg 杀整棵树
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd(cwd),
            env=dict(env) if env is not None else None,
            **kwargs,
        )


class DockerRuntime(ExecutionRuntime):
    """One long-lived Docker container used for all commands in a solve run."""

    kind = "docker"

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        image: str = DEFAULT_RUNTIME_IMAGE,
        docker_binary: str = "docker",
        network: str = "bridge",
        mounts: Sequence[str] = (),
        auto_build: bool = True,
        auto_remove: bool = True,
        workdir: str = DEFAULT_CONTAINER_WORKDIR,
        container_name: str | None = None,
        command_runner: CommandRunner | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        if not image.strip():
            raise ValueError("Docker runtime image must not be empty")
        if not docker_binary.strip():
            raise ValueError("docker_binary must not be empty")
        normalized_workdir = posixpath.normpath(workdir)
        if not normalized_workdir.startswith("/"):
            raise ValueError("Docker workdir must be an absolute container path")

        self.workspace = Path(workspace).resolve()
        self.image = image
        self.docker_binary = docker_binary
        self.network = network
        self.mounts = tuple(mounts)
        self.auto_build = auto_build
        self.auto_remove = auto_remove
        self.workdir = normalized_workdir
        self.container_name = container_name or f"foxdog-{uuid.uuid4().hex[:12]}"
        self._command_runner = command_runner or process.run_argv
        self._status_callback = status_callback
        self._started = False
        self._start_attempted = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    async def _run_cli(self, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        return await _run_docker_cli(self._command_runner, argv, **kwargs)

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    def _run_command(self) -> list[str]:
        argv = [self.docker_binary, "run", "--detach", "--init"]
        if self.auto_remove:
            argv.append("--rm")
        argv.extend([
            "--name", self.container_name,
            "--label", "foxdog.runtime=true",
            "--label", f"foxdog.workspace={self.workspace.name}",
        ])
        if self.network:
            argv.extend(["--network", self.network])
        argv.extend(
            [
                "--add-host",
                "host.docker.internal:host-gateway",
                # nmap 等需要原始套接字（无 NET_RAW 时 /usr/lib/nmap/nmap:
                # Operation not permitted，2026-08-03 连通性实测翻车）；
                # NET_ADMIN 供 -O/-sV 等高级扫描与 iptables 类操作
                "--cap-add",
                "NET_RAW",
                "--cap-add",
                "NET_ADMIN",
                "--workdir",
                self.workdir,
                "--env",
                "LANG=C.UTF-8",
                "--env",
                "LC_ALL=C.UTF-8",
                "--env",
                "PYTHONUNBUFFERED=1",
                "--tmpfs",
                "/tmp:rw,exec,size=2g",
                "--mount",
                f"type=bind,source={self.workspace},target={CONTAINER_WORKSPACE}",
            ]
        )
        for mount in self.mounts:
            argv.extend(["--volume", mount])
        argv.extend(["--entrypoint", "sleep", self.image, "infinity"])
        return argv

    def _status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    async def _image_available(self) -> bool:
        result = await self._run_cli(
            [self.docker_binary, "image", "inspect", self.image],
            idle_timeout=30.0,
            total_timeout=30.0,
            max_output=4096,
        )
        return result.error is None and result.exit_code == 0

    async def _discard_container(self) -> None:
        try:
            await self._run_cli(
                [self.docker_binary, "rm", "--force", self.container_name],
                idle_timeout=30.0,
                total_timeout=30.0,
                max_output=4096,
            )
        except (Exception, asyncio.CancelledError):
            pass

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("runtime is already closed")
            if self._started:
                return
            await self._start_fresh()

    async def _start_fresh(self) -> None:
        """完整创建流程（无锁；image 检查/构建/run）。仅供 start 与子类复用。"""
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "work").mkdir(parents=True, exist_ok=True)
        self._start_attempted = True
        try:
            if not await self._image_available():
                if not self.auto_build:
                    raise RuntimeError(
                        f"Docker runtime image {self.image!r} is unavailable; "
                        "run `foxdog runtime-build` or enable runtime.auto_build"
                    )
                self._status(f"Docker 工具镜像 {self.image} 不存在，开始首次构建")
                last_progress = time.monotonic()

                def build_progress(_chunk: str) -> None:
                    nonlocal last_progress
                    now = time.monotonic()
                    if now - last_progress >= 15.0:
                        self._status(f"Docker 工具镜像 {self.image} 仍在构建")
                        last_progress = now

                await build_runtime_image(
                    self.image,
                    docker_binary=self.docker_binary,
                    command_runner=self._command_runner,
                    on_output=build_progress,
                )
                self._status(f"Docker 工具镜像 {self.image} 构建完成")
            result = await self._run_cli(
                self._run_command(),
                idle_timeout=120.0,
                total_timeout=120.0,
            )
            if result.error is not None or result.exit_code != 0:
                detail = result.error or result.output.strip() or f"exit code {result.exit_code}"
                raise RuntimeError(f"failed to start Docker runtime: {detail}")
            self._started = True
        except BaseException:
            await self._discard_container()
            raise

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            if self._start_attempted:
                if self.auto_remove:
                    await self._discard_container()
                elif self._started:
                    try:
                        await self._run_cli(
                            [self.docker_binary, "stop", "--time", "2", self.container_name],
                            idle_timeout=30.0,
                            total_timeout=30.0,
                            max_output=4096,
                        )
                    except (Exception, asyncio.CancelledError):
                        pass
            self._started = False
            self._closed = True

    async def _prepare(self) -> None:
        if not self.started:
            await self.start()

    def _container_cwd(self, cwd: str | os.PathLike[str] | None) -> str:
        if cwd is None:
            return self.workdir

        raw = os.fspath(cwd)
        raw_posix = raw.replace("\\", "/")
        host_path = Path(raw)
        if host_path.is_absolute():
            try:
                relative = host_path.resolve().relative_to(self.workspace)
            except ValueError:
                # POSIX container paths and POSIX host paths are both rooted with
                # `/`. Only paths below the known host workspace are translated.
                # A Windows drive path, however, can never be a container path.
                if os.name == "nt":
                    raise ValueError(f"cwd is outside the mounted workspace: {host_path}") from None
            else:
                return str(PurePosixPath(CONTAINER_WORKSPACE, *relative.parts))

        if raw_posix.startswith("/"):
            return posixpath.normpath(raw_posix)

        target = posixpath.normpath(posixpath.join(self.workdir, raw_posix))
        workspace_prefix = CONTAINER_WORKSPACE + "/"
        if target != CONTAINER_WORKSPACE and not target.startswith(workspace_prefix):
            raise ValueError(f"relative cwd escapes the mounted workspace: {raw}")
        return target

    def _exec_command(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
    ) -> list[str]:
        command = [
            self.docker_binary,
            "exec",
            "--workdir",
            self._container_cwd(cwd),
        ]
        for key, value in (env or {}).items():
            command.extend(["--env", f"{key}={value}"])
        command.append(self.container_name)
        command.extend(argv)
        return command

    async def _terminate_exec(self, pidfile: str) -> None:
        """Best-effort kill of the command's whole in-container process group."""
        try:
            await asyncio.wait_for(
                self._run_cli(
                    self._exec_command(
                        ["sh", "-c", _KILL_WRAPPER, "foxdog-kill", pidfile],
                        cwd="/tmp",
                        env=None,
                    ),
                    idle_timeout=10.0,
                    total_timeout=10.0,
                    max_output=4096,
                ),
                timeout=15.0,
            )
        except (Exception, asyncio.CancelledError):
            pass

    async def _exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
        idle_timeout: float,
        total_timeout: float,
        max_output: int,
        cas_store: CasStore | None,
        on_output: OutputCallback | None,
        _pidfile: str | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must not be empty")
        await self._prepare()
        exec_id = uuid.uuid4().hex
        pidfile = _pidfile or f"/tmp/foxdog-exec-{exec_id}.pid"
        wrapped = [
            "setsid", "--wait", "sh", "-c", _EXEC_WRAPPER,
            "foxdog-exec", pidfile, *argv,
        ]
        try:
            result = await self._run_cli(
                self._exec_command(wrapped, cwd=cwd, env=env),
                idle_timeout=idle_timeout,
                total_timeout=total_timeout,
                max_output=max_output,
                cas_store=cas_store,
                on_output=on_output,
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._terminate_exec(pidfile))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            raise
        if result.timed_out or result.idle_timed_out:
            await self._terminate_exec(pidfile)
        return result

    async def run_shell(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        cas_store: CasStore | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        return await self._exec(
            ["sh", "-c", command],
            cwd=cwd,
            env=env,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            max_output=max_output,
            cas_store=cas_store,
            on_output=on_output,
        )

    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        cas_store: CasStore | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        return await self._exec(
            argv,
            cwd=cwd,
            env=env,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            max_output=max_output,
            cas_store=cas_store,
            on_output=on_output,
        )

    async def which(self, executable: str) -> str | None:
        if not executable:
            return None
        result = await self.run_argv(
            ["sh", "-c", 'command -v "$1"', "foxdog-which", executable],
            max_output=4096,
        )
        if result.error is not None or result.exit_code != 0:
            return None
        return next((line.strip() for line in result.output.splitlines() if line.strip()), None)

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """`docker exec -i` 包装：容器内长生命周期进程，stdio 经 client 管道透传。"""
        if not argv:
            raise ValueError("argv must not be empty")
        await self._prepare()
        exec_argv = self._exec_command(list(argv), cwd=cwd, env=env)
        exec_argv.insert(2, "-i")  # docker exec **-i**（stdin 管道）
        return await asyncio.create_subprocess_exec(
            *exec_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    def host_to_container(self, path: str | os.PathLike[str]) -> str:
        """宿主 run 目录内路径 → 容器 /workspace 路径（越界走 _container_cwd 的报错）。"""
        return self._container_cwd(path)

    # ------------------------------------------------------------ 文件工具路径语义
    def resolve_fs_path(
        self,
        raw: str,
        *,
        base_dir: str | os.PathLike[str],
        write: bool,
    ) -> tuple[Path, str | None]:
        """容器视图：/workspace ↔ 宿主挂载目录双向映射，文件工具与 shell 同源。

        - /workspace/... → 映射回宿主挂载目录内；越界（../）直接拒绝。
        - 相对路径 → 基于 base_dir（run work 目录，与容器内 shell cwd 对应）。
        - 挂载目录内的宿主绝对路径 → 原样（两端可见，展示时转 /workspace 形式）。
        - /workspace 之外的容器内路径（如 /tmp、/etc）→ 文件工具够不着，指引用 shell_exec。
        - 挂载目录外的宿主文件（Windows 桌面等）→ 读写都拒绝：容器看不见，唯一入口 import_file。
          （曾经读放行带警告，实战中模型无视提示直接拿内容喂容器命令，双重身份是困惑根源。）
        """
        raw_s = str(raw).strip()
        mount_root = self.workspace.resolve()
        if raw_s.replace("\\", "/").startswith("/"):
            norm = posixpath.normpath(raw_s.replace("\\", "/"))
            if norm == CONTAINER_WORKSPACE or norm.startswith(CONTAINER_WORKSPACE + "/"):
                rel = norm[len(CONTAINER_WORKSPACE):].lstrip("/")
                host = (mount_root / rel).resolve() if rel else mount_root
                if host != mount_root and mount_root not in host.parents:
                    raise PermissionError(f"容器路径越出挂载目录: {raw_s}")
                return host, None
            raise PermissionError(
                f"{raw_s} 是 /workspace 之外的容器内路径，文件工具只能读写挂载的工作区（/workspace）。"
                "请改用 shell_exec（cat/cp）查看或搬运容器内文件，或先 cp 进 /workspace 再 read_file。"
            )
        p = Path(raw_s).expanduser()
        host = (Path(base_dir) / p).resolve() if not p.is_absolute() else p.resolve()
        if host == mount_root or mount_root in host.parents:
            return host, None
        raise PermissionError(
            f"{raw_s} 在容器内不可见（shell 命令访问不到）——文件工具与 shell 共用同一文件系统视图，"
            "只能读写挂载的工作区（容器内即 /workspace）。要读宿主文件请先 import_file "
            "把它复制进工作区再用。"
        )

    def display_path(self, host_path: str | os.PathLike[str]) -> str:
        """宿主路径 → /workspace 形式展示（越界回退原样），与 shell 所见一致。"""
        try:
            return self.host_to_container(host_path)
        except ValueError:
            return str(host_path)


class DockerPersistentRuntime(DockerRuntime):
    """常驻全功能 Kali 容器：所有 solve run 共享同一个 `foxdog-kali`。

    与临时版（DockerRuntime，一 run 一容器、默认 --rm）的差别只在生命周期：
    - 容器命名固定 `foxdog-kali`（label `foxdog.kali=true`），不加 `--rm`，
      `auto_remove=False`；已存在且 running 直接复用，stopped 则 start，不存在才创建。
    - 绑定的是 **runs 根目录**（run 目录的父级 `.foxdog-runs/`）而非单个 run 目录；
      每个 run 的默认 cwd 是容器内 `/workspace/<run-id>/work`（天然映射宿主 run 目录）。
    - close() 只 pkill 收尾本 run 实例启动过的 exec（pidfile 跟踪），**不 stop 容器**。
    """

    kind = "docker-persistent"

    DEFAULT_NAME = "foxdog-kali"

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        image: str = DEFAULT_RUNTIME_IMAGE,
        docker_binary: str = "docker",
        network: str = "bridge",
        mounts: Sequence[str] = (),
        auto_build: bool = True,
        container_name: str | None = None,
        command_runner: CommandRunner | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        workspace_path = Path(workspace).resolve()
        # 挂载 runs 根目录：run 目录的父级（.foxdog-runs/）
        runs_root = workspace_path.parent
        super().__init__(
            runs_root,
            image=image,
            docker_binary=docker_binary,
            network=network,
            mounts=mounts,
            auto_build=auto_build,
            auto_remove=False,  # 常驻：不加 --rm，close 不删容器
            workdir=f"{CONTAINER_WORKSPACE}/{workspace_path.name}/work",
            container_name=container_name or self.DEFAULT_NAME,
            command_runner=command_runner,
            status_callback=status_callback,
        )
        self.run_workspace = workspace_path  # 宿主 run 目录（路径映射基准）
        #: 本 run 实例启动、尚未自然退出的 exec pidfile（close 时 pkill 收尾）
        self._open_pidfiles: set[str] = set()

    def _run_command(self) -> list[str]:
        argv = super()._run_command()
        # 常驻容器打专属 label（kali status/exec 定位用），不加 --rm。
        # 注意：父类 argv 末尾是 ["--entrypoint","sleep",image,"infinity"]，
        # label 必须插在它前面，否则会被当成容器命令参数。
        return argv[:-4] + ["--label", "foxdog.kali=true"] + argv[-4:]

    async def _container_state(self) -> str | None:
        """常驻容器当前状态：running / exited / 其他；不存在返回 None。"""
        result = await self._run_cli(
            [
                self.docker_binary, "ps", "-a",
                "--filter", f"name=^/{self.container_name}$",
                "--format", "{{.State}}",
            ],
            idle_timeout=30.0,
            total_timeout=30.0,
            max_output=4096,
        )
        if result.error is not None or result.exit_code != 0:
            return None
        state = result.output.strip().splitlines()
        return state[0].strip() if state and state[0].strip() else None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("runtime is already closed")
            if self._started:
                return
            self.run_workspace.mkdir(parents=True, exist_ok=True)
            (self.run_workspace / "work").mkdir(parents=True, exist_ok=True)
            self._start_attempted = True
            state = await self._container_state()
            if state == "running":
                self._started = True  # 复用已在跑的常驻容器，不重建
                return
            if state is not None:
                # 存在但停止：直接 start 复用（容器内历史状态保留）
                result = await self._run_cli(
                    [self.docker_binary, "start", self.container_name],
                    idle_timeout=60.0,
                    total_timeout=60.0,
                )
                if result.error is None and result.exit_code == 0:
                    self._started = True
                    return
            # 不存在：走完整创建流程（镜像缺失按 auto_build 构建）
            await self._start_fresh()

    async def _exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
        idle_timeout: float,
        total_timeout: float,
        max_output: int,
        cas_store: CasStore | None,
        on_output: OutputCallback | None,
    ) -> ExecResult:
        """执行并跟踪 pidfile：active 期间入集合，close() 时统一 pkill 收尾。"""
        pidfile = f"/tmp/foxdog-exec-{uuid.uuid4().hex}.pid"
        self._open_pidfiles.add(pidfile)
        try:
            return await super()._exec(
                argv,
                cwd=cwd,
                env=env,
                idle_timeout=idle_timeout,
                total_timeout=total_timeout,
                max_output=max_output,
                cas_store=cas_store,
                on_output=on_output,
                _pidfile=pidfile,
            )
        finally:
            # 父类各出口（正常退出/超时/取消）均已终结进程，不再欠收尾
            self._open_pidfiles.discard(pidfile)

    async def close(self) -> None:
        """只收尾本 run 的 exec（pkill 进程组），不 stop 常驻容器。"""
        async with self._lifecycle_lock:
            if self._closed:
                return
            for pidfile in sorted(self._open_pidfiles):
                await self._terminate_exec(pidfile)
            self._open_pidfiles.clear()
            self._started = False
            self._closed = True


def create_runtime(
    kind: str = "local",
    *,
    workspace: str | os.PathLike[str] | None = None,
    image: str = DEFAULT_RUNTIME_IMAGE,
    docker_binary: str = "docker",
    network: str = "bridge",
    mounts: Sequence[str] = (),
    auto_build: bool = True,
    auto_remove: bool = True,
    workdir: str = DEFAULT_CONTAINER_WORKDIR,
    container_name: str | None = None,
    command_runner: CommandRunner | None = None,
    status_callback: StatusCallback | None = None,
) -> ExecutionRuntime:
    """Build a runtime without starting it."""

    normalized = kind.strip().lower()
    if normalized == "local":
        return LocalRuntime(workspace=workspace)
    if normalized == "docker":
        if workspace is None:
            raise ValueError("Docker runtime requires a workspace")
        return DockerRuntime(
            workspace,
            image=image,
            docker_binary=docker_binary,
            network=network,
            mounts=mounts,
            auto_build=auto_build,
            auto_remove=auto_remove,
            workdir=workdir,
            container_name=container_name,
            command_runner=command_runner,
            status_callback=status_callback,
        )
    if normalized == "docker-persistent":
        if workspace is None:
            raise ValueError("Docker persistent runtime requires a workspace")
        return DockerPersistentRuntime(
            workspace,
            image=image,
            docker_binary=docker_binary,
            network=network,
            mounts=mounts,
            auto_build=auto_build,
            container_name=container_name,
            command_runner=command_runner,
            status_callback=status_callback,
        )
    raise ValueError(f"unsupported execution runtime: {kind!r}")


__all__ = [
    "CONTAINER_WORKSPACE",
    "DEFAULT_CONTAINER_WORKDIR",
    "DEFAULT_RUNTIME_IMAGE",
    "CommandRunner",
    "DockerPersistentRuntime",
    "DockerRuntime",
    "ExecutionRuntime",
    "LocalRuntime",
    "build_runtime_image",
    "bundled_image_context",
    "create_runtime",
]

"""运行环境事实探测（M1 教训：agent 在 Windows 上写脚本假设 POSIX，浪费 step 试错）。

启动时探测一次，把 OS、shell 类型、python 命令名、tmp 目录、已安装安全工具
注入系统提示；模型写脚本前先对齐这些事实，别再 `cd /tmp` / `python3`。
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from ..executor.process import windows_shell_executable

if TYPE_CHECKING:
    from ..executor.runtime import ExecutionRuntime

# 常见 CTF/安全工具探测清单（内置配方之外的常用件也列上，供 prompt 参考）
SECURITY_TOOLS = [
    "nmap", "ffuf", "gobuster", "dirsearch", "feroxbuster", "nikto", "sqlmap",
    "hydra", "john", "hashcat", "binwalk", "steghide", "exiftool", "zsteg",
    "checksec", "gdb", "radare2", "tshark", "volatility3", "vol",
    "enum4linux-ng", "nc", "ncat", "socat", "curl", "wget", "git", "docker",
    "whatweb", "wafw00f", "arjun", "wpscan", "nuclei", "katana", "dalfox",
    "gau", "waybackurls", "jadx", "apktool", "adb", "ROPgadget", "ropper",
    "one_gadget", "pwninit", "masscan", "ncrack", "proxychains4", "whois",
    "upx", "seccomp-tools", "ffmpeg", "sox", "convert", "magick", "gifsicle",
    "qemu-x86_64", "testdisk", "pcapfix", "qrencode", "sqlite3", "xxd",
    "hexedit", "apngdis", "multimon-ng", "stegsnow", "stegcracker",
    "zbarimg", "foremost", "pngcheck",
]


@dataclass
class EnvFacts:
    os: str  # Windows / Linux / Darwin
    is_windows: bool
    shell: str  # shell_exec 实际走的解释器
    python_cmd: str  # 可用的 python 命令（绝对路径）
    python3_on_path: bool
    tmp_dir: str
    bash_path: str | None = None  # Windows 上的 Git Bash 等（有则 bash/sh 脚本可用）
    tools: list[str] = field(default_factory=list)  # 已检测到的安全工具
    runtime_kind: str = "local"
    runtime_workdir: str | None = None


def probe_env() -> EnvFacts:
    is_windows = sys.platform == "win32"
    # Windows 上找可用的 bash（Git Bash 最常见）——有它 sh 脚本、/tmp、单引号都能用，
    # worker 不必为 POSIX 语法反复试错（swarm 实战：worker 试了 5 轮才发现 bash 可用）
    bash_path = shutil.which("bash")
    return EnvFacts(
        os=platform.system(),
        is_windows=is_windows,
        shell=(f"{windows_shell_executable()} -Command" if is_windows else "sh -c"),
        python_cmd=sys.executable,
        python3_on_path=shutil.which("python3") is not None,
        tmp_dir=tempfile.gettempdir(),
        bash_path=bash_path,
        tools=[t for t in SECURITY_TOOLS if shutil.which(t)],
        runtime_kind="local",
        runtime_workdir=None,
    )


async def probe_runtime_env(
    runtime: "ExecutionRuntime",
    tool_names: Iterable[str] | None = None,
) -> EnvFacts:
    """Probe the environment where shell/recipe commands actually execute."""
    names = list(dict.fromkeys(tool_names or SECURITY_TOOLS))
    if runtime.kind == "local":
        env = probe_env()
        resolved = await asyncio.gather(*(runtime.which(name) for name in names))
        env.tools = [name for name, path in zip(names, resolved) if path]
        env.runtime_workdir = str(runtime.workdir) if getattr(runtime, "workdir", None) else None
        return env

    probe_names = list(dict.fromkeys([*names, "python3", "python", "bash"]))
    raw_paths = await asyncio.gather(
        *(runtime.which(name) for name in probe_names),
        return_exceptions=True,
    )
    paths = {
        name: (path if isinstance(path, str) else None)
        for name, path in zip(probe_names, raw_paths)
    }
    python_cmd = paths.get("python3") or paths.get("python") or "python3"
    return EnvFacts(
        os="Linux",
        is_windows=False,
        shell="sh -c",
        python_cmd=python_cmd,
        python3_on_path=bool(paths.get("python3")),
        tmp_dir="/tmp",
        bash_path=paths.get("bash"),
        tools=[name for name in names if paths.get(name)],
        runtime_kind=runtime.kind,
        runtime_workdir=str(getattr(runtime, "workdir", "/workspace/work")),
    )


def render_env(env: EnvFacts) -> str:
    """渲染成系统提示里的环境事实块。"""
    lines: list[str] = []
    if env.runtime_kind != "local":
        lines.extend(
            [
                f"- Docker 工具运行手册：/opt/foxdog/AGENTS.md（容器内可直接读取）",
                f"- shell_exec、python_eval、shell_session 与外部工具配方都在 Docker Linux 容器里执行（{env.runtime_kind}，本 run 共享）",
                f"- run 目录 ↔ 容器 /workspace（同一批文件，bind mount）：文件工具与 shell 看见同一视图，容器内默认工作目录是 {env.runtime_workdir or '/workspace/work'}",
                "- 宿主桌面/下载/题目附件先用 import_file 复制进 inputs/，不要猜两端路径",
                "- 文件工具（read_file/write_file/edit_file）只准碰工作区；工作区外（宿主其他目录、容器内 /tmp 等）一律拒绝",
            ]
        )
    lines.extend([
        f"- OS：{env.os}（{'Windows；不要假设能力，以下项目均为本机实测' if env.is_windows else 'POSIX'}）",
        f"- shell_exec 实际解释器：{env.shell}",
        f"- python 命令：{env.python_cmd}（python3 在 PATH 上：{'是' if env.python3_on_path else '否'}）",
        f"- 临时目录：{env.tmp_dir}",
    ])
    if env.is_windows:
        lines.append(
            f"- Windows 脚本策略：计算、编码、payload、解析和文件处理优先 Python；短逻辑用 python_eval"
            f" 或 `{env.python_cmd} -c`，需要复用/调试/多步执行时再写 `.py`；PowerShell 主要用于短原生命令、"
            "目录查看和进程启动，避免长脚本与嵌套引号"
        )
        if env.bash_path:
            lines.append(
                f"- 检测到 bash：{env.bash_path}（Git Bash/MSYS。需要 POSIX 语法时用"
                f" `bash 脚本.sh` 或 bash -c，支持 /tmp、单引号、for 循环）"
            )
        else:
            lines.append("- 未检测到 bash：使用 PowerShell 语法，别用 POSIX shell 语法")
    lines.extend(
        [
            "- 协作纪律：facts 只放可验证事实；旁观者/操作员纠偏写 directive，不要污染事实板",
            "- 长扫描、监听器和卡住的命令先转后台；交互式 nc/gdb/ssh/ftp 用 shell_session",
            "- 先 which 再假设工具存在；大输出走 CAS handle，需要全文再 fetch_output",
        ]
    )
    lines.append(
        f"- 已安装安全工具：{', '.join(env.tools) if env.tools else '（未检测到，外部工具靠 select_tools 查配方）'}"
    )
    return "运行环境事实（写脚本/命令前先看这里）：\n" + "\n".join(lines)

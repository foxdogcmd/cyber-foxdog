"""typer 入口：foxdog solve / foxdog runs / foxdog mcp。

solve：默认 headless rich 渲染事件流；--json 每行一个 Event JSON；--tui 起 Textual 前端。
运行中可直接打字回车 steer 指挥（headless tty 下后台线程读 stdin）。
exit code：0=flag 验证通过，2=预算耗尽，3=异常。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

# Windows 控制台默认 GBK，rich 的特殊字符（▶ 等）和 JSONL 里的非 ASCII
# 会直接 UnicodeEncodeError。在创建任何 Console 之前把标准流切到 UTF-8。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

from .core.goal import EXIT_BUDGET, EXIT_COMPLETE, EXIT_ERROR, EXIT_SOLVED
from .core.steer import SteerQueue, start_stdin_thread
from .protocol.types import Event
from .runner import SolveRun, build_run, execute_run
from .store.rollout import read_session_meta

app = typer.Typer(help="foxdog — CTF 自动解题 agent CLI（M5）")
err_console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """裸 foxdog 进入交互式会话 TUI（kimi-code 风格持续会话）。"""
    if ctx.invoked_subcommand is None:
        from .interactive import ChatApp  # 懒加载：headless 路径不 import textual
        from .tui_driver import run_tui_app

        run_tui_app(ChatApp())


@app.command()
def chat(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="run 根目录（默认当前目录）"),
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """进入交互式会话 TUI（裸 foxdog 同款）：持续对话，连续解多题。"""
    from .interactive import ChatApp
    from .tui_driver import run_tui_app

    run_tui_app(ChatApp(workspace=workspace, overrides=list(overrides)))


class EventRenderer:
    """事件流渲染：rich（默认）或 JSONL（--json）。"""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self.console = Console()
        self._streaming = False

    def __call__(self, event: Event) -> None:
        if self.as_json:
            print(event.model_dump_json(), flush=True)
            return
        kind = event.kind
        if kind == "agent_message_delta":
            self.console.print(event.delta, end="", highlight=False)  # type: ignore[attr-defined]
            self._streaming = True
        elif kind == "model_request_complete":
            if event.reasoning_chars and event.duration_ms >= 1000:  # type: ignore[attr-defined]
                self._end_stream()
                tokens = (
                    f" · {event.completion_tokens:,} tokens"  # type: ignore[attr-defined]
                    if event.completion_tokens is not None  # type: ignore[attr-defined]
                    else ""
                )
                self.console.print(
                    f"[dim italic]{self._prefix(event)}thinking 完成 · "
                    f"{event.duration_ms / 1000:.1f}s{tokens}[/dim italic]"  # type: ignore[attr-defined]
                )
        elif kind == "turn_started":
            self.console.rule(f"[bold]{self._prefix(event)}turn {event.turn}")  # type: ignore[attr-defined]
        elif kind == "tool_call_begin":
            self._end_stream()
            args = event.arguments.replace("\n", " ")  # type: ignore[attr-defined]
            if len(args) > 80:
                args = args[:77] + "..."
            self.console.print(f"[cyan]{self._prefix(event)}▶ {event.name}[/cyan] {args}")  # type: ignore[attr-defined]
        elif kind == "tool_call_end":
            status = "[green]✓[/green]" if event.ok else "[red]✗[/red]"  # type: ignore[attr-defined]
            note = f" [red]{event.error}[/red]" if event.error else ""  # type: ignore[attr-defined]
            self.console.print(f"  {self._prefix(event)}{status} {event.name} ({event.duration_ms}ms){note}")  # type: ignore[attr-defined]
        elif kind == "flag_verified":
            if event.ok:  # type: ignore[attr-defined]
                self.console.print(f"\n[bold green]{self._prefix(event)}FLAG VERIFIED: {event.flag}[/bold green]")  # type: ignore[attr-defined]
            else:
                self.console.print(f"[yellow]{self._prefix(event)}flag 被拒: {event.reason}[/yellow]")  # type: ignore[attr-defined]
        elif kind == "turn_complete":
            self._end_stream()
            if event.stop_reason in ("completed", "truncated", "max_steps"):  # type: ignore[attr-defined]
                reason = {
                    "completed": "本轮完成，准备下一轮",
                    "truncated": "输出触顶，恢复推进",
                    "max_steps": "本轮达到步数上限，继续推进",
                }[event.stop_reason]  # type: ignore[attr-defined]
                self.console.print(f"[dim]{self._prefix(event)}turn {event.turn} 完成 · {reason} usage={event.usage}[/dim]")  # type: ignore[attr-defined]
            else:
                self.console.print(f"[dim]{self._prefix(event)}turn {event.turn} 结束: {event.stop_reason} usage={event.usage}[/dim]")  # type: ignore[attr-defined]
        elif kind == "background_task_complete":
            self._end_stream()
            status = "[green]✓[/green]" if event.ok else "[red]✗[/red]"  # type: ignore[attr-defined]
            code = f" exit={event.exit_code}" if event.exit_code is not None else ""  # type: ignore[attr-defined]
            self.console.print(f"{self._prefix(event)}{status} 后台任务 {event.task_id} 完成{code}")  # type: ignore[attr-defined]
        elif kind == "swarm_note":
            self._end_stream()
            self.console.print(f"[magenta]⚑ {self._prefix(event)}{event.message}[/magenta]")  # type: ignore[attr-defined]
        elif kind == "error":
            self._end_stream()
            err_console.print(f"[red]{self._prefix(event)}error: {event.message}[/red]")  # type: ignore[attr-defined]

    def _prefix(self, event: Event) -> str:
        r"""worker 事件前缀（swarm/子 agent 输出来源区分；\[ 转义防 rich 当标签）。"""
        w = getattr(event, "worker", None)
        return f"\\[{w}] " if w else ""

    def _end_stream(self) -> None:
        if self._streaming:
            self.console.print()
            self._streaming = False


async def _solve_async(
    objective: str,
    target: str | None,
    flag_format: str | None,
    attach: list[Path],
    mode: str,
    max_minutes: float | None,
    max_tokens: int | None,
    max_turns: int | None,
    workers: int,
    planner_model: str | None,
    expect_flags: int | None,
    engines: bool | None,
    as_json: bool,
    workspace: Path | None,
    overrides: list[str],
) -> int:
    render = EventRenderer(as_json)
    steer = SteerQueue()
    start_stdin_thread(steer)  # headless steer：tty 下后台线程读 stdin（非 tty 跳过）
    run = await build_run(
        objective, target, flag_format, list(attach), mode,
        max_minutes, max_tokens, max_turns, workers, planner_model, list(overrides),
        emit=render, steer=steer, workspace=workspace, expected_flags=expect_flags, engines_enabled=engines,
    )
    code = await execute_run(run)

    summary_console = err_console if as_json else render.console
    if run.writeup_path is not None:
        summary_console.print(f"[dim]writeup: {run.writeup_path}[/dim]")
    if code == EXIT_SOLVED:
        summary_console.print(f"[bold green]解题成功[/bold green] flag={run.gate.flag} run={run.ws.run_id}")
    elif code == EXIT_COMPLETE:
        summary_console.print(f"[bold cyan]调查完成[/bold cyan]（旁观者/规划者判定任务已收口）run={run.ws.run_id}")
    elif code == EXIT_BUDGET:
        err_console.print(f"[yellow]预算耗尽，未拿到 flag。run={run.ws.run_id}[/yellow]")
    else:
        err_console.print(f"[red]异常终止。run={run.ws.run_id}[/red]")
    return code


@app.command()
def solve(
    objective: str = typer.Argument(..., help="题目描述"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="靶机地址（URL 或 host:port）"),
    flag_format: Optional[str] = typer.Option(None, "--flag-format", help=r"flag 格式正则，如 flag\{.*\}"),
    attach: list[Path] = typer.Option([], "--attach", "-a", help="题目附件（可多次）"),
    yolo: bool = typer.Option(True, "--yolo/--plan", help="权限模式：yolo 全放行 / plan 只读"),
    max_minutes: Optional[float] = typer.Option(None, "--max-minutes", help="时间预算（分钟，默认 60）"),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens", help="token 预算"),
    max_turns: Optional[int] = typer.Option(None, "--max-turns", help="轮数预算（默认 50）"),
    workers: int = typer.Option(1, "--workers", help="worker 数：1=单 agent（默认），>1 进 swarm 模式"),
    planner_model: Optional[str] = typer.Option(None, "--planner-model", help="规划/压缩模型名（覆盖 [model.planner]，沿用 solver 端点）"),
    expect_flags: Optional[int] = typer.Option(None, "--expect-flags", help="本题预期 flag 面数（多 flag 题 >1，默认读 [platform].expected_flags 或 1）"),
    engines: Optional[bool] = typer.Option(None, "--engines/--no-engines", help="race 异构引擎：默认按配置/探测自动；--no-engines 强制单 solver"),
    as_json: bool = typer.Option(False, "--json", help="每行一个 Event JSON，stdout 可管道"),
    tui: bool = typer.Option(False, "--tui", help="Textual TUI 前端（时间线+黑板面板+steer 输入框）"),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="run 根目录（默认当前目录）"),
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """自动解一道 CTF 题：侦察→分析→利用→submit_flag。"""
    mode = "yolo" if yolo else "plan"
    if tui:
        from .tui import FoxdogApp  # 懒加载：headless 不 import textual
        from .tui_driver import run_tui_app

        tui_app = FoxdogApp(
            objective=objective, target=target, flag_format=flag_format, attach=list(attach),
            mode=mode, max_minutes=max_minutes, max_tokens=max_tokens, max_turns=max_turns,
            workers=workers, planner_model=planner_model, workspace=workspace, overrides=list(overrides),
        )
        run_tui_app(tui_app)
        raise typer.Exit(tui_app.exit_code)
    try:
        code = asyncio.run(
            _solve_async(
                objective, target, flag_format, list(attach), mode,
                max_minutes, max_tokens, max_turns, workers, planner_model, expect_flags, engines, as_json, workspace, list(overrides),
            )
        )
    except KeyboardInterrupt:
        code = EXIT_ERROR
    raise typer.Exit(code)


@app.command()
def mcp(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="保留位（不用）"),
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """列出配置的 MCP server 及其工具（调试用）。"""
    from .config import load_config

    cfg = load_config(list(overrides))
    if not cfg.mcp_servers:
        err_console.print(r"[dim]未配置 MCP server（config.toml \[mcp_servers.<name>]）[/dim]")
        raise typer.Exit(0)

    async def _list() -> None:
        from .mcp_client import McpManager

        manager = McpManager()
        results = await manager.connect_all(cfg.mcp_servers)
        for name, warning in results.items():
            if warning:
                Console().print(f"[red]✗ {name}[/red] {warning}")
            else:
                tools = [n.split("__", 2)[2] for n in manager.names if n.startswith(f"mcp__{name}__")]
                Console().print(f"[green]✓ {name}[/green]（{len(tools)} 个工具）")
                for t in tools:
                    prefixed = f"mcp__{name}__{t}"
                    desc = dict(manager.catalog_entries()).get(prefixed, "")
                    Console().print(f"    {prefixed}  [dim]{desc[:70]}[/dim]")
        await manager.close()

    asyncio.run(_list())


@app.command("runtime-build")
def runtime_build(
    image: Optional[str] = typer.Option(None, "--image", help="镜像名（默认读取 [runtime].image）"),
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """构建 FoxDog 的无凭据 CTF 工具镜像。"""
    from .config import load_config
    from .executor.runtime import build_runtime_image

    cfg = load_config(list(overrides))
    selected = image or cfg.runtime.image
    console = Console()
    console.print(f"[cyan]构建 Docker 工具镜像 {selected}[/cyan]")

    def stream(chunk: str) -> None:
        console.print(chunk, end="", highlight=False)

    try:
        asyncio.run(
            build_runtime_image(
                selected,
                docker_binary=cfg.runtime.docker_binary,
                on_output=stream,
            )
        )
    except (RuntimeError, KeyboardInterrupt) as exc:
        err_console.print(f"[red]工具镜像构建失败：{exc}[/red]")
        raise typer.Exit(EXIT_ERROR) from exc
    console.print(f"[bold green]工具镜像已就绪：{selected}[/bold green]")


@app.command()
def runs(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="run 根目录（默认当前目录）"),
) -> None:
    """列出历史 run 及结果。"""
    base = (workspace or Path.cwd()) / ".foxdog-runs"
    if not base.is_dir():
        err_console.print("[dim]还没有任何 run[/dim]")
        raise typer.Exit(0)
    table = Table("run_id", "目标", "模型", "结果", "flag")
    for run_dir in sorted(base.iterdir()):
        if not run_dir.is_dir():
            continue
        meta = read_session_meta(run_dir / "rollout.jsonl") or {}
        winner = None
        wpath = run_dir / "winner.json"
        if wpath.is_file():
            import json

            winner = json.loads(wpath.read_text(encoding="utf-8"))
        objective = str(meta.get("objective", ""))[:40]
        table.add_row(
            run_dir.name,
            objective,
            str(meta.get("model", "")),
            "[green]solved[/green]" if winner else "—",
            str(winner.get("flag", "")) if winner else "",
        )
    Console().print(table)


@app.command()
def resume(
    run_id: Optional[str] = typer.Argument(None, help="要续跑的 run_id（省略则打开 TUI 运行选择器）"),
    headless: bool = typer.Option(False, "--headless", help="不开 TUI，直接 headless 续跑（脚本用；省略 run_id 时续最近一个）"),
    workers: int = typer.Option(1, "--workers", help="worker 数：1=单 agent，>1 进 swarm（仅 --headless）"),
    max_minutes: Optional[float] = typer.Option(None, "--max-minutes", help="追加时间预算（分钟，仅 --headless）"),
    as_json: bool = typer.Option(False, "--json", help="每行一个 Event JSON（仅 --headless）"),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="run 根目录（默认当前目录）"),
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """续跑历史任务：默认打开交互 TUI（给 run_id 直接续，省略则列出历史 run 让你选）。

    从 .foxdog-runs/<run_id>/rollout.jsonl 重放主 agent 历史 + 目标/靶机/flag 格式，
    在新 run 目录里继续（原 run 记录不改）。--headless 走无界面续跑（省略 run_id 续最近一个）。
    """
    if not headless:
        # 默认：打开交互 TUI，进入 resume 流程（run_id 直接续，空串弹运行选择器）
        from .interactive import ChatApp
        from .tui_driver import run_tui_app

        app_obj = ChatApp(
            workspace=workspace, overrides=list(overrides),
            resume_run_id=run_id or "",  # "" → TUI 里列出历史 run 让用户 /resume <id>
        )
        run_tui_app(app_obj)
        raise typer.Exit(app_obj.exit_code)

    # --headless：无界面续跑（脚本/CI 用）
    from .store.rollout import find_run_summary, replay_history

    item = find_run_summary(workspace, run_id)
    if item is None:
        base = (workspace or Path.cwd()) / ".foxdog-runs"
        if not base.is_dir():
            err_console.print("[red]没有任何 run 可续跑[/red]")
        else:
            err_console.print(f"[red]找不到可续跑的 run{'：' + run_id if run_id else ''}[/red]")
        raise typer.Exit(EXIT_ERROR)

    history = replay_history(item.path / "rollout.jsonl")
    if not history:
        err_console.print(f"[yellow]{item.run_id} 无可重放历史（可能刚开始就中断），将按原目标重新开始[/yellow]")
    objective = item.objective or "（续跑：目标未记录）"
    err_console.print(f"[cyan]续跑 {item.run_id}[/cyan]：{objective[:60]}（重放 {len(history)} 条历史）")

    try:
        code = asyncio.run(
            _resume_async(
                objective, item.target, item.flag_format,
                workers, max_minutes, history, as_json, workspace, list(overrides), item.path,
            )
        )
    except KeyboardInterrupt:
        code = EXIT_ERROR
    raise typer.Exit(code)


async def _resume_async(objective, target, flag_format, workers, max_minutes, history, as_json, workspace, overrides, resume_from) -> int:
    render = EventRenderer(as_json)
    steer = SteerQueue()
    start_stdin_thread(steer)
    run = await build_run(
        objective, target, flag_format, [], "yolo",
        max_minutes, None, None, workers, None, overrides,
        emit=render, steer=steer, workspace=workspace, history=history, resume_from=resume_from,
    )
    code = await execute_run(run)
    summary_console = err_console if as_json else render.console
    if run.writeup_path is not None:
        summary_console.print(f"[dim]writeup: {run.writeup_path}[/dim]")
    if code == EXIT_SOLVED:
        summary_console.print(f"[bold green]解题成功[/bold green] flag={run.gate.flag} run={run.ws.run_id}")
    elif code == EXIT_COMPLETE:
        summary_console.print(f"[bold cyan]调查完成[/bold cyan] run={run.ws.run_id}")
    elif code == EXIT_BUDGET:
        err_console.print(f"[yellow]预算耗尽。run={run.ws.run_id}[/yellow]")
    else:
        err_console.print(f"[red]异常终止。run={run.ws.run_id}[/red]")
    return code

kali_app = typer.Typer(help="常驻 Kali 容器（foxdog-kali）生命周期管理", no_args_is_help=True)
app.add_typer(kali_app, name="kali")


def _kali_runtime(overrides: list[str]):
    """管理命令用的 runtime 句柄：bind 当前目录的 .foxdog-runs 根。"""
    from .config import load_config
    from .executor.runtime import DockerPersistentRuntime

    cfg = load_config(list(overrides))
    runs_dir = Path.cwd() / ".foxdog-runs" / "kali-shared"
    return DockerPersistentRuntime(
        runs_dir,
        image=cfg.runtime.image,
        docker_binary=cfg.runtime.docker_binary,
        network=cfg.runtime.network,
        auto_build=cfg.runtime.auto_build,
    )


async def _kali_exec(argv: list[str], overrides: list[str]) -> int:
    from .config import load_config
    from .executor.process import run_argv

    cfg = load_config(list(overrides))
    return_code = 0
    result = await run_argv(argv, idle_timeout=300.0, total_timeout=600.0)
    if result.output:
        Console().print(result.output, end="", highlight=False)
    if result.error:
        err_console.print(f"[red]{result.error}[/red]")
        return_code = 1
    elif result.exit_code != 0:
        return_code = result.exit_code
    return return_code


@kali_app.command("start")
def kali_start(
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """创建/复用常驻容器 foxdog-kali 并打印状态（镜像缺失且 auto_build 开则先构建）。"""
    console = Console()

    async def _start() -> None:
        runtime = _kali_runtime(overrides)
        await runtime.start()
        state = await runtime._container_state()
        console.print(f"[bold green]foxdog-kali 运行中[/bold green]（state={state}，镜像 {runtime.image}）")
        console.print(f"挂载：{runtime.workspace} ↔ /workspace；默认 cwd /workspace/kali-shared/work")
        await runtime.close()

    asyncio.run(_start())


@kali_app.command("stop")
def kali_stop(
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """stop + rm 常驻容器（容器内状态一并丢弃）。"""
    from .config import load_config

    cfg = load_config(list(overrides))
    code = asyncio.run(
        _kali_exec(
            [cfg.runtime.docker_binary, "rm", "--force", "foxdog-kali"],
            overrides,
        )
    )
    if code == 0:
        Console().print("[yellow]foxdog-kali 已停止并删除[/yellow]")
    raise typer.Exit(code)


@kali_app.command("reset")
def kali_reset(
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """rm 后重建常驻容器（清空容器内全部状态）。"""
    from .config import load_config

    cfg = load_config(list(overrides))
    asyncio.run(_kali_exec([cfg.runtime.docker_binary, "rm", "--force", "foxdog-kali"], overrides))

    async def _start() -> None:
        runtime = _kali_runtime(overrides)
        await runtime.start()
        await runtime.close()

    asyncio.run(_start())
    Console().print("[bold green]foxdog-kali 已重建（干净状态）[/bold green]")


@kali_app.command("status")
def kali_status(
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """打印常驻容器运行状态 / 镜像 / 挂载。"""
    from .config import load_config
    from .executor.process import run_argv

    cfg = load_config(list(overrides))

    async def _status() -> None:
        console = Console()
        runtime = _kali_runtime(overrides)
        state = await runtime._container_state()
        console.print(f"容器 foxdog-kali：{state or '不存在'}")
        inspect = await run_argv(
            [cfg.runtime.docker_binary, "image", "inspect", "--format", "{{.Id}} {{.Size}}", runtime.image],
            idle_timeout=30.0, total_timeout=30.0,
        )
        if inspect.error is None and inspect.exit_code == 0:
            console.print(f"镜像 {runtime.image}：{inspect.output.strip()}")
        else:
            console.print(f"镜像 {runtime.image}：[red]缺失[/red]（foxdog kali start 会自动构建）")
        if state:
            mounts = await run_argv(
                [cfg.runtime.docker_binary, "inspect", "--format", "{{json .Mounts}}", "foxdog-kali"],
                idle_timeout=30.0, total_timeout=30.0,
            )
            if mounts.error is None and mounts.exit_code == 0:
                console.print(f"挂载：{mounts.output.strip()}")

    asyncio.run(_status())


@kali_app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def kali_exec(
    ctx: typer.Context,
    overrides: list[str] = typer.Option([], "-c", help="配置覆盖 key=value（可多次）"),
) -> None:
    """透传交互 shell：`foxdog kali exec` 进 bash；`foxdog kali exec nmap -sV x` 跑单条命令。"""
    import subprocess  # noqa: S404 - 交互 shell 必须继承 stdio

    from .config import load_config

    cfg = load_config(list(overrides))
    argv = [cfg.runtime.docker_binary, "exec"]
    if sys.stdin.isatty():
        argv.append("-it")
    argv.append("foxdog-kali")
    if ctx.args:
        argv.extend(["bash", "-lc", " ".join(ctx.args)])
    else:
        argv.append("bash")
    raise typer.Exit(subprocess.call(argv))
if __name__ == "__main__":
    app()

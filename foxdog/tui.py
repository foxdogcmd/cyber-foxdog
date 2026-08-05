"""Textual TUI（DESIGN §8.1）：事件时间线 + 黑板面板 + steer 输入框 + 状态行。

与 headless 共用 runner.build_run/execute_run；emit 是同步回调，事件进
queue.SimpleQueue，Textual 定时器泵到 RichLog（线程安全，无需 call_from_thread）。
solve 协程跑在 Textual 事件循环的 asyncio task 里；退出先取消 task，
execute_run 的 finally 负责干净清理（sessions/background/rollout/黑板/writeup）。
"""
from __future__ import annotations

import asyncio
import queue
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static

from .core.goal import EXIT_SOLVED
from .core.steer import SteerQueue
from .protocol.types import Event
from .runner import SolveRun, build_run, execute_run
from .tui_driver import get_tui_driver_class


class FoxdogApp(App):
    """foxdog solve --tui 前端。"""

    TITLE = "foxdog"
    ALLOW_SELECT = True

    CSS = """
    #status { height: 1; dock: top; padding: 0 1; background: $panel; }
    #board { width: 46; border-left: solid $primary; }
    #board Static { height: auto; max-height: 33%; border-bottom: solid $panel; overflow-y: auto; }
    #timeline { width: 1fr; }
    Input { dock: bottom; }
    """
    BINDINGS = [
        ("q", "quit", "退出"),
        Binding("ctrl+c", "handle_ctrl_c", "复制/退出", priority=True),
        ("ctrl+b", "background_command", "命令转后台"),
        ("/", "focus_input", "输入"),
    ]

    def __init__(
        self,
        *,
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
        workspace: Path | None,
        overrides: list[str],
    ) -> None:
        super().__init__(driver_class=get_tui_driver_class())
        self._params = dict(
            objective=objective, target=target, flag_format=flag_format, attach=attach, mode=mode,
            max_minutes=max_minutes, max_tokens=max_tokens, max_turns=max_turns,
            workers=workers, planner_model=planner_model, overrides=overrides, workspace=workspace,
        )
        self.exit_code = 3
        self.steer = SteerQueue()
        self._queue: queue.SimpleQueue[Event | None] = queue.SimpleQueue()  # None=solve 结束哨兵
        self.run: SolveRun | None = None
        self._solve_task: asyncio.Task | None = None
        self._stream_buf = ""  # agent_message_delta 攒行缓冲
        self._done = False

    # ------------------------------------------------------------ 布局
    def compose(self) -> ComposeResult:
        yield Static("装配中…", id="status")
        with Horizontal():
            yield RichLog(id="timeline", wrap=True)
            with Vertical(id="board"):
                yield Static("已知事实\n（装配中）", id="facts")
                yield Static("已排除方向\n（装配中）", id="deadends")
                yield Static("Intents\n（装配中）", id="intents")
        yield Input(placeholder="steer 指挥… 回车注入 | /quit 退出 | /mode yolo|plan 切权限模式")

    async def on_mount(self) -> None:
        self.set_interval(0.1, self._pump_events)
        self.set_interval(2.0, self._refresh_board)
        self.set_interval(1.0, self._refresh_status)
        try:
            self.run = await build_run(**self._params, emit=self._queue.put, steer=self.steer)
        except Exception as exc:  # noqa: BLE001 - 装配失败直接显示
            self.query_one("#timeline", RichLog).write(f"装配失败: {type(exc).__name__}: {exc}")
            self._done = True
            return
        self._solve_task = asyncio.create_task(self._execute())

    async def _execute(self) -> None:
        assert self.run is not None
        try:
            code = await execute_run(self.run)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - execute_run 内部已兜底，这里是双保险
            code = 3
        self.exit_code = code
        self._queue.put(None)

    # ------------------------------------------------------------ 事件泵
    def _pump_events(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._on_solve_done()
                continue
            self._render_event(item)

    def _render_event(self, event: Event) -> None:
        log = self.query_one("#timeline", RichLog)
        kind = event.kind
        if kind == "agent_message_delta":
            self._stream_buf += event.delta  # type: ignore[attr-defined]
            if "\n" in self._stream_buf:
                head, _, self._stream_buf = self._stream_buf.rpartition("\n")
                log.write(Text(head))
            return
        if kind == "agent_reasoning_delta" or kind == "model_request_started":
            return
        self._flush_stream(log)
        prefix = f"[{event.worker}] " if getattr(event, "worker", None) else ""
        if kind == "turn_started":
            log.write(Text(f"── {prefix}turn {event.turn} ──", style="bold"))  # type: ignore[attr-defined]
        elif kind == "model_request_complete":
            if event.reasoning_chars and event.duration_ms >= 1000:  # type: ignore[attr-defined]
                tokens = (
                    f" · {event.completion_tokens:,} tokens"  # type: ignore[attr-defined]
                    if event.completion_tokens is not None  # type: ignore[attr-defined]
                    else ""
                )
                log.write(
                    Text(
                        f"{prefix}thinking 完成 · {event.duration_ms / 1000:.1f}s{tokens}",  # type: ignore[attr-defined]
                        style="dim italic",
                    )
                )
        elif kind == "tool_call_begin":
            args = event.arguments.replace("\n", " ")  # type: ignore[attr-defined]
            if len(args) > 80:
                args = args[:77] + "..."
            log.write(Text(f"{prefix}▶ {event.name} {args}", style="cyan"))  # type: ignore[attr-defined]
        elif kind == "tool_call_end":
            mark = "✓" if event.ok else "✗"  # type: ignore[attr-defined]
            style = "green" if event.ok else "red"  # type: ignore[attr-defined]
            note = f" {event.error}" if event.error else ""  # type: ignore[attr-defined]
            log.write(Text(f"  {prefix}{mark} {event.name} ({event.duration_ms}ms){note}", style=style))  # type: ignore[attr-defined]
        elif kind == "flag_verified":
            if event.ok:  # type: ignore[attr-defined]
                log.write(Text(f"{prefix}FLAG VERIFIED: {event.flag}", style="bold green"))  # type: ignore[attr-defined]
            else:
                log.write(Text(f"{prefix}flag 被拒: {event.reason}", style="yellow"))  # type: ignore[attr-defined]
        elif kind == "turn_complete":
            if event.stop_reason in ("completed", "truncated", "max_steps"):  # type: ignore[attr-defined]
                reason = {
                    "completed": "本轮完成，准备下一轮",
                    "truncated": "输出触顶，恢复推进",
                    "max_steps": "本轮达到步数上限，继续推进",
                }[event.stop_reason]  # type: ignore[attr-defined]
                log.write(Text(f"{prefix}turn {event.turn} 完成 · {reason}", style="dim"))  # type: ignore[attr-defined]
            else:
                log.write(Text(f"{prefix}turn {event.turn} 结束: {event.stop_reason}", style="dim"))  # type: ignore[attr-defined]
        elif kind == "background_task_complete":
            mark = "✓" if event.ok else "✗"  # type: ignore[attr-defined]
            style = "green" if event.ok else "red"  # type: ignore[attr-defined]
            code = f" exit={event.exit_code}" if event.exit_code is not None else ""  # type: ignore[attr-defined]
            log.write(Text(f"{prefix}{mark} 后台任务 {event.task_id} 完成{code}", style=style))  # type: ignore[attr-defined]
        elif kind == "swarm_note":
            log.write(Text(f"⚑ {prefix}{event.message}", style="magenta"))  # type: ignore[attr-defined]
        elif kind == "error":
            log.write(Text(f"{prefix}error: {event.message}", style="red"))  # type: ignore[attr-defined]

    def _flush_stream(self, log: RichLog) -> None:
        if self._stream_buf.strip():
            log.write(Text(self._stream_buf))
        self._stream_buf = ""

    def _on_solve_done(self) -> None:
        if self._done:
            return
        self._done = True
        log = self.query_one("#timeline", RichLog)
        self._flush_stream(log)
        if self.exit_code == EXIT_SOLVED and self.run is not None:
            log.write(Text(f"解题成功 flag={self.run.gate.flag}", style="bold green"))
        elif self.exit_code == 2:
            log.write(Text("预算耗尽，未拿到 flag。", style="yellow"))
        else:
            log.write(Text("已终止（异常或手动退出）。", style="red"))
        if self.run is not None and self.run.writeup_path is not None:
            log.write(Text(f"writeup: {self.run.writeup_path}", style="dim"))
        self._refresh_status()

    # ------------------------------------------------------------ 面板刷新
    def _refresh_board(self) -> None:
        if self.run is None:
            return
        board = self.run.ctx.facts
        if board is None:
            return
        try:
            facts = board.facts_view()
            deadends = board.deadends_view()
            intents = board.intents(limit=20)
        except Exception:  # noqa: BLE001 - 任务刚结束黑板已关闭，跳过本拍
            return
        self.query_one("#facts", Static).update(
            "已知事实\n" + ("\n".join(f"• {f['fact'][:60]}" for f in facts) or "（无）")
        )
        self.query_one("#deadends", Static).update(
            "已排除方向\n" + ("\n".join(f"• {d['direction'][:50]}" for d in deadends) or "（无）")
        )
        self.query_one("#intents", Static).update(
            "Intents\n"
            + ("\n".join(f"• {i['id']}[{i['status']}] {i['goal'][:40]}" for i in intents) or "（无）")
        )

    def _refresh_status(self) -> None:
        if self.run is None:
            return
        elapsed = time.monotonic() - self.run.started
        state = self.run.loop.budget_state
        if self.run.coordinator is not None and self.run.coordinator.factory.budget_state is not None:
            state = self.run.coordinator.factory.budget_state
        tokens = state.total_tokens if state is not None else 0
        flag = self.run.gate.flag or "未拿到"
        cap = f"{self.run.budget.max_minutes:g}min" if self.run.budget.max_minutes is not None else "∞"
        line = Text()
        line.append("引擎 ", style="dim")
        for index, engine in enumerate(self.run.active_engine_labels):
            if index:
                line.append(" + ", style="dim")
            line.append(engine, style="bold cyan")
        line.append(
            f" | run={self.run.ws.run_id} | 用时 {elapsed / 60:.1f}/{cap}"
            f" | tokens={tokens} | workers={self.run.workers} | mode={self.run.permission.mode}"
            f" | runtime={self.run.runtime.kind} | flag={flag}",
            style="dim",
        )
        self.query_one("#status", Static).update(line, layout=False)

    # ------------------------------------------------------------ 输入
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        log = self.query_one("#timeline", RichLog)
        if text == "/quit":
            asyncio.create_task(self.action_quit())
            return
        if text.startswith("/mode "):
            mode = text.split(None, 1)[1].strip()
            if mode in ("yolo", "plan") and self.run is not None:
                self.run.permission.set_mode(mode)
                log.write(Text(f"权限模式切换为 {mode}", style="magenta"))
            else:
                log.write(Text("用法: /mode yolo|plan", style="red"))
            return
        self.steer.put(text)
        log.write(Text(f"steer 已排队：{text}", style="magenta"))

    def action_focus_input(self) -> None:
        self.query_one(Input).focus()

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        try:
            import pyperclip

            pyperclip.copy(text)
        except Exception:  # noqa: BLE001 - OSC52 remains available as fallback
            pass

    def action_copy_selection(self) -> bool:
        """Copy selection and report whether anything was copied."""
        selected = self.screen.get_selected_text() or ""
        if not selected:
            focused = self.screen.focused
            if isinstance(focused, Input):
                selected = focused.selected_text
        if selected:
            self.copy_to_clipboard(selected)
            return True
        return False

    def action_handle_ctrl_c(self) -> None:
        if not self.action_copy_selection():
            asyncio.create_task(self.action_quit())

    def action_background_command(self) -> None:
        if self.run is None:
            return
        task = self.run.background.detach_latest()
        log = self.query_one("#timeline", RichLog)
        if task is None:
            log.write(Text("当前没有等待中的 shell 命令", style="dim"))
            return
        log.write(Text(f"↳ {task.task_id} 已转后台：{task.command}", style="yellow"))

    async def action_quit(self) -> None:
        """退出：先取消 solve task（execute_run finally 干净清理），再关 app。"""
        if self._solve_task is not None and not self._solve_task.done():
            self._solve_task.cancel()
            try:
                await asyncio.wait_for(self._solve_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self.exit()

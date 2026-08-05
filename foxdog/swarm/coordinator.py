"""协调主循环（DESIGN §6.1 简化版）：race 快路径 + 2s 一拍 OODA swarm。

race：主 worker 整题冲刺（现有 GoalMode），race_minutes 或出 flag 为止。
主循环：黑板有变化且过冷却 → ReasonPlanner 出 intent 上板（自动去重）；
空槽且有货 → 每拍最多补 1 个 worker（平滑扩容，上限 workers）；
worker 认领 intent 带黑板摘要跑单 turn，结束自动兜底收尾；
预算：race 与 swarm 阶段共用一本累计账（BudgetState 接力），worker loop
每 step 前检查共享 BudgetState。
"""
from __future__ import annotations

import asyncio
import itertools
import re
import time
from typing import Any

from ..core.budget import Budget, BudgetExceeded, BudgetState
from ..core.goal import EXIT_BUDGET, EXIT_COMPLETE, EXIT_ERROR, EXIT_SOLVED, GoalMode
from ..core.steer import SteerQueue
from ..gate.flag_gate import FlagGate
from ..llm.messages import system_message, user_message
from ..protocol.types import AgentReasoningDelta, Error, ModelRequestComplete, ModelRequestStarted, SwarmNote
from ..store.rollout import RolloutWriter
from ..tools.builtin.subagent import last_assistant_text
from .blackboard import Blackboard
from .planner import ReasonPlanner
from .worker import WorkerFactory
from .spectator import Spectator

WORKER_SYSTEM = """\
你是 CTF 解题 swarm 的 worker，与其他 worker 共享工作区、黑板和 FlagGate。
纪律：
1. 开工先读板：下方黑板摘要里有控制指令、已知事实、已排除方向和其他 intent 状态；死路别再试。
2. 每确认一个事实就 post_fact 上板，每排除一个方向就 post_deadend 上板。
3. 你的 intent 完成就 complete_intent(status=done, result=结论)；试尽了没货就
   complete_intent(status=barren, result=原因)——不汇报等于白跑。
4. 拿到候选 flag 立刻 submit_flag——闸门全队共享，过闸即全员停止。
5. 只做你认领的 intent，别抢别人的方向。
6. 盯着靶机打，别元探索：不翻本地 run 目录/rollout/平台主站/代码仓库找答案——
   答案只在靶机上。靶机暂时打不通就轮询等待（background 任务），同时继续做手头能做的事。
7. 改变靶机全局状态的操作（.htaccess/入口文件/配置）遵守主纪律第 11 条：先备份、
   最小验证、写完立刻检查 200，5xx 马上复原。
8. **收尾必须记录发现**：完成 intent 前，先把本轮所有重要发现（端口/服务/漏洞/凭证/路径）
   用 post_fact 上板——没上板的发现等于没发现，后续 worker 和 planner 看不到。
   即使是 barren（方向走不通），也要 post_deadend 记录排除原因。
9. read_file/write_file/edit_file 与 shell 同一个文件系统：相对路径与 /workspace 路径两端可见；
   宿主上的资料先 import_file 复制进工作区，别猜两端路径。
"""


class Coordinator:
    def __init__(
        self,
        *,
        board: Blackboard,
        planner: ReasonPlanner,
        factory: WorkerFactory,
        emit,
        rollout: RolloutWriter | None,
        gate: FlagGate,
        objective: str,
        budget: Budget,
        target: str | None = None,
        flag_format: str | None = None,
        workers: int = 2,
        history: list[dict[str, Any]] | None = None,
        race_goal: GoalMode | None = None,
        race_minutes: float = 3.0,
        tick: float = 2.0,
        planner_cooldown: float = 10.0,
        spectator_cooldown: float = 30.0,
        steer: SteerQueue | None = None,
        interrupt: asyncio.Event | None = None,
        spawn_batch: int = 3,
        race_engines: list[tuple[str, Any, Any]] | None = None,
        spectator: Spectator | None = None,
    ) -> None:
        self.board = board
        self.planner = planner
        self.factory = factory
        self.emit = emit
        self.rollout = rollout
        self.gate = gate
        self.objective = objective
        self.budget = budget
        self.target = target
        self.flag_format = flag_format
        self.workers = max(2, workers)
        self.desired_workers = self.workers
        self.history = history if history is not None else []
        self.race_goal = race_goal
        self.race_minutes = race_minutes
        self.tick = tick
        self.planner_cooldown = planner_cooldown
        self.spectator_cooldown = spectator_cooldown
        self.steer = steer
        self.interrupt = interrupt or asyncio.Event()  # 用户 Esc 中断（swarm 阶段）
        self.spawn_batch = spawn_batch  # 每拍最多补的 worker 数（平滑扩容放宽）
        #: race 异构引擎：[（名字, runner 协程函数, GoalMode|None)]；history 挂在 goal.history_ref（遗产交接）
        self.race_engines = list(race_engines or [])
        self.spectator = spectator
        self._last_spectator_version = -1
        self._last_spectator_at = 0.0  # 上次旁观者审查时刻（冷却，避免每次板变动都审）
        #: 收口结论（spectator 或 planner 判定 complete 时写入）→ runner 写进 writeup.md
        self.final_summary = ""
        self._race_legacy_collected = False
        self._active_race_engines: set[str] = set()
        self._worker_seq = itertools.count(1)
        self._extra_provider_idx = 0  # round-robin index for extra providers
        self._planner_seq = itertools.count(1)
        self._last_plan_created = 0
        self._last_plan_verdict = "explore"
        self._last_plan_summary = ""
        #: 在跑 worker：wid → (task, intent_id)，copilot get_status/kill_worker 用
        self._worker_tasks: dict[str, asyncio.Task] = {}
        self._worker_intents: dict[str, str] = {}

    def active_workers(self) -> list[tuple[str, str]]:
        """(worker_id, intent_id) 列表（copilot get_status 用）。"""
        return sorted(self._worker_intents.items())

    async def _spectator_once(self) -> str | None:
        """让旁观者审查全局；返回 complete 时的最终退出理由。

        冷却（spectator_cooldown）避免每次黑板变动都审查——否则一个正常干活的
        worker 每被派新 intent/上一条 fact 就可能被 review 腰斩。correct 时把纠偏写成
        独立控制指令（不是 fact；worker 下一 turn 的 board.summary 会看到）；
        无在跑 worker 时顺带触发重规划。complete 才收口。
        """
        if self.spectator is None or self.board.version() == self._last_spectator_version:
            return None
        now = time.monotonic()
        if now - self._last_spectator_at < self.spectator_cooldown:
            return None  # 冷却中：本拍不审，避免高频抢占
        self._last_spectator_version = self.board.version()
        self._last_spectator_at = now
        workers = "\n".join(
            f"- {wid}: intent={self._worker_intents.get(wid, '?')} running={not task.done()}"
            for wid, task in self._worker_tasks.items()
        ) or "（无活动 worker）"
        review = await self.spectator.review(
            self.objective,
            self.board.summary(),
            workers,
            "\n".join(str(item.get("content", "")) for item in self.history[-8:] if isinstance(item, dict)) or "（无）",
        )
        if review.summary:
            self.emit(SwarmNote(message=f"spectator：{review.summary}"))
        if review.verdict == "correct" and review.instruction:
            # 非破坏性纠偏：写独立 directive，避免污染事实表。worker 下一 turn
            # 和 planner 的 board.summary 都会看到；无在跑 worker 时触发重规划落地。
            self.board.post_directive(
                f"旁观者纠偏：{review.instruction}", actor="spectator", priority="high"
            )
            running = any(not t.done() for t in self._worker_tasks.values())
            if not running:
                self._last_plan_created = 0
            self.emit(SwarmNote(message=f"spectator 纠偏：{review.instruction}"))
        elif review.verdict == "complete":
            return review.summary or "旁观者确认任务完成"
        return None

    @property
    def active_race_engines(self) -> tuple[str, ...]:
        """Race engines that are still running, in stable display order."""
        order = ["solver", *(name for name, _fn, _goal in self.race_engines)]
        return tuple(name for name in order if name in self._active_race_engines)

    async def cancel_worker(self, worker_id: str) -> bool:
        """操作员终止在跑 worker：取消 task + 其 intent 置 barren。"""
        task = self._worker_tasks.get(worker_id)
        if task is None or task.done():
            return False
        task.cancel()
        intent_id = self._worker_intents.get(worker_id)
        if intent_id:
            self.board.finish_claimed(intent_id, worker_id, "barren", "操作员终止（kill_worker）")
        self.emit(SwarmNote(message=f"{worker_id} 已被操作员终止", worker=worker_id))
        return True

    def _record(self, record: dict[str, Any]) -> None:
        if self.rollout:
            self.rollout.write(record)

    def _drain_steer(self) -> bool:
        """操作员指令最高优先：上板并抢占旧 worker。"""
        if self.steer is None:
            return False
        received = False
        for text in self.steer.drain():
            received = True
            self.history.append(user_message(f"[操作员 steer 指令] {text}"))
            self.board.post_directive(
                f"最高优先级操作员指令（立即执行，覆盖旧计划）：{text}",
                actor="operator",
                priority="critical",
            )
            self._record({"type": "steer", "text": text})
            self.emit(SwarmNote(message=f"操作员指令已抢占当前计划：{text}"))
        if received:
            for worker_id, task in list(self._worker_tasks.items()):
                if task.done():
                    continue
                task.cancel()
                intent_id = self._worker_intents.get(worker_id)
                if intent_id:
                    self.board.finish_claimed(
                        intent_id, worker_id, "barren", "被更高优先级操作员指令抢占"
                    )
        return received

    # ------------------------------------------------------------ race 快路径
    async def _race(self) -> tuple[int | None, float, int]:
        """返回 (exit_code 或 None 继续 swarm, 已耗分钟, 已耗 tokens)。

        race_minutes 是 race 内部节奏（默认 3 分钟冲刺铃），不是预算；
        总预算 None=无限时不参与 min 计算。
        """
        if self.race_goal is None or self.race_minutes <= 0:
            return None, 0.0, 0
        if self.budget.max_minutes is None:
            race_cap = self.race_minutes
        else:
            race_cap = min(self.race_minutes, self.budget.max_minutes)
        self.race_goal.budget = Budget(
            max_minutes=race_cap,
            max_tokens=self.budget.max_tokens,
            max_turns=self.budget.max_turns,
        )
        if self.race_engines:
            # 异构竞速：内置 solver + model.extra 端点 + 外部 CLI 引擎，first-valid-flag-wins
            return await self._race_with_engines(race_cap)
        code = await self.race_goal.solve(self.history)
        state = self.race_goal.loop.budget_state
        minutes = state.elapsed_minutes if state else self.race_minutes
        tokens = state.total_tokens if state else 0
        if code == EXIT_SOLVED:
            return EXIT_SOLVED, minutes, tokens
        if code == EXIT_ERROR:
            if self.race_goal.last_stop_reason == "interrupted":
                return EXIT_ERROR, minutes, tokens
            self.emit(
                SwarmNote(
                    message=(
                        f"race 未能继续推进（{self.race_goal.last_stop_reason or 'error'}），"
                        "提前切换 swarm 重新规划"
                    )
                )
            )
            return None, minutes, tokens
        # EXIT_BUDGET：区分 race 时间到 vs 总预算到（无限预算不会撞后者）
        if self.race_goal.last_stop_reason == "max_turns":
            return EXIT_BUDGET, minutes, tokens
        if self.budget.max_minutes is not None and minutes >= self.budget.max_minutes:
            return EXIT_BUDGET, minutes, tokens
        if self.budget.max_tokens is not None and tokens >= self.budget.max_tokens:
            return EXIT_BUDGET, minutes, tokens
        return None, minutes, tokens  # race 铃响，进 swarm

    async def _race_with_engines(self, race_cap: float) -> tuple[int | None, float, int]:
        """异构竞速：内置 solver + 全部 race_engines 并行，first-valid-flag-wins。

        任一引擎让 gate.solved → 立即取消其余引擎（cancel → 工具执行层杀进程组）；
        race_cap 到点各 GoalMode 自行 EXIT_BUDGET；全部无旗收场 → 返回 None 进 swarm。
        """
        # 引擎 GoalMode 统一套 race 预算
        for _name, _fn, goal in self.race_engines:
            if goal is not None:
                goal.budget = Budget(
                    max_minutes=race_cap,
                    max_tokens=self.budget.max_tokens,
                    max_turns=self.budget.max_turns,
                )
        runners: list[tuple[str, Any]] = [("solver", lambda: self.race_goal.solve(self.history))]
        runners += [(name, fn) for name, fn, _goal in self.race_engines]
        tasks: dict[asyncio.Task, str] = {}
        for name, fn in runners:
            tasks[asyncio.create_task(fn())] = name
            self._active_race_engines.add(name)
            self.emit(SwarmNote(message=f"race 引擎启动：{name}"))
        started = time.monotonic()
        main_code: int | None = None
        try:
            while tasks:
                done, _pending = await asyncio.wait(
                    set(tasks), timeout=0.2, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    name = tasks.pop(t)
                    self._active_race_engines.discard(name)
                    try:
                        code = t.result()
                    except asyncio.CancelledError:
                        code = None
                    except Exception as exc:  # noqa: BLE001 - 单引擎异常不炸 race
                        self.emit(SwarmNote(message=f"race 引擎 {name} 异常: {type(exc).__name__}: {exc}"))
                        code = None
                    if name == "solver":
                        main_code = code
                    else:
                        self.emit(SwarmNote(message=f"race 引擎 {name} 结束（code={code}）"))
                if self.gate.solved:
                    winner = "（多 flag 凑齐）" if self.gate.expected_flags > 1 else ""
                    self.emit(SwarmNote(message=f"race 出 flag{winner}，取消其余 {len(tasks)} 个引擎"))
                    for t in tasks:
                        t.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                    break
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._active_race_engines.clear()
        # 记账：分钟=墙钟；tokens=所有 GoalMode 引擎合计（CLI 引擎无账单）
        minutes = time.monotonic() - started
        tokens = 0
        goals = [self.race_goal] + [g for _n, _f, g in self.race_engines if g is not None]
        for g in goals:
            if g is not None and g.loop.budget_state is not None:
                tokens += g.loop.budget_state.total_tokens
        if self.gate.solved:
            return EXIT_SOLVED, minutes, tokens
        # 无旗收场：沿用单 worker 的语义（error→提前切 swarm；预算尽→EXIT_BUDGET）
        if main_code == EXIT_ERROR:
            if self.race_goal.last_stop_reason == "interrupted":
                return EXIT_ERROR, minutes, tokens
            self.emit(
                SwarmNote(
                    message=(
                        f"race 未能继续推进（{self.race_goal.last_stop_reason or 'error'}），"
                        "提前切换 swarm 重新规划"
                    )
                )
            )
            return None, minutes, tokens
        if self.budget.max_minutes is not None and minutes / 60 >= self.budget.max_minutes:
            return EXIT_BUDGET, minutes, tokens
        if self.budget.max_tokens is not None and tokens >= self.budget.max_tokens:
            return EXIT_BUDGET, minutes, tokens
        await self._collect_race_legacy()
        return None, minutes, tokens

    async def _collect_race_legacy(self) -> None:
        """race 遗产交接（无 flag 进 swarm 前）：把各引擎的 race 历史**摘要成结构化结论**
        上板，并进 planner 上下文——planner 不从头再来。

        旧实现直接把 last_assistant_text（常是"Let me look at..."这种半句思考）+ 原始
        todo dump 上板，无意义且被 planner audit 拒收后显示成 [rejected] 再消失。改为用
        planner 模型对每个引擎的 race 历史压成"已探明/待验证"要点；模型没产出真结论就
        不上板（worker 纪律要求 agent 自己 post_fact，遗产只补结构化总结）。"""
        if self._race_legacy_collected:
            return
        self._race_legacy_collected = True
        from ..tools.builtin.subagent import last_assistant_text

        # 收集各引擎完整 race 历史（不是只取最后半句）
        histories: list[tuple[str, list]] = []
        if self.history:
            histories.append(("solver", self.history))
        for name, _fn, goal in self.race_engines:
            hist = getattr(goal, "history_ref", None) if goal is not None else None
            if hist:
                histories.append((name, hist))
        if not histories:
            return

        summary = await self._summarize_race(histories)
        if not summary.strip():
            # 模型没给出可用总结：不上碎片，只在有真 fact 时静默（workers 已自己上板）
            return
        self.board.post_fact(f"race 阶段结论（已探明/待验证）：\n{summary[:1200]}", actor="race")
        self._race_legacy = [f"race 阶段结论：\n{summary[:1000]}"]
        self.emit(SwarmNote(message="race 遗产已总结上板（planner 续用，不从头再来）"))

    async def _summarize_race(self, histories: list[tuple[str, list]]) -> str:
        """用 planner 模型把 race 历史压成结构化结论（已探明/待验证/死路）。

        失败/无模型/空历史 → 返回空串（调用方不上板）。绝不把模型思考中途的
        过渡句当结论。"""
        provider = getattr(self.planner, "provider", None)
        if provider is None:
            return ""
        # 拼接各引擎历史里的 assistant 文本（含工具动作痕迹），限长
        chunks: list[str] = []
        for name, hist in histories:
            texts = [
                str(item.get("content", ""))
                for item in hist
                if isinstance(item, dict) and item.get("role") == "assistant" and item.get("content")
            ]
            joined = "\n".join(texts)[-2500:]
            if joined.strip():
                chunks.append(f"【引擎 {name}】\n{joined}")
        if not chunks:
            return ""
        prompt = (
            "下面是多个引擎在限时冲刺阶段的推理/操作记录。请压成给后续团队的**结构化交接**，"
            "只保留确定的事实，不要复述半句思考。用三段：\n"
            "已探明：（确认的文件/服务/漏洞面/关键地址等，逐条）\n"
            "待验证：（有线索但未证实的方向）\n"
            "死路：（已排除的方向，避免重复）\n"
            "每段没有内容就写“无”。不要用 markdown 代码块，直接输出这三段。\n\n"
            + "\n\n".join(chunks)[:8000]
        )
        try:
            msg = await provider.stream_chat(
                [{"role": "user", "content": prompt}], [], lambda _d: None,
            )
            return (msg.content or "").strip()
        except Exception:  # noqa: BLE001 - 总结失败不致命，遗产降级为空
            return ""

    # ------------------------------------------------------------ 规划
    def _planning_context(self) -> str:
        """planner 的完整上下文：题目 + 真实靶机地址 + flag 格式。

        实战翻车：planner 只拿到 objective 文本，看不到真实 URL，派的 intent
        goal 里写占位符 http://target:80/，worker 对着空气打了 15 分钟。"""
        parts = [self.objective]
        if self.target:
            parts.append(f"靶机地址：{self.target}（所有 intent 的 goal 必须用这个真实地址，禁止写 target/占位主机名）")
        if self.flag_format:
            parts.append(f"flag 格式（正则）：{self.flag_format}")
        expected = getattr(self.gate, "expected_flags", 1) if self.gate is not None else 1
        if expected > 1:
            # 多 flag 题：planner 上下文必须带进度，避免过早收工
            parts.append(f"flag 进度：本题共需 {expected} 面，已拿 {len(self.gate.flags)} 面——未凑齐前继续派活")
        legacy = getattr(self, "_race_legacy", None)
        if legacy:
            parts.append("race 阶段已探明（冲刺阶段结构化交接）：\n" + "\n".join(legacy))
        return "\n".join(parts)

    #: goal 文本里的占位主机名特征（出现即拒收该 intent）
    _PLACEHOLDER_GOAL_RE = re.compile(r"https?://target\b|//target:|example\.com|占位|<[^>]+>")

    async def _plan_once(self) -> None:
        request_id = f"planner:{next(self._planner_seq)}"
        started = time.monotonic()
        reasoning_chars = 0
        content_chars = 0
        self.emit(ModelRequestStarted(request_id=request_id, turn=0, step=0, worker="planner"))

        def on_delta(text: str) -> None:
            nonlocal content_chars
            content_chars += len(text)

        def on_reasoning_delta(text: str) -> None:
            nonlocal reasoning_chars
            reasoning_chars += len(text)
            if not self.interrupt.is_set():
                self.emit(AgentReasoningDelta(request_id=request_id, delta=text, worker="planner"))

        try:
            result = await self.planner.plan(
                self._planning_context(),
                self.board.summary(),
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        except asyncio.CancelledError:
            self.emit(
                ModelRequestComplete(
                    request_id=request_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    ok=False,
                    error="cancelled",
                    reasoning_chars=reasoning_chars,
                    content_chars=content_chars,
                    worker="planner",
                )
            )
            raise
        except Exception as exc:
            self.emit(
                ModelRequestComplete(
                    request_id=request_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    reasoning_chars=reasoning_chars,
                    content_chars=content_chars,
                    worker="planner",
                )
            )
            raise
        self.emit(
            ModelRequestComplete(
                request_id=request_id,
                duration_ms=round((time.monotonic() - started) * 1000),
                reasoning_chars=reasoning_chars,
                content_chars=content_chars,
                worker="planner",
            )
        )
        created: list[str] = []
        for spec in result.intents:
            if self._PLACEHOLDER_GOAL_RE.search(spec.goal):
                # 占位地址 intent 直接拒收，别让它上板浪费 worker
                self._record({"type": "event", "kind": "intent_rejected", "goal": spec.goal[:120]})
                self.emit(SwarmNote(message=f"intent 含占位地址，已拒收：{spec.goal[:60]}"))
                continue
            ok, info = self.board.create_intent(
                spec.goal, spec.category, spec.surface, spec.technique, lane=spec.lane, actor="planner"
            )
            if ok:
                created.append(info)
        for fid in result.audit:
            self.board.reject_fact(fid, actor="planner")
        self._record(
            {
                "type": "event",
                "kind": "swarm_plan",
                "verdict": result.verdict,
                "created": created,
                "audit": result.audit,
            }
        )
        if created:
            self.emit(SwarmNote(message=f"planner 新派 {len(created)} 个 intent: {', '.join(created)}"))
        self._last_plan_created = len(created)
        self._last_plan_verdict = result.verdict
        self._last_plan_summary = result.summary.strip()
        # 并发上限由用户配置的 workers 决定；planner 只负责产出可执行 intent。
        # 实际派发仍由 open_intents 数量限制，不会创建空转 worker。
        self.desired_workers = self.workers

    # ------------------------------------------------------------ worker
    async def _run_worker(self, worker_id: str) -> None:
        intent = self.board.claim_intent(worker_id)
        if intent is None:
            return
        self._worker_intents[worker_id] = intent["id"]
        self.emit(SwarmNote(message=f"认领 {intent['id']}：{intent['goal'][:60]}", worker=worker_id))
        self._record({"type": "event", "kind": "worker_start", "worker": worker_id, "intent": intent["id"]})
        # If this worker_id has a model-specific provider, use it instead of the default
        provider = None
        for label, model_name, ep_provider in getattr(self.factory, "extra_providers", []):
            if worker_id.startswith(label + "-"):
                provider = ep_provider
                break
        loop = self.factory.make_loop(worker_id, provider=provider)
        target_line = (
            f"\n真实靶机地址：{self.target}（intent 描述里的地址若与此不一致，一律以这里为准）"
            if self.target
            else ""
        )
        history = [
            system_message(f"{WORKER_SYSTEM}\n\n{self.board.summary()}"),
            user_message(
                f"领取任务 intent {intent['id']}：{intent['goal']}{target_line}\n"
                + (
                    f"本题共需 {self.gate.expected_flags} 面 flag，已拿 {len(self.gate.flags)} 面；"
                    if self.gate is not None and self.gate.expected_flags > 1
                    else ""
                )
                + "完成后 complete_intent 汇报；拿到 flag 直接 submit_flag。"
            ),
        ]
        report = ""
        cancelled = False
        try:
            await loop.run_turn(history, turn_no=1)
            report = last_assistant_text(history)
        except asyncio.CancelledError:
            # 外部抢占（spectator 纠偏 / 凑齐 flag / operator kill / 会话退出）：
            # 不是 worker 自己"跑完没汇报"，收尾时不发"完成（无汇报）"误导。
            cancelled = True
            raise
        except BudgetExceeded:
            pass  # 共享预算耗尽，coordinator 主循环统一收口
        except Exception as exc:  # noqa: BLE001 - worker 异常不炸协调循环
            self.emit(Error(message=f"{worker_id} 异常: {type(exc).__name__}: {exc}", worker=worker_id))
            report = f"异常: {exc}"
        finally:
            self._worker_intents.pop(worker_id, None)
            if self.gate.solved:
                # flag 已出：无论是否被取消，收尾都算 done（避免把已解出的 intent 记 barren）
                self.board.finish_claimed(intent["id"], worker_id, "done", "[flag 已出，收尾]")
                self._record({"type": "event", "kind": "worker_done", "worker": worker_id, "intent": intent["id"]})
                self.emit(SwarmNote(message=f"完成 {intent['id']}（flag 已出）", worker=worker_id))
            elif cancelled:
                # 被抢占（spectator 纠偏 / operator kill / 会话退出）：认领状态由抢占方收口；
                # 它没收口时才兜底标记，且措辞是"被抢占中断"而非"完成（无汇报）"。
                claimed = self.board.finish_claimed(
                    intent["id"], worker_id, "barren", "[worker 被抢占中断]"
                )
                if claimed:
                    self.emit(SwarmNote(message=f"{intent['id']} 被抢占中断（未完成）", worker=worker_id))
            else:
                # agent 没自己 complete_intent 的兜底：有汇报记 done，没汇报记 barren
                status = "done" if report.strip() else "barren"
                self.board.finish_claimed(
                    intent["id"], worker_id, status, f"[worker 自动收尾] {report[:200]}"
                )
                self._record({"type": "event", "kind": "worker_done", "worker": worker_id, "intent": intent["id"]})
                self.emit(SwarmNote(message=f"完成 {intent['id']}（{report[:60] or '无汇报'}）", worker=worker_id))

    def _next_worker_id(self) -> str:
        """Generate worker ID with model label if extra providers exist."""
        seq = next(self._worker_seq)
        extras = getattr(self.factory, "extra_providers", [])
        if not extras:
            return f"worker-{seq}"
        label, model_name, _ = extras[self._extra_provider_idx % len(extras)]
        self._extra_provider_idx += 1
        return f"{label}-{seq}"

    async def _watch_gate(self) -> None:
        """按预期 flag 数收口（0.2s 轮询；finally 里统一取消兜底）：
        第 k 面过闸且 k < expected → 不杀任何 worker，发"继续"通告；
        k == expected（凑齐）→ 立即 cancel 全部在跑 worker 并通告。"""
        if self.gate is None:
            return
        last_count = 0
        while not self.gate.solved and not self.interrupt.is_set():
            count = len(getattr(self.gate, "flags", []) or [])
            expected = getattr(self.gate, "expected_flags", 1) or 1
            if count > last_count and not self.gate.solved:
                last_count = count
                self.emit(
                    SwarmNote(message=f"第 {count}/{expected} 面 flag 过闸，继续")
                )
            await asyncio.sleep(0.2)
        if not self.gate.solved:
            return
        killed = 0
        for _wid, task in list(self._worker_tasks.items()):
            if not task.done():
                task.cancel()
                killed += 1
        if killed:
            expected = getattr(self.gate, "expected_flags", 1) or 1
            self.emit(
                SwarmNote(message=f"{expected} 面 flag 已全部到手，终止 {killed} 个在跑 worker")
            )

    # ------------------------------------------------------------ 主循环
    async def solve(self) -> int:
        try:
            code, used_minutes, used_tokens = await self._race()
        except Exception as exc:  # noqa: BLE001 - race 阶段异常降级进 swarm，不整局报废
            self.emit(SwarmNote(message=f"race 异常（降级进 swarm）: {type(exc).__name__}: {exc}"))
            code, used_minutes, used_tokens = None, 0.0, 0
        if code is not None:
            return code
        await self._collect_race_legacy()  # race 无旗收场：遗产统一上板 + 进 planner 上下文

        remaining = Budget(
            # 无限预算（None）不做扣减接力，直接延续
            max_minutes=(max(0.01, self.budget.max_minutes - used_minutes) if self.budget.max_minutes is not None else None),
            max_tokens=(self.budget.max_tokens - used_tokens) if self.budget.max_tokens is not None else None,
            max_turns=self.budget.max_turns,  # worker 单发单 turn，不计入主 turns
        )
        state = BudgetState(remaining)
        self.factory.budget_state = state  # 全体 worker 共享账本

        last_version = -1  # 首拍强制规划一次
        last_plan = 0.0

        # flag 过闸守望者：solved 立即取消在跑 worker（不等下一拍/长命令自然停）
        solved_watcher = asyncio.create_task(self._watch_gate())

        try:
            while not self.gate.solved and not self.interrupt.is_set():
                try:
                    state.check()
                except BudgetExceeded:
                    return EXIT_BUDGET
                if self._drain_steer():
                    last_version = -1
                    last_plan = 0.0
                workers = self._worker_tasks
                # 收割完成的 worker
                for wid, task in list(workers.items()):
                    if task.done():
                        workers.pop(wid)
                # 黑板有变化且过冷却 → 规划
                version = self.board.version()
                if version != last_version:
                    if time.monotonic() - last_plan >= self.planner_cooldown:
                        last_version = version
                        try:
                            await self._plan_once()
                            spectator_summary = await self._spectator_once()
                            if spectator_summary:
                                self._last_plan_summary = spectator_summary
                                self.final_summary = spectator_summary
                                self.emit(SwarmNote(message="spectator 确认任务完成\n" + spectator_summary))
                                return EXIT_COMPLETE
                        except Exception as exc:  # noqa: BLE001 - planner 网络/解析故障不炸主循环
                            # 实战翻车：DeepSeek 侧 RemoteProtocolError 逃逸导致整个 solve 崩溃
                            self.emit(SwarmNote(message=f"planner 异常（下一拍重试）: {type(exc).__name__}: {exc}"))
                            last_version = -1  # 强制下拍重试
                        finally:
                            # Cooldown is measured after the request completes. A
                            # multi-minute planner call must not immediately launch
                            # another request when it returns empty.
                            last_plan = time.monotonic()
                        if (
                            self._last_plan_created == 0
                            and not workers
                            and not self.board.open_intents()
                            and not self.gate.solved
                        ):
                            if self._last_plan_verdict == "complete":
                                summary = self._last_plan_summary or self.board.summary()
                                self.final_summary = summary
                                self.emit(SwarmNote(message="planner 确认调查完成\n" + summary))
                                return EXIT_COMPLETE
                            last_version = -1
                            self.emit(SwarmNote(message=(
                                f"planner 未派出可执行 intent（verdict={self._last_plan_verdict}），"
                                f"{self.planner_cooldown:g}s 后重新规划"
                            )))
                # 平滑扩容（放宽版）：空槽且有货，每拍最多补 spawn_batch 个，尽快填满
                free = self.desired_workers - len(workers)
                if free > 0:
                    for _ in range(min(self.spawn_batch, free, len(self.board.open_intents()))):
                        wid = self._next_worker_id()
                        workers[wid] = asyncio.create_task(self._run_worker(wid))
                await asyncio.sleep(self.tick)
        finally:
            if self.gate.solved and not solved_watcher.done():
                # 给守望者一个抢先窗口（0.2s 轮询比 tick 快）：由它取消+发通告
                try:
                    await asyncio.wait_for(asyncio.shield(solved_watcher), timeout=0.5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            solved_watcher.cancel()
            try:
                await solved_watcher
            except asyncio.CancelledError:
                pass
            for task in self._worker_tasks.values():
                task.cancel()
            if self._worker_tasks:
                await asyncio.gather(*self._worker_tasks.values(), return_exceptions=True)
            self._worker_tasks.clear()
            self._worker_intents.clear()
        if self.interrupt.is_set() and not self.gate.solved:
            return EXIT_ERROR  # 用户 Esc 中断
        return EXIT_SOLVED

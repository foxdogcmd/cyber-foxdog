"""solve 运行装配：build_run 搭好全部组件，execute_run 跑到底并清理。

headless（cli.py EventRenderer）与 TUI（tui.py / interactive.py）共用同一条
装配线：差别只在 emit（事件消费者）与 steer 来源（stdin 线程 / TUI 输入框）。

交互式会话（interactive.py）用 SessionContext 会话级共享 provider/权限链/
env/技能注册表/MCP 连接，并把持续对话 history 传入 build_run——每次任务一个
独立 run（Workspace/FlagGate/Blackboard 题目级隔离），对话历史延续。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import AppConfig, build_endpoint_provider, build_provider, build_role_provider, load_config
from .llm.client import ModelEndpoint as _ModelEndpoint
from .core import context as ctx_mod
from .core.compaction import CompactionManager
from .core.envprobe import SECURITY_TOOLS, EnvFacts, probe_env, probe_runtime_env
from .core.goal import EXIT_BUDGET, EXIT_ERROR, EXIT_SOLVED, Budget, GoalMode
from .core.loop import AgentLoop
from .core.orchestrator import ToolOrchestrator
from .core.permission import PermissionChain
from .core.prompts import build_system_prompt, build_user_prompt
from .core.steer import SteerQueue
from .executor.background import BackgroundRegistry
from .executor.runtime import ExecutionRuntime, create_runtime
from .gate.flag_gate import FlagGate
from .llm.client import LLMClient, ModelEndpoint
from .llm.messages import system_message, user_message
from .protocol.types import Error, Event, SwarmNote
from .recipes.loader import Recipe, RecipeManager, load_recipes
from .recipes.tool import RecipeHelpTool, SelectToolsTool
from .skills.loader import SkillRegistry, load_skills
from .skills.tool import ActivateSkillTool
from .store.rollout import RolloutWriter
from .store.workspace import Workspace
from .store.writeup import generate_writeup
from .swarm.coordinator import Coordinator
from .swarm.planner import ReasonPlanner
from .swarm.worker import WorkerFactory
from .tools import ToolContext, ToolRegistry, register_builtin_tools
from .tools.builtin.subagent import ListAgentsTool, SpawnAgentTool, SubAgentManager, WaitAgentTool


@dataclass
class SessionContext:
    """交互式会话级共享组件（interactive.py 一次装配，多任务复用）。"""

    cfg: AppConfig
    provider: Any  # ChatProvider
    env: EnvFacts
    skill_registry: SkillRegistry
    permission: PermissionChain
    mcp_manager: Any = None  # McpManager | None（会话级连接，会话结束统一 close）


async def build_session(
    mode: str = "",
    planner_model: str | None = None,
    overrides: list[str] | None = None,
) -> SessionContext:
    """装配会话级组件；MCP 连接失败不致命（warnings 通过返回值附带）。"""
    cfg = load_config(list(overrides or []))
    if mode:
        cfg.agent.mode = mode
    if planner_model:
        cfg.planner = ModelEndpoint(**{**cfg.solver.model_dump(), "model": planner_model})
    provider = build_provider(cfg)
    env = probe_env()
    # 技能可用性检测需要一个含内置工具的 registry
    probe_registry = ToolRegistry()
    register_builtin_tools(probe_registry)
    skill_registry = SkillRegistry(
        load_skills(lambda name: probe_registry.get(name) is not None or shutil.which(name) is not None)
    )
    permission = PermissionChain(mode=cfg.agent.mode)
    mcp_manager = None
    if cfg.mcp_servers:
        from .mcp_client import McpManager  # 懒加载：未配置零开销

        mcp_manager = McpManager()
        await mcp_manager.connect_all(cfg.mcp_servers)
        if not mcp_manager.names:
            await mcp_manager.close()
            mcp_manager = None  # 全挂了等同未配置
    return SessionContext(
        cfg=cfg,
        provider=provider,
        env=env,
        skill_registry=skill_registry,
        permission=permission,
        mcp_manager=mcp_manager,
    )


@dataclass
class SolveRun:
    """一次 solve 的全部运行期组件（TUI 状态面板直接读这些字段）。"""

    objective: str
    cfg: AppConfig
    ws: Workspace
    emit: Callable[[Event], None]
    steer: SteerQueue
    rollout: RolloutWriter
    gate: FlagGate
    background: BackgroundRegistry
    runtime: ExecutionRuntime
    ctx: ToolContext
    registry: ToolRegistry
    permission: PermissionChain
    loop: AgentLoop
    goal: GoalMode
    budget: Budget
    history: list[dict[str, Any]]
    subagents: SubAgentManager | None = None
    workers: int = 1
    coordinator: Coordinator | None = None
    mcp_manager: Any = None  # McpManager（未配置 MCP 时 None）
    owns_mcp: bool = True  # 会话级共享时 False（execute_run 不负责 close）
    interrupted: bool = False  # 用户 Esc 中断标记（TUI 设置，摘要区分"中断"与"异常"）
    engine_labels: dict[str, str] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    writeup_path: Path | None = None

    @property
    def active_engine_labels(self) -> tuple[str, ...]:
        """Names of engines currently doing solve work, for status UIs."""
        names: tuple[str, ...] = ()
        if self.coordinator is not None:
            names = self.coordinator.active_race_engines
        if not names:
            names = ("solver",)
        labels = [self.engine_labels.get(name, name.removeprefix("engine-")) for name in names]
        return tuple(dict.fromkeys(labels))


async def _prepare_execution_runtime(
    cfg: AppConfig,
    ws: Workspace,
    emit: Callable[[Event], None],
    session_env: EnvFacts | None,
) -> tuple[ExecutionRuntime, EnvFacts, list[Recipe], list[str], set[str] | None]:
    """Start one run runtime and discover commands in that exact environment."""
    runtime_cfg = cfg.runtime
    runtime = create_runtime(
        runtime_cfg.kind,
        workspace=ws.dir,
        image=runtime_cfg.image,
        docker_binary=runtime_cfg.docker_binary,
        network=runtime_cfg.network,
        mounts=runtime_cfg.mounts,
        auto_build=runtime_cfg.auto_build,
        auto_remove=runtime_cfg.auto_remove,
        workdir=runtime_cfg.workdir,
        status_callback=lambda message: emit(SwarmNote(message=message)),
    )
    try:
        if runtime.kind != "local":
            emit(SwarmNote(message=f"启动 Docker 工具环境：{runtime_cfg.image}（{runtime.kind}）"))
        await runtime.start()

        if runtime.kind == "local":
            env = session_env or probe_env()
            recipes, skipped = load_recipes()
            return runtime, env, recipes, skipped, None

        # Parse once without filtering so user recipes are included in the
        # container PATH probe, then load again with the discovered availability.
        candidates, _ = load_recipes(command_available=lambda _command: True)
        commands = list(dict.fromkeys([*SECURITY_TOOLS, *(r.spec.command for r in candidates)]))
        env = await probe_runtime_env(runtime, commands)
        available = set(env.tools)
        recipes, skipped = load_recipes(command_available=lambda command: command in available)
        emit(
            SwarmNote(
                message=(
                    f"Docker 工具环境就绪：{runtime_cfg.workdir}，"
                    f"检测到 {len(available)} 个外部命令"
                )
            )
        )
        return runtime, env, recipes, skipped, available
    except BaseException:
        await runtime.close()
        raise


async def build_run(
    objective: str,
    target: str | None,
    flag_format: str | None,
    attach: list[Path] | None,
    mode: str,
    max_minutes: float | None,
    max_tokens: int | None,
    max_turns: int | None,
    workers: int,
    planner_model: str | None,
    overrides: list[str],
    emit: Callable[[Event], None],
    steer: SteerQueue | None = None,
    workspace: Path | None = None,
    session: SessionContext | None = None,
    history: list[dict[str, Any]] | None = None,
    expected_flags: int | None = None,
    engines_enabled: bool | None = None,
    resume_from: Path | None = None,
) -> SolveRun:
    """装配一次 solve 的全部组件（不开跑）。

    session：交互式会话传入（共享 provider/权限/env/技能/MCP）；缺省全部新建
    （headless 路径行为与 M2 以来完全一致）。
    history：交互式会话传入持续对话历史（系统提示重建、任务 prompt 追加在尾部）；
    缺省新建 [system, user] 两条。
    """
    if session is not None:
        cfg = session.cfg
        provider = session.provider
        session_env = session.env
        skill_registry = session.skill_registry
        permission = session.permission
        mcp_manager = session.mcp_manager
        owns_mcp = False
    else:
        cfg = load_config(overrides)
        if mode:
            cfg.agent.mode = mode
        if planner_model:
            # --planner-model 覆盖：沿用 solver 的端点/密钥，只换模型名
            cfg.planner = ModelEndpoint(**{**cfg.solver.model_dump(), "model": planner_model})
        provider = build_provider(cfg)
        session_env = None
        skill_registry = None  # 下面统一建
        permission = PermissionChain(mode=cfg.agent.mode)
        mcp_manager = None
        owns_mcp = True

    if engines_enabled is not None:
        cfg.engines.enabled = engines_enabled

    ws = Workspace(base=workspace, seed_from=resume_from)
    attachments: list[str] = []
    for src in attach or []:
        dst = ws.inputs_dir / src.name
        shutil.copy2(src, dst)
        attachments.append(src.name)

    # fake provider 冒烟：预置"目标侧"flag 文件（模拟 flag 由目标系统产出，
    # agent 只能 cat/type 读到——直接 echo 进输出的假旗会被 FlagGate 污染扫描拒收）
    if cfg.solver.provider == "fake":
        fake_flag = os.environ.get("FOXDOG_FAKE_FLAG", "flag{smoke_test_flag_123}")
        (ws.work_dir / "fake_target_flag.txt").write_text(f"congrads! {fake_flag}\n", encoding="utf-8")

    rollout = RolloutWriter(ws.dir / "rollout.jsonl")
    await rollout.start(
        {
            "run_id": ws.run_id,
            "cwd": str(Path.cwd()),
            "objective": objective,
            "target": target,
            "flag_format": flag_format,
            "model": cfg.solver.model,
            "provider": cfg.solver.provider,
            "mode": cfg.agent.mode,
            "workers": workers,
            "runtime": cfg.runtime.kind,
            "runtime_image": cfg.runtime.image if cfg.runtime.kind != "local" else None,
            "ts": time.time(),
        }
    )

    runtime: ExecutionRuntime | None = None
    try:
        if session is None and cfg.mcp_servers:
            from .mcp_client import McpManager  # 懒加载：未配置零开销

            mcp_manager = McpManager()
            for name, warning in (await mcp_manager.connect_all(cfg.mcp_servers)).items():
                if warning:
                    emit(SwarmNote(message=f"MCP server {name} 连接失败（不致命）：{warning}"))
            if not mcp_manager.names:
                await mcp_manager.close()
                mcp_manager = None  # 全挂了等同未配置

        runtime, env, recipes, skipped, runtime_commands = await _prepare_execution_runtime(
            cfg,
            ws,
            emit,
            session_env,
        )
    except BaseException:
        if runtime is not None:
            await runtime.close()
        if mcp_manager is not None and owns_mcp:
            await mcp_manager.close()
        await rollout.close()
        raise

    registry = ToolRegistry()
    register_builtin_tools(registry)
    gate = FlagGate(
        ws,
        flag_format=flag_format,
        # 多 flag 题：CLI --expect-flags > [platform].expected_flags > 1
        expected_flags=expected_flags if expected_flags is not None else (cfg.platform.expected_flags or 1),
    )
    background = BackgroundRegistry()
    ctx = ToolContext(
        workspace=ws,
        background=background,
        emit=emit,
        fetch_output=lambda h, o, l: ctx_mod.fetch_output(ws, h, o, l),
        flag_gate=gate,
        runtime=runtime,
    )

    # 配方（渐进披露）+ 技能（渐进披露）+ MCP server（会话级共享或本 run 自建）
    recipe_manager = RecipeManager(recipes, skipped)
    registry.register(SelectToolsTool(recipe_manager, registry, mcp_manager))
    registry.register(RecipeHelpTool(recipe_manager))
    if runtime.kind != "local":
        # SessionContext was probed on the host before a run workspace/container
        # existed, so Docker runs need their own availability-marked registry.
        skill_registry = None
    if skill_registry is None:
        def tool_available(name: str) -> bool:
            if registry.get(name) is not None:
                return True
            if runtime_commands is not None:
                return name in runtime_commands
            return shutil.which(name) is not None

        skill_registry = SkillRegistry(
            load_skills(tool_available)
        )
    registry.register(ActivateSkillTool(skill_registry))
    if skipped:
        emit(SwarmNote(message=f"配方跳过：{'、'.join(skipped)}"))

    orchestrator = ToolOrchestrator(registry, permission, ctx)

    # 上下文压缩：planner/便宜模型优先，无配置用 solver 模型
    compact_provider = build_role_provider(cfg.planner_endpoints, cfg.planner) if cfg.planner_endpoints else (build_endpoint_provider(cfg.planner) if cfg.planner is not None else provider)
    todo_tool = registry.get("todo_list")

    def render_state() -> str:
        parts = []
        if todo_tool is not None:
            parts.append("当前 todo 计划：\n" + todo_tool.render())  # type: ignore[attr-defined]
        parts.append(ctx.facts.render())
        return "\n\n".join(parts)

    compaction = CompactionManager(
        compact_provider,
        window_tokens=cfg.agent.context_window,
        state_renderer=render_state,
    )

    run_interrupt = asyncio.Event()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        orchestrator=orchestrator,
        emit=emit,
        rollout=rollout,
        flag_gate=gate,
        max_steps=cfg.agent.max_steps,
        compaction=compaction,
        interrupt=run_interrupt,
    )
    budget = Budget(
        max_minutes=max_minutes if max_minutes is not None else cfg.budget.max_minutes,
        max_tokens=max_tokens if max_tokens is not None else cfg.budget.max_tokens,
        max_turns=max_turns if max_turns is not None else cfg.budget.max_turns,
    )

    # 每 turn 开始重渲染系统提示：环境事实 + 技能目录 + 事实板最新摘要
    def refresh_system(history: list, turn_no: int) -> None:
        history[0] = system_message(build_system_prompt(env, skill_registry.list(), ctx.facts))

    steer_q = steer or SteerQueue()
    goal = GoalMode(loop, budget, on_turn_start=refresh_system, steer=steer_q)

    # 子 agent 工具（主 agent 可扇出；子 agent registry 剥掉 spawn 系→深度上限 1）
    # Build extra providers for multi-solver: additional solver profiles
    # become worker assignments so different model suppliers can cooperate.
    extra_providers: list[tuple[str, str, Any]] = []
    for _name, _ep in cfg.solver_endpoints.items():
        if _name == "solver":
            continue
        _label = _ep.model or _ep.provider or _name
        _provider = build_endpoint_provider(_ep)
        extra_providers.append((_label, _ep.model or _label, _provider))
    # Also include legacy extra endpoints as worker models
    for _name, _ep in (cfg.extra or {}).items():
        _label = _ep.model or _ep.provider or _name
        _provider = build_endpoint_provider(_ep)
        extra_providers.append((_label, _ep.model or _label, _provider))
    factory = WorkerFactory(
        provider,
        registry,
        permission,
        ctx,
        emit,
        rollout,
        gate,
        max_steps=cfg.agent.max_steps,
        interrupt=run_interrupt,
        extra_providers=extra_providers,
    )
    subagent_manager = SubAgentManager(factory, ctx.facts)
    registry.register(SpawnAgentTool(subagent_manager))
    registry.register(WaitAgentTool(subagent_manager))
    registry.register(ListAgentsTool(subagent_manager))

    if history is None:
        history = [
            system_message(build_system_prompt(env, skill_registry.list(), ctx.facts)),
            user_message(build_user_prompt(objective, target, flag_format, attachments)),
        ]
    else:
        # 交互式会话：对话历史延续，系统提示换最新（新黑板/技能/环境），任务追加尾部
        fresh_system = system_message(build_system_prompt(env, skill_registry.list(), ctx.facts))
        if history and history[0].get("role") == "system":
            history[0] = fresh_system
        else:
            history.insert(0, fresh_system)
        history.append(user_message(build_user_prompt(objective, target, flag_format, attachments)))

    coordinator = None
    engine_labels = {"solver": cfg.solver.model or cfg.solver.provider or "未配置"}
    # Multi-solver: additional profiles from cfg.solver_endpoints are shown
    # as distinct engines so the TUI status bar reflects who is working.
    for _name, _ep in cfg.solver_endpoints.items():
        if _name != "solver":
            engine_labels[f"engine-{_name}"] = _ep.model or _ep.provider or _name
    if workers > 1:
        # swarm 模式：race 快路径 + OODA 协调循环（预算整体共享）
        planner_provider = build_role_provider(cfg.planner_endpoints, cfg.planner) if cfg.planner_endpoints else (build_endpoint_provider(cfg.planner) if cfg.planner is not None else provider)
        # 旁观者：统揽全局、审查方向、纠偏、判定收口、完成后其结论写进 writeup。
        # 有专用 [model.spectator] 端点则用之；否则复用 planner 模型（swarm 模式默认启用）。
        from .swarm.spectator import Spectator
        spectator_provider = build_endpoint_provider(cfg.spectator) if cfg.spectator is not None else planner_provider
        spectator = Spectator(spectator_provider)
        coordinator = Coordinator(
            board=ctx.facts,
            planner=ReasonPlanner(planner_provider),
            factory=factory,
            emit=emit,
            rollout=rollout,
            gate=gate,
            objective=objective,
            budget=budget,
            target=target,
            flag_format=flag_format,
            workers=workers,
            history=history,
            race_goal=goal,
            steer=steer_q,
            interrupt=run_interrupt,
            spectator=spectator,
        )
        # 异构 race 引擎：model.extra 端点 × N + claude CLI（探测/上限见 engines.plan_race_engines）
        if cfg.engines.enabled:
            from .swarm.engines import plan_race_engines

            claude_cfg = cfg.engines.claude_code
            claude_env: dict[str, str] = {}
            pre_notes: list[str] = []
            claude_path = ""
            if claude_cfg.enabled:
                if claude_cfg.containerized:
                    if runtime.kind == "local":
                        pre_notes.append("Claude Code 配置为容器运行，但当前 runtime=local，跳过该引擎")
                    else:
                        claude_path = await runtime.which("claude") or ""
                else:
                    claude_path = shutil.which("claude") or ""
                    pre_notes.append("Claude Code 使用宿主兼容模式（未沙箱化）")
                if claude_path and claude_cfg.import_host_config:
                    from .swarm.claude_config import ClaudeConfigError, load_claude_code_env

                    try:
                        claude_env = load_claude_code_env()
                    except ClaudeConfigError as exc:
                        pre_notes.append(f"Claude Code 本机配置导入失败，跳过该引擎：{exc}")
                        claude_path = ""
                    else:
                        pre_notes.append(
                            f"Claude Code 已注入本机配置：{', '.join(sorted(claude_env))}（值不记录）"
                        )
            specs, engine_notes = plan_race_engines(cfg, claude_path=claude_path)
            for note in pre_notes:
                emit(SwarmNote(message=note))
            for note in engine_notes:
                emit(SwarmNote(message=note))
            engine_runners: list[tuple[str, Any, Any]] = []
            for spec in specs:
                engine_labels[spec.name] = spec.display_name
                if spec.kind == "model":
                    engine_provider = _build_endpoint_provider(spec.endpoint)
                    engine_factory = WorkerFactory(
                        engine_provider, registry, permission, ctx, emit, rollout, gate,
                        max_steps=cfg.agent.max_steps,
                    )
                    engine_loop = engine_factory.make_loop(spec.name)
                    engine_history = [
                        history[0],
                        user_message(
                            build_user_prompt(objective, target, flag_format, attachments)
                            + f"\n你是引擎 {spec.name}（异构竞速）：独立解题，别等其他 worker；其他引擎也在并行打同一题。重要发现立刻 post_fact 上板，别只写在回复里。"
                        ),
                    ]
                    engine_goal = GoalMode(engine_loop, Budget())
                    engine_goal.history_ref = engine_history  # race 遗产交接用（coordinator 收集末轮汇报）
                    engine_runners.append(
                        (spec.name, (lambda g=engine_goal, h=engine_history: g.solve(h)), engine_goal)
                    )
                else:
                    from .swarm.cli_engine import CliEngine

                    cli_engine = CliEngine(
                        spec.name, spec.cli, ws, gate, emit,
                        total_timeout=max(60.0, coordinator.race_minutes * 60 + 30),
                        runtime=runtime if claude_cfg.containerized else None,
                        env=claude_env,
                    )
                    engine_runners.append((spec.name, (lambda e=cli_engine: e.run(objective, target, flag_format)), None))
            coordinator.race_engines = engine_runners

    run = SolveRun(
        objective=objective,
        cfg=cfg,
        ws=ws,
        emit=emit,
        steer=steer_q,
        rollout=rollout,
        gate=gate,
        background=background,
        runtime=runtime,
        ctx=ctx,
        registry=registry,
        permission=permission,
        loop=loop,
        goal=goal,
        budget=budget,
        history=history,
        subagents=subagent_manager,
        workers=workers,
        coordinator=coordinator,
        mcp_manager=mcp_manager,
        owns_mcp=owns_mcp,
        engine_labels=engine_labels,
    )
    return run


async def _platform_submit(run: SolveRun) -> None:
    """统一平台闭环：FlagGate 验证通过后自动 POST flag（5s 超时，失败只警告不炸）。"""
    url = run.cfg.platform.submit_url
    if not url:
        return
    payload = {"flag": run.gate.flag, "flags": list(run.gate.flags), "run_id": run.ws.run_id}
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            resp = await client.post(url, json=payload)
        run.emit(SwarmNote(message=f"flag 已自动提交平台（HTTP {resp.status_code}）"))
    except Exception as exc:  # noqa: BLE001 - 提交失败不影响本地 solved 结论
        run.emit(SwarmNote(message=f"平台自动提交失败（不致命）：{type(exc).__name__}: {exc}"))


async def execute_run(run: SolveRun) -> int:
    """跑 solve 到底；无论成败清理资源 + 生成 writeup。"""
    try:
        if run.coordinator is not None:
            code = await run.coordinator.solve()
        else:
            code = await run.goal.solve(run.history)
    except KeyboardInterrupt:
        code = EXIT_ERROR
    except asyncio.CancelledError:
        if run.interrupted:
            # Ctrl+C/Esc is a completed manual interruption, not an application
            # shutdown. Swallow only that cancellation so the normal summary and
            # writeup path still run after cleanup.
            code = EXIT_ERROR
        else:
            raise  # TUI 退出取消：finally 清理后照常传播
    except Exception as exc:  # noqa: BLE001
        run.emit(Error(message=f"未处理异常: {type(exc).__name__}: {exc}"))
        code = EXIT_ERROR
    finally:
        if run.subagents is not None:
            await run.subagents.stop_all()
        await run.ctx.sessions.close_all()
        await run.background.stop_all()
        await run.runtime.close()
        if run.mcp_manager is not None and run.owns_mcp:
            await run.mcp_manager.close()  # 会话级共享的 MCP 由会话方统一关闭
        await run.rollout.close()
        run.ctx.facts.close()

    if code == EXIT_SOLVED and run.cfg.platform.submit_url:
        await _platform_submit(run)
    # solve 结束（无论成败）生成 writeup 骨架；complete 时带上收口结论
    final_summary = ""
    if run.coordinator is not None:
        final_summary = getattr(run.coordinator, "final_summary", "") or ""
    run.writeup_path = generate_writeup(run.ws.dir, final_summary)
    return code


def _build_endpoint_provider(ep: "_ModelEndpoint"):
    """按端点配置构造 ChatProvider（三种协议；race 引擎用）。"""
    return build_endpoint_provider(ep)


__all__ = [
    "SolveRun", "SessionContext", "build_run", "build_session", "execute_run",
    "EXIT_SOLVED", "EXIT_BUDGET", "EXIT_ERROR",
]

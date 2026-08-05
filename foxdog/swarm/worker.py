"""WorkerFactory：按 worker_id 造子 AgentLoop（子 agent 与 coordinator worker 共用）。

共享：provider、权限链、工作区、后台任务、黑板、FlagGate、rollout。
独立：registry（剥掉 spawn 系工具→深度上限 1）、ToolContext/emit（事件带
worker 前缀）、history。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..core.loop import AgentLoop
from ..core.orchestrator import ToolOrchestrator
from ..core.permission import PermissionChain
from ..gate.flag_gate import FlagGate
from ..llm.client import ChatProvider
from ..protocol.types import Event
from ..store.rollout import RolloutWriter
from ..tools import ToolContext, ToolRegistry

#: 子 agent registry 剥掉的工具（深度上限：子 agent 不能再扇出）
SPAWN_TOOLS = {"spawn_agent", "wait_agent", "list_agents"}


def with_worker(event: Event, worker_id: str) -> Event:
    """给事件打上 worker 前缀（无 worker 字段的事件类型原样返回）。"""
    if hasattr(event, "worker"):
        return event.model_copy(update={"worker": worker_id})
    return event


class WorkerFactory:
    def __init__(
        self,
        provider: ChatProvider,
        base_registry: ToolRegistry,
        permission: PermissionChain,
        base_ctx: ToolContext,
        emit: Callable[[Event], None],
        rollout: RolloutWriter | None,
        flag_gate: FlagGate | None,
        max_steps: int = 200,
        exclude_tools: set[str] | None = None,
        interrupt: asyncio.Event | None = None,
        extra_providers: list[tuple[str, str, ChatProvider]] | None = None,
    ) -> None:
        self.provider = provider
        self.base_registry = base_registry
        self.permission = permission
        self.base_ctx = base_ctx
        self.emit = emit
        self.rollout = rollout
        self.flag_gate = flag_gate
        self.max_steps = max_steps
        self.exclude_tools = exclude_tools if exclude_tools is not None else set(SPAWN_TOOLS)
        self.interrupt = interrupt
        #: coordinator 开跑前注入的共享预算账本（各 worker loop 每 step 前检查）
        self.budget_state = None
        #: (label, model_name, provider) for each extra model endpoint;
        #: the coordinator assigns these to workers in round-robin order.
        self.extra_providers = extra_providers or []

    def make_loop(self, worker_id: str, provider: ChatProvider | None = None) -> AgentLoop:
        """造一个子 agent loop：独立 registry/history，事件与 rollout 记录带 worker。"""
        active_provider = provider if provider is not None else self.provider
        emit = self.emit

        def worker_emit(event: Event) -> None:
            emit(with_worker(event, worker_id))

        ctx = ToolContext(
            workspace=self.base_ctx.workspace,
            background=self.base_ctx.background,
            emit=worker_emit,
            fetch_output=self.base_ctx.fetch_output,
            flag_gate=self.flag_gate,
            sessions=self.base_ctx.sessions,
            facts=self.base_ctx.facts,
            runtime=self.base_ctx.runtime,
        )
        registry = self.base_registry.filtered(self.exclude_tools)
        orchestrator = ToolOrchestrator(registry, self.permission, ctx)
        return AgentLoop(
            provider=active_provider,
            registry=registry,
            orchestrator=orchestrator,
            emit=worker_emit,
            rollout=self.rollout,
            flag_gate=self.flag_gate,
            max_steps=self.max_steps,
            budget_state=self.budget_state,
            worker=worker_id,
            interrupt=self.interrupt,
        )

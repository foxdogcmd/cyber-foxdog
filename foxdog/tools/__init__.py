"""Tool 基类 + ToolAccess + registry。

每个工具声明 accesses（读写哪些资源），调度器据此做资源冲突感知并发：
无冲突的 tool call 并行，有冲突的串行（见 core/scheduler.py）。

ctx 为工具提供运行期能力：workspace / CAS / 执行 runtime / 后台任务 /
FlagGate / 事件发射。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..executor.background import BackgroundRegistry
from ..executor.runtime import ExecutionRuntime, LocalRuntime
from ..executor.session import SessionRegistry
from ..protocol.types import Event
from ..store.workspace import Workspace
from ..swarm.facts import FactsBoard

Resource = Literal["shell", "workspace", "network", "python", "state", "gate", "tasks", "meta"]


class ToolAccess(BaseModel):
    resource: str  # 资源名，如 "shell"、"workspace"、"network"
    mode: Literal["r", "w"]


class ToolResult(BaseModel):
    ok: bool = True
    output: str = ""
    error: str | None = None
    handle: str | None = None  # 大输出 spill 到 CAS 的句柄
    data: dict[str, Any] = Field(default_factory=dict)

    def render(self) -> str:
        """回填给模型的文本。"""
        parts: list[str] = []
        if self.error:
            parts.append(f"error: {self.error}")
        if self.output:
            parts.append(self.output)
        if self.handle:
            parts.append(f"[输出已截断，全文用 fetch_output(handle={self.handle!r}) 取回]")
        return "\n".join(parts) or "(no output)"


class ToolContext:
    """工具运行期上下文。flag_gate 用后注册（避免循环依赖）。

    sessions / facts 缺省自动创建（持久 shell 会话注册表、run 级事实板），
    CLI 可注入共享实例以便 solve 结束时统一 close_all。
    """

    def __init__(
        self,
        workspace: Workspace | None,
        background: BackgroundRegistry,
        emit: Callable[[Event], None],
        fetch_output: Callable[[str, int, int], str],
        flag_gate: Any = None,
        sessions: SessionRegistry | None = None,
        facts: FactsBoard | None = None,
        runtime: ExecutionRuntime | None = None,
    ) -> None:
        self.workspace = workspace
        self.background = background
        self.emit = emit
        self.fetch_output = fetch_output
        self.flag_gate = flag_gate
        self.runtime = runtime or LocalRuntime(workspace.dir if workspace is not None else None)
        self.sessions = sessions or SessionRegistry()
        if facts is not None:
            self.facts = facts
        elif workspace is not None:
            self.facts = FactsBoard(workspace.dir / "blackboard.db")
        else:
            self.facts = None  # /setting 配置 agent：无工作区无黑板（工具集不含事实板）


class Tool(ABC):
    name: str
    description: str
    accesses: list[ToolAccess] = []
    #: 参数模型（pydantic），同时用于生成 JSON schema 与校验模型传入的 args
    args_model: type[BaseModel] | None = None

    @property
    def readonly(self) -> bool:
        return all(a.mode == "r" for a in self.accesses)

    def parameters(self) -> dict[str, Any]:
        if self.args_model is not None:
            schema = self.args_model.model_json_schema()
            schema.pop("title", None)
            return schema
        return {"type": "object", "properties": {}}

    def parse_args(self, args: dict[str, Any]) -> BaseModel:
        if self.args_model is None:
            raise ValueError(f"{self.name} 不接受参数")
        return self.args_model(**args)

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def filtered(self, exclude: set[str]) -> "ToolRegistry":
        """复制 registry 并剥掉指定工具（子 agent 深度上限：不含 spawn_agent）。"""
        out = ToolRegistry()
        for name, tool in self._tools.items():
            if name not in exclude:
                out.register(tool)
        return out

    def openai_tools(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]


def register_builtin_tools(registry: ToolRegistry) -> None:
    """注册全部内置工具（常驻）。"""
    from .builtin.board import ClaimIntentTool, CompleteIntentTool, ListBoardTool
    from .builtin.facts import PostDeadEndTool, PostFactTool
    from .builtin.import_file import ImportFileTool
    from .builtin.files import EditFileTool, GlobTool, GrepTool, ReadFileTool, WriteFileTool
    from .builtin.flag import SubmitFlagTool
    from .builtin.answer import SubmitAnswerTool
    from .builtin.http import HttpRequestTool
    from .builtin.meta import FetchOutputTool, TaskListTool, TaskOutputTool, TaskStopTool
    from .builtin.python_repl import PythonEvalTool
    from .builtin.session import ShellSessionTool
    from .builtin.shell import ShellExecTool
    from .builtin.todo import TodoListTool

    for tool in (
        ShellExecTool(),
        ShellSessionTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        PythonEvalTool(),
        HttpRequestTool(),
        SubmitFlagTool(),
        SubmitAnswerTool(),
        TodoListTool(),
        PostFactTool(),
        PostDeadEndTool(),
        ImportFileTool(),
        ListBoardTool(),
        ClaimIntentTool(),
        CompleteIntentTool(),
        FetchOutputTool(),
        TaskListTool(),
        TaskOutputTool(),
        TaskStopTool(),
    ):
        registry.register(tool)

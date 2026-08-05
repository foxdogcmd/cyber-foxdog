"""上下文管理与压缩（借鉴 LeoAI 归档 + kimi-code compaction/renderTodoList）。

两级压缩：
- 微压缩：每个 step 后，超过 keep_steps 个 step 且 >min_size 的旧工具输出替换为
  `[已归档 handle=xxx 摘要]`——全文已在 CAS（orchestrator 对每个输出归档，
  loop 记录 tool_call_id → 句柄），模型可用 fetch_output 取回。
- 全量压缩：估算 token（chars/4）≥ 窗口 85% 时触发，用压缩 provider（planner/
  便宜模型，无配置则 solver 模型）按 CTF 专用指令压缩历史；压缩后把 todo list、
  事实板摘要根据 state_renderer 重渲染回上下文，防"压缩性失忆"。

每次压缩都通过 record 回调写 CompactionRecord 进 rollout。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..llm.client import ChatProvider

#: CTF 专用压缩指令：保留解题关键状态，丢弃噪声
COMPACT_INSTRUCTION = """\
你是 CTF 解题会话的压缩器。把下面的解题历史压缩成结构化摘要，必须保留：
1. 目标地址、端口与服务清单（含版本）
2. 已验证事实（含证据出处）
3. 已排除方向（死路）及排除原因
4. 当前假设与下一步计划
5. 工作区已写文件清单及各自用途
6. flag 状态（候选值/已提交/被拒及被拒原因）
丢弃：重复的命令输出、长 banner、失败命令的完整回显（只留一句结论）。
直接输出中文摘要，不超过 800 字。

=== 解题历史 ===
"""

# 压缩输入上限：头 10000 字符 + 尾 50000 字符（早期目标描述 + 近期进展最重要）
_HEAD_KEEP = 10_000
_TAIL_KEEP = 50_000


class CompactionRecord(BaseModel):
    kind: str  # micro | full
    before_tokens: int
    after_tokens: int
    replaced: int = 0  # micro：替换掉的旧输出条数
    summary: str = ""  # full：压缩摘要前 200 字
    ts: float = 0.0


def estimate_tokens(history: list[dict[str, Any]]) -> int:
    """粗略估算：字符数 / 4。"""
    chars = 0
    for m in history:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            chars += len(fn.get("arguments", "")) + len(fn.get("name", ""))
    return chars // 4


class CompactionManager:
    def __init__(
        self,
        provider: ChatProvider,
        window_tokens: int = 128_000,
        threshold: float = 0.85,
        keep_steps: int = 6,
        min_size: int = 1024,
        state_renderer: Callable[[], str] | None = None,
    ) -> None:
        self.provider = provider
        self.window_tokens = window_tokens
        self.threshold = threshold
        self.keep_steps = keep_steps
        self.min_size = min_size
        #: 全量压缩后重渲染 todo list + 事实板摘要（kimi renderTodoList 思路）
        self.state_renderer = state_renderer

    # ------------------------------------------------------------ 微压缩
    def micro_compact(self, history: list[dict[str, Any]], archived: dict[str, str]) -> int:
        """把"超过 keep_steps 个 step 且 >min_size"的旧工具输出替换为归档占位符。

        年龄定义：该 tool 消息之后出现的 assistant(tool_calls) 消息数。
        archived: tool_call_id → CAS 句柄（全文已在 CAS，直接引用）。
        """
        n = len(history)
        # suffix[i] = 位置 i 之后（含 i）的 assistant(tool_calls) 消息数
        suffix = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            m = history[i]
            is_step = m.get("role") == "assistant" and m.get("tool_calls")
            suffix[i] = suffix[i + 1] + (1 if is_step else 0)

        replaced = 0
        for i, m in enumerate(history):
            if m.get("role") != "tool":
                continue
            content = m.get("content") or ""
            if len(content) <= self.min_size or content.startswith("[已归档"):
                continue
            if suffix[i + 1] <= self.keep_steps:
                continue  # 仍属最近 keep_steps 步之内
            handle = archived.get(m.get("tool_call_id"), "N/A")
            digest = " ".join(content[:200].split())
            m["content"] = f"[已归档 handle={handle} 摘要: {digest}…]"
            replaced += 1
        return replaced

    # ------------------------------------------------------------ 全量压缩
    def _serialize(self, history: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for m in history:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if m.get("tool_calls"):
                calls = ", ".join(
                    f"{(tc.get('function') or {}).get('name')}({(tc.get('function') or {}).get('arguments', '')[:200]})"
                    for tc in m["tool_calls"]
                )
                content = f"{content} [调用工具: {calls}]"
            lines.append(f"<{role}> {content}")
        text = "\n".join(lines)
        if len(text) > _HEAD_KEEP + _TAIL_KEEP:
            text = text[:_HEAD_KEEP] + "\n...[中段省略]...\n" + text[-_TAIL_KEEP:]
        return text

    async def full_compact(
        self,
        history: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> str:
        """调压缩 provider 生成摘要；返回摘要文本。"""
        serialized = self._serialize(history)
        msg = await self.provider.stream_chat(
            [{"role": "user", "content": COMPACT_INSTRUCTION + serialized}],
            [],
            on_delta or (lambda _d: None),
            on_reasoning_delta=on_reasoning_delta,
        )
        return msg.content.strip()

    async def maybe_full_compact(
        self,
        history: list[dict[str, Any]],
        record: Callable[[dict[str, Any]], None],
        *,
        on_full_start: Callable[[], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> CompactionRecord | None:
        """估算 token ≥ 窗口 85% 时执行全量压缩（原地改 history）。"""
        before = estimate_tokens(history)
        if before < int(self.window_tokens * self.threshold):
            return None
        if on_full_start is not None:
            on_full_start()
        summary = await self.full_compact(
            history,
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
        state = self.state_renderer() if self.state_renderer else ""
        system = history[0] if history and history[0].get("role") == "system" else None
        rebuilt: list[dict[str, Any]] = []
        if system is not None:
            rebuilt.append(system)
        rebuilt.append(
            {
                "role": "user",
                "content": (
                    f"[历史已压缩，以下为摘要]\n{summary}\n\n"
                    f"[当前状态重渲染]\n{state or '（无）'}\n\n继续解题。"
                ),
            }
        )
        history[:] = rebuilt
        rec = CompactionRecord(
            kind="full",
            before_tokens=before,
            after_tokens=estimate_tokens(history),
            summary=summary[:200],
            ts=time.time(),
        )
        record({"type": "compaction", **rec.model_dump()})
        return rec

    async def step(
        self,
        history: list[dict[str, Any]],
        archived: dict[str, str],
        record: Callable[[dict[str, Any]], None],
        *,
        on_full_start: Callable[[], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> CompactionRecord | None:
        """每个 step 前调用：先微压缩，再判断是否全量压缩。"""
        replaced = self.micro_compact(history, archived)
        if replaced:
            rec = CompactionRecord(
                kind="micro",
                before_tokens=0,
                after_tokens=estimate_tokens(history),
                replaced=replaced,
                ts=time.time(),
            )
            record({"type": "compaction", **rec.model_dump()})
        return await self.maybe_full_compact(
            history,
            record,
            on_full_start=on_full_start,
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning_delta,
        )

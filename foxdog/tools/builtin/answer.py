"""通用答案提交工具。

submit_flag 保持严格 FlagGate 兼容；submit_answer 用于一般网络安全任务，
把结构化答案落盘并通知 spectator/平台闭环，不假设答案一定是 flag。
"""
from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from ...protocol.types import SwarmNote
from .. import Tool, ToolAccess, ToolContext, ToolResult


class SubmitAnswerArgs(BaseModel):
    answer: str = Field(description="最终答案，可以是漏洞结论、凭据、报告摘要或平台要求的文本")
    answer_type: str = Field(default="security_result", description="答案类型，如 finding/credential/report/flag")
    evidence: str | None = Field(default=None, description="可选 CAS 证据句柄")
    details: dict[str, Any] = Field(default_factory=dict, description="可选结构化字段")


class SubmitAnswerTool(Tool):
    name = "submit_answer"
    description = (
        "提交网络安全任务的最终答案。答案会落盘、通知旁观者并可由平台提交；"
        "不要求答案是 flag，也不要把测试文本当成答案。提交后继续等待 spectator 审查。"
    )
    accesses = [ToolAccess(resource="workspace", mode="w")]
    args_model = SubmitAnswerArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        if ctx.workspace is None:
            return ToolResult(ok=False, error="当前没有任务工作区")
        record = {
            "answer": a.answer,
            "answer_type": a.answer_type,
            "evidence": a.evidence,
            "details": a.details,
            "ts": time.time(),
        }
        path = ctx.workspace.dir / "answer.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if ctx.facts is not None:
            ctx.facts.post_fact(
                f"已提交最终答案（{a.answer_type}）：{a.answer[:300]}",
                a.evidence,
                actor="main",
            )
        ctx.emit(SwarmNote(message=f"已提交答案（{a.answer_type}），等待 spectator 审查"))
        return ToolResult(output=f"答案已保存到 {path.name}，已通知 spectator 审查。")

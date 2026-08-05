"""http_request：原始 HTTP 请求工具（比 curl 可控，headers/body 完全自定义）。"""
from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from ...core.context import spill_text
from .. import Tool, ToolAccess, ToolContext, ToolResult


class HttpArgs(BaseModel):
    url: str
    method: str = Field(default="GET", description="GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS")
    headers: dict[str, str] | None = Field(default=None, description="自定义请求头")
    body: str | None = Field(default=None, description="原始请求体")
    timeout: float = Field(default=30.0, description="秒")
    follow_redirects: bool = False
    verify: bool = Field(
        default=False,
        description="是否校验 TLS 证书。CTF 靶机自签证书常见，默认跳过；httpx 不走 urllib3，无 InsecureRequestWarning 需抑制",
    )


class HttpRequestTool(Tool):
    name = "http_request"
    description = "发送原始 HTTP 请求，完全控制 method/headers/body，返回状态行+响应头+响应体。"
    accesses = [ToolAccess(resource="network", mode="w")]
    args_model = HttpArgs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        a = self.parse_args(args)
        try:
            async with httpx.AsyncClient(follow_redirects=a.follow_redirects, verify=a.verify) as client:
                resp = await client.request(
                    a.method.upper(), a.url, headers=a.headers, content=a.body, timeout=a.timeout
                )
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        header_lines = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
        raw = f"HTTP {resp.status_code} {resp.reason_phrase}\n{header_lines}\n\n{resp.text}"
        view, handle, truncated = spill_text(ctx.workspace, raw, 32 * 1024)
        return ToolResult(output=view, handle=handle, data={"status": resp.status_code, "truncated": truncated})

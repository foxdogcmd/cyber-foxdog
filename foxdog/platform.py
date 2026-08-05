"""配置驱动的平台 HTTP 客户端：题目拉取、附件下载、答案提交、状态查询。"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import PlatformConfig


def parse_platform_stats(payload: Any) -> dict[str, Any]:
    """Extract challenge totals from common platform response shapes.

    The platform schema is intentionally configurable, so this parser accepts
    both aggregate fields and challenge lists without assuming a CTF vendor.
    Unknown values remain ``None`` instead of being guessed.
    """
    root = payload if isinstance(payload, dict) else {"data": payload}
    data = root.get("data") if "data" in root and isinstance(root.get("data"), (dict, list)) else root
    items: Any = data if isinstance(data, list) else None
    for key in ("challenges", "items", "results", "problems"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            items = data[key]
            break
    def first_int(keys: tuple[str, ...]) -> int | None:
        for obj in (data if isinstance(data, dict) else {}, root):
            if not isinstance(obj, dict):
                continue
            for key in keys:
                value = obj.get(key)
                if isinstance(value, bool):
                    continue
                try:
                    if value is not None:
                        return int(value)
                except (TypeError, ValueError):
                    continue
        return None
    total = first_int(("total", "count", "total_challenges", "challenge_count"))
    solved = first_int(("solved", "solved_count", "completed", "completed_count"))
    challenges: list[dict[str, Any]] = []
    if items is not None:
        for item in items:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", ""))
            is_solved = item.get("solved") is True or status.lower() in {
                "solved", "completed", "accepted", "done"
            }
            challenges.append({
                "id": str(item.get("id") or item.get("slug") or item.get("name") or "?"),
                "title": str(item.get("title") or item.get("name") or item.get("slug") or ""),
                "status": status or ("solved" if is_solved else "unknown"),
                "solved": is_solved,
                "detail": str(item.get("description") or item.get("detail") or ""),
            })
        if total is None:
            total = len(challenges)
        if solved is None:
            solved = sum(1 for item in challenges if item["solved"])
    return {"total": total, "solved": solved, "challenges": challenges, "raw": payload}


async def poll_platform_status(cfg: PlatformConfig, *, run_id: str = "", challenge_id: str = "", resolve_secret=None) -> dict[str, Any]:
    """Poll a configured platform status endpoint once."""
    if not cfg.status_url:
        return {"total": None, "solved": None, "challenges": [], "raw": None}
    values = {"id": challenge_id, "run_id": run_id}
    url = _render(cfg.status_url, values)
    if resolve_secret is not None:
        url = resolve_secret(url) or url
    async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
        if cfg.status_method == "POST":
            resp = await client.post(url, json=_render(cfg.fetch_body, values))
        else:
            resp = await client.get(url)
        if getattr(resp, "status_code", 200) >= 400:
            raise httpx.HTTPStatusError("platform status failed", request=resp.request, response=resp)
        try:
            payload = resp.json()
        except (ValueError, AttributeError):
            payload = {"status": getattr(resp, "text", "")}
        stats = parse_platform_stats(payload)
        stats["fetched_at"] = time.time()
        return stats


class _SafeDict(dict):
    """format_map 兜底：未知占位符原样保留 `{key}`，不抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return "{" + key + "}"


def _render(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(_SafeDict({k: str(v) for k, v in values.items()}))
    if isinstance(value, dict):
        return {k: _render(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, values) for v in value]
    return value


async def fetch_challenge(cfg: PlatformConfig, identifier: str, download_dir: Path | None = None) -> tuple[str, list[Path]]:
    values = {"id": identifier, "run_id": ""}
    # fetch_url 支持 {id} 占位（经 _render 安全渲染）；为空则把 identifier 当裸 URL
    url = _render(cfg.fetch_url, values) if cfg.fetch_url else identifier
    async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
        if cfg.fetch_method == "POST":
            resp = await client.post(url, json=_render(cfg.fetch_body, values))
        else:
            resp = await client.get(url, params=_render(cfg.fetch_body, values) or None)
        if getattr(resp, "status_code", 200) >= 400:
            raise httpx.HTTPStatusError("platform fetch failed", request=resp.request, response=resp)
        try:
            payload = resp.json()
        except (ValueError, AttributeError):
            return resp.text, []
        text = payload.get("description") or payload.get("content") or payload.get("statement") or json_text(payload)
        urls = payload.get(cfg.attachment_field) or []
        if isinstance(urls, str):
            urls = [urls]
        paths: list[Path] = []
        if download_dir:
            download_dir.mkdir(parents=True, exist_ok=True)
            for index, item in enumerate(urls):
                link = urljoin(str(resp.url), str(item.get("url") if isinstance(item, dict) else item))
                data = await client.get(link)
                if getattr(data, "status_code", 200) >= 400:
                    continue
                name = Path(link.split("?", 1)[0]).name or f"attachment-{index}"
                target = download_dir / re.sub(r"[^A-Za-z0-9._-]", "_", name)
                target.write_bytes(data.content)
                paths.append(target)
        return str(text), paths


def json_text(payload: Any) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def submit(cfg: PlatformConfig, *, answer: str, answer_type: str, run_id: str, flag: str | None = None, flags: list[str] | None = None, details: dict[str, Any] | None = None) -> int:
    if not cfg.submit_url:
        return 0
    values = {"answer": answer, "answer_type": answer_type, "flag": flag or answer, "flags": flags or [], "run_id": run_id, "details": details or {}}
    body = _render(cfg.submit_body, values) if cfg.submit_body else values
    async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=True) as client:
        url = cfg.submit_url.replace("{run_id}", run_id)
        if cfg.submit_method == "POST":
            response = await client.post(url, json=body)
        elif cfg.submit_method == "PUT":
            response = await client.put(url, json=body)
        else:
            response = await client.patch(url, json=body)
    return response.status_code

"""Best-effort model discovery for OpenAI/Anthropic-compatible endpoints."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from .client import ModelEndpoint


def model_urls(endpoint: ModelEndpoint) -> list[str]:
    base = (endpoint.base_url or "").rstrip("/")
    if not base:
        return []
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    if path.endswith("/anthropic"):
        path = path[: -len("/anthropic")]
    roots = [path]
    if path.endswith("/coding"):
        roots.insert(0, path + "/v1")
    urls = [urlunsplit((parts.scheme, parts.netloc, root + "/models", "", "")) for root in roots]
    return list(dict.fromkeys(urls))


async def fetch_model_ids(endpoint: ModelEndpoint, *, timeout: float = 8.0) -> list[str]:
    """Return available model IDs; unsupported/unreachable endpoints return []."""
    key = endpoint.resolved_key()
    if not key:
        return []
    headers = {"Authorization": f"Bearer {key}", "x-api-key": key}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in model_urls(endpoint):
            try:
                response = await client.get(url, headers=headers)
                if response.status_code >= 400:
                    continue
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            entries = payload.get("data") if isinstance(payload, dict) else None
            if entries is None and isinstance(payload, dict):
                entries = payload.get("models")
            ids = sorted({
                str(item.get("id") or item.get("name"))
                for item in (entries or [])
                if isinstance(item, dict) and (item.get("id") or item.get("name"))
            })
            if ids:
                return ids
    return []

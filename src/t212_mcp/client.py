"""Thin async client for the Trading 212 public API (read-only endpoints)."""

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from .config import BASE_URLS, Settings

API_PREFIX = "/api/v0"
MAX_PAGE_SIZE = 50


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str | None
    env: str  # live | demo

    @classmethod
    def from_settings(cls, settings: Settings) -> "Credentials":
        if settings.api_key is None:
            raise ToolError("Missing configuration: set T212_API_KEY (and T212_API_SECRET) in .env.")
        return cls(settings.api_key.get_secret_value(),
                   settings.api_secret.get_secret_value() if settings.api_secret else None, settings.env)

    def __repr__(self) -> str:  # never print keys
        return f"Credentials(env={self.env!r})"


def auth_header(creds: "Credentials | Settings") -> str:
    if isinstance(creds, Settings):
        creds = Credentials.from_settings(creds)
    if creds.api_secret is None:
        return creds.api_key
    token = base64.b64encode(f"{creds.api_key}:{creds.api_secret}".encode()).decode()
    return f"Basic {token}"


class T212Client:
    """One Trading 212 account. `creds` defaults to the key in settings (the owner / single-user setup)."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 creds: Credentials | None = None):
        self.settings = settings
        self.creds = creds or Credentials.from_settings(settings)
        self._http = httpx.AsyncClient(
            base_url=BASE_URLS[self.creds.env],
            headers={"Authorization": auth_header(self.creds), "Accept": "application/json"},
            timeout=20.0,
            transport=transport,
        )
        self._cache: dict[tuple, tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict | None = None, ttl: float = 0) -> Any:
        """GET an API path (relative to /api/v0), caching the result for `ttl` seconds."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = (path, tuple(sorted(params.items())))
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        data = await self._request(API_PREFIX + path, params)
        if ttl:
            self._cache[key] = (time.monotonic() + ttl, data)
        return data

    async def get_paginated(self, path: str, limit: int, params: dict | None = None) -> list[dict]:
        """Follow `nextPagePath` cursors until `limit` items are collected."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["limit"] = min(limit, MAX_PAGE_SIZE)
        page = await self._request(API_PREFIX + path, params)
        items = list(page.get("items", []))
        while len(items) < limit and page.get("nextPagePath"):
            next_path = page["nextPagePath"]
            if not next_path.startswith(API_PREFIX):
                next_path = API_PREFIX + next_path
            page = await self._request(next_path, None)
            items.extend(page.get("items", []))
        return items[:limit]

    async def _request(self, url: str, params: dict | None, retried: bool = False) -> Any:
        try:
            resp = await self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise ToolError(f"Could not reach Trading 212: {e}") from e

        if resp.status_code == 429 and not retried:
            await asyncio.sleep(_retry_delay(resp))
            return await self._request(url, params, retried=True)
        if resp.status_code == 429:
            raise ToolError("Trading 212 rate limit hit; try again in a few seconds.")
        if resp.status_code == 401:
            raise ToolError(
                f"Trading 212 rejected the credentials (env={self.creds.env}). "
                "Check T212_API_KEY / T212_API_SECRET and that the key belongs to this environment."
            )
        if resp.status_code == 403:
            raise ToolError(
                f"The API key lacks permission for {url}. Enable the matching read scope for the key in Trading 212."
            )
        if resp.status_code == 404:
            raise ToolError(f"Not found: {url}")
        if resp.is_error:
            raise ToolError(f"Trading 212 returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def _retry_delay(resp: httpx.Response) -> float:
    reset = resp.headers.get("x-ratelimit-reset")
    try:
        # Header is a unix timestamp (seconds) at which the limit resets.
        delay = float(reset) - time.time()
    except (TypeError, ValueError):
        delay = 2.0
    return min(max(delay, 0.5), 10.0)

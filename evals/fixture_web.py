"""Offline web for component evals.

While `offline()` is active, every HTTP request except to the LLM API is answered from the FixtureWeb that the
current asyncio task selected with `use()`; anything not in it gets a 404. Headless-browser fetches are served the
same way, so cases can run concurrently without seeing each other's pages.
"""

import os
import re
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import respx

from t212_mcp.lookthrough import extractors


@dataclass(frozen=True)
class Page:
    pattern: str  # regex searched in the full URL
    body: bytes
    status: int = 200
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class FixtureWeb:
    pages: tuple[Page, ...] = ()

    def find(self, url: str) -> Page | None:
        return next((p for p in self.pages if re.search(p.pattern, url)), None)


_current: ContextVar[FixtureWeb | None] = ContextVar("fixture_web", default=None)


@contextmanager
def use(web: FixtureWeb):
    token = _current.set(web)
    try:
        yield
    finally:
        _current.reset(token)


def _lookup(url: str) -> Page | None:
    web = _current.get()
    return web.find(url) if web else None


def _dispatch(request: httpx.Request) -> httpx.Response:
    page = _lookup(str(request.url))
    if page is None:
        return httpx.Response(404, text="not in fixtures")
    return httpx.Response(page.status, content=page.body, headers={"content-type": page.content_type})


class _FakeBrowser:
    async def goto(self, url: str) -> int:
        return 200

    async def fetch_in_page(self, url: str) -> tuple[int, str]:
        page = _lookup(url)
        return (page.status, page.body.decode()) if page else (404, "")


@asynccontextmanager
async def _fake_browser_session():
    yield _FakeBrowser()


def llm_hosts() -> set[str]:
    hosts = {"api.openai.com"}
    if base := os.environ.get("OPENAI_BASE_URL"):
        hosts.add(urlparse(base).hostname or "")
    return hosts


@contextmanager
def offline():
    original = extractors.browser_session
    extractors.browser_session = _fake_browser_session
    try:
        with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
            for host in llm_hosts():
                router.route(host=host).pass_through()
            router.route().mock(side_effect=_dispatch)
            yield
    finally:
        extractors.browser_session = original

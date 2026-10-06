"""Read-only tools for the discovery agent. All output is untrusted web content."""

import json
import os
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx
from langchain_core.tools import tool

from .extractors import browser_session, http_client, normalize_download_url, parse_table, sniff_format

MAX_TEXT = 2500
LINK_HINT = re.compile(
    r"holding|constituent|portfolio|composition|basket|download|document|xls|csv|json|export|spreadsheet|factsheet|pcf|fund-data",
    re.I,
)
DATA_HINT = re.compile(r"holding|constituent|portfolio|basket|composition|weight", re.I)
CONSENT_BUTTONS = ("Accept all", "Accept All", "Accept", "I agree", "Agree", "Allow all", "OK")
NOISE_HOSTS = re.compile(r"google-analytics|googletagmanager|doubleclick|facebook|hotjar|segment|optimizely|onetrust|cookielaw|adobedtm|demdex|newrelic|sentry|clarity", re.I)

_search_provider = "duckduckgo"


def configure(search_provider: str) -> None:
    global _search_provider
    _search_provider = search_provider


def _untrusted(body: str) -> str:
    return f"<untrusted_web_content>\n{body}\n</untrusted_web_content>"


def _html_text(html: str) -> tuple[str, str]:
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    return (unescape(title.group(1).strip()) if title else ""), re.sub(r"\s+", " ", text).strip()


def _links(html: str, base: str) -> list[str]:
    out = []
    for m in re.finditer(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", html, re.S | re.I):
        href = urljoin(base, unescape(m.group(1)))
        label = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()[:60]
        if LINK_HINT.search(href) or LINK_HINT.search(label):
            out.append(f"{label} -> {href}")
    return list(dict.fromkeys(out))[:40]


def summarize_data(content: bytes, content_type: str, url: str) -> str:
    fmt = sniff_format(content, content_type, url)
    if fmt == "json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return "json (unparseable)"
        return "format: json\n" + _json_shape(data)
    if fmt in ("xls", "xlsx", "csv"):
        try:
            rows = parse_table(content, fmt)
        except Exception as e:
            return f"format: {fmt} (could not parse: {e})"
        lines = [f"format: {fmt}, {len(rows)} rows. First rows (cells separated by ' | '):"]
        for i, row in enumerate(rows[:16]):
            cells = ["" if v is None else str(v)[:28] for v in row]
            lines.append(f"row {i}: " + " | ".join(cells))
        if len(rows) > 16:
            lines.append("...")
            lines.append(f"row {len(rows) - 1}: " + " | ".join("" if v is None else str(v)[:28] for v in rows[-1]))
        return "\n".join(lines)
    return f"format: {fmt}"


def _json_shape(data, path: str = "", depth: int = 0) -> str:
    lines = []
    if isinstance(data, dict):
        for k, v in list(data.items())[:25]:
            p = f"{path}.{k}" if path else k
            if isinstance(v, list):
                lines.append(f"{p}: list[{len(v)}]" + (f" first item: {json.dumps(v[0])[:300]}" if v else ""))
            elif isinstance(v, dict) and depth < 2:
                lines.append(_json_shape(v, p, depth + 1))
            else:
                lines.append(f"{p}: {json.dumps(v)[:80]}")
    elif isinstance(data, list):
        lines.append(f"(root) list[{len(data)}]" + (f" first item: {json.dumps(data[0])[:300]}" if data else ""))
    return "\n".join(lines)


@tool
async def web_search(query: str) -> str:
    """Search the web. Returns the top results (title, URL, snippet). Include the fund's ISIN for precise results."""
    try:
        if _search_provider == "tavily":
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post("https://api.tavily.com/search", json={"api_key": os.environ["TAVILY_API_KEY"], "query": query, "max_results": 8})
            results = [{"title": x["title"], "href": x["url"], "body": x.get("content", "")} for x in r.json().get("results", [])]
        elif _search_provider == "brave":
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": 8},
                                headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"})
            results = [{"title": x["title"], "href": x["url"], "body": x.get("description", "")} for x in r.json().get("web", {}).get("results", [])]
        else:
            import asyncio

            from ddgs import DDGS

            results = await asyncio.to_thread(lambda: list(DDGS().text(query, max_results=8)))
    except Exception as e:
        return f"search failed: {type(e).__name__}: {e}"
    if not results:
        return "no results"
    return _untrusted("\n".join(f"- {r['title']}\n  {r['href']}\n  {r.get('body', '')[:200]}" for r in results))


@tool
async def fetch_url(url: str) -> str:
    """Plain HTTP GET (no browser). For HTML pages returns the title, a text excerpt and links that look like
    holdings/downloads. For data files (xls/xlsx/csv/json) returns their structure. Google Sheets/Drive share
    links are fetched as file exports."""
    target = normalize_download_url(url)
    try:
        async with http_client() as c:
            r = await c.get(target)
    except httpx.HTTPError as e:
        return f"request failed: {type(e).__name__}: {e}"
    head = f"GET {target} -> HTTP {r.status_code}, content-type: {r.headers.get('content-type', '')}, final URL: {r.url}"
    if r.is_error:
        hint = " (blocked for non-browser clients; try browser_open)" if r.status_code in (401, 403, 406, 429) else ""
        return head + hint
    fmt = sniff_format(r.content, r.headers.get("content-type", ""), str(r.url))
    if fmt == "html":
        title, text = _html_text(r.text)
        links = _links(r.text, str(r.url))
        body = f"title: {title}\ntext: {text[:MAX_TEXT]}\nlinks:\n" + "\n".join(links)
        return head + "\n" + _untrusted(body)
    return head + "\n" + _untrusted(summarize_data(r.content, r.headers.get("content-type", ""), str(r.url)))


def is_data_request(url: str, content_type: str, resource_type: str) -> bool:
    """Data a page loads: any fetch/XHR (APIs often send JSON as text/plain), or a file-like response."""
    if NOISE_HOSTS.search(url):
        return False
    if resource_type in ("fetch", "xhr"):
        return True
    return any(k in content_type for k in ("json", "csv", "excel", "spreadsheet", "octet-stream")) or bool(
        re.search(r"\.(xlsx?|csv|json)(\?|$)", url))


def rank_data_requests(seen: list[tuple[str, str, int]]) -> list[str]:
    """(url, description, size) triples, de-duplicated; holdings-looking URLs first, larger responses first
    within each group (a full holdings list is the big one)."""
    unique = list(dict.fromkeys(seen))
    unique.sort(key=lambda item: (0 if DATA_HINT.search(item[0]) else 1, -item[2]))
    return [desc for _, desc, _ in unique][:40]


async def _dismiss_consent(page) -> str:
    for label in CONSENT_BUTTONS:
        try:
            await page.get_by_role("button", name=label, exact=True).first.click(timeout=800)
            await page.wait_for_timeout(1500)
            return f"dismissed cookie banner ('{label}')\n"
        except Exception:
            continue
    return ""


@tool
async def browser_open(url: str, click_text: str | None = None) -> str:
    """Open a page in a real headless browser (works on sites that block plain HTTP). Dismisses cookie banners,
    then returns the title, a text excerpt, holdings/download links, and every data request (fetch/XHR and
    file downloads) the page made, with status, content type and size, holdings-looking URLs first. Optionally
    clicks the first element whose text contains `click_text` (e.g. 'Holdings'). Holdings tables are usually
    filled from one of these data requests: copy its exact URL rather than guessing API paths."""
    seen: list[tuple[str, str, int]] = []

    def on_response(resp):
        ct = resp.headers.get("content-type", "")
        if is_data_request(resp.url, ct, resp.request.resource_type):
            size = resp.headers.get("content-length")
            seen.append((resp.url, f"{resp.status} {resp.request.method} {ct.split(';')[0] or '?'}"
                                   f"{f' {size}B' if size else ''} {resp.url}", int(size) if size and size.isdigit() else 0))

    try:
        async with browser_session() as b:
            b.page.on("response", on_response)
            status = await b.goto(url)
            clicked = await _dismiss_consent(b.page)
            if click_text:
                try:
                    await b.page.get_by_text(click_text, exact=False).first.click(timeout=5000)
                    await b.page.wait_for_timeout(3000)
                    clicked += f"clicked '{click_text}'\n"
                except Exception as e:
                    clicked += f"could not click '{click_text}': {type(e).__name__}\n"
            html = await b.page.content()
            title, text = _html_text(html)
            links = _links(html, b.page.url)
    except Exception as e:
        return f"browser failed: {type(e).__name__}: {e}"
    body = (
        f"title: {title}\n{clicked}text: {text[:MAX_TEXT]}\nlinks:\n" + "\n".join(links)
        + "\ndata requests made by the page (copy URLs exactly):\n" + "\n".join(rank_data_requests(seen))
    )
    return f"browser GET {url} -> HTTP {status}\n" + _untrusted(body)


@tool
async def inspect_source(url: str, open_page_first: str | None = None) -> str:
    """Download a candidate data source and show its structure: sheet rows for xls/xlsx/csv, keys and list
    paths for JSON. If the source only works inside a browser session, pass the page to open first as
    `open_page_first`; the URL is then fetched from inside that page."""
    target = normalize_download_url(url)
    try:
        if open_page_first:
            async with browser_session() as b:
                await b.goto(open_page_first)
                status, text = await b.fetch_in_page(target)
            if status != 200:
                return (f"in-browser fetch {target} -> HTTP {status}. The API rejected the request itself, which "
                        "usually means a wrong path or query parameter: copy the exact URL from browser_open's "
                        "data requests instead of guessing")
            return f"in-browser fetch {target} -> HTTP 200\n" + _untrusted(summarize_data(text.encode(), "", target))
        async with http_client() as c:
            r = await c.get(target)
        if r.is_error:
            return f"GET {target} -> HTTP {r.status_code}" + (
                " (this API refuses plain HTTP clients: retry with open_page_first set to a page on the same site)"
                if r.status_code in (401, 403, 406, 429) else "")
        return f"GET {target} -> HTTP 200, {len(r.content)} bytes\n" + _untrusted(
            summarize_data(r.content, r.headers.get("content-type", ""), str(r.url))
        )
    except Exception as e:
        return f"inspect failed: {type(e).__name__}: {e}"


TOOLS = [web_search, fetch_url, browser_open, inspect_source]


def host(url: str) -> str:
    return urlparse(url).netloc

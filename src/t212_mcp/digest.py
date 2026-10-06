"""Morning portfolio digest: how each holding moved since the last digest, a chart of it, and recent headlines for
the largest holdings, sent by email through Resend."""

import asyncio
import base64
import html
import io
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx

from . import snapshots
from .config import Settings

log = logging.getLogger("t212_mcp.digest")

CHART_CID = "holdings-chart"
GAIN, LOSS = "#2a78d6", "#e34948"  # diverging blue/red, validated for colour-vision deficiency on white
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e7e6e2"
CURRENCY = {"GBP": "£", "USD": "$", "EUR": "€"}
NEWS_URL = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-GB&gl=GB&ceid=GB:en"
# Market-focused: a bare company name also matches shopping deals and product reviews.
NEWS_QUERY = '"{name}" (stock OR shares OR earnings)'


def snapshot_key(env: str) -> str:
    """Digest snapshots are their own series, so intraday get_portfolio_update calls don't move the baseline."""
    return f"dg-{env}"


# ---------------------------------------------------------------- holdings


def holding_rows(positions: list[dict], previous: dict | None) -> list[dict]:
    """One row per holding, largest first. The change is the price move (in account currency, so it includes FX)
    applied to the shares held now; buying or selling shows as a note rather than as a gain or loss."""
    prev = (previous or {}).get("positions", {})
    rows = []
    for p in positions:
        before = prev.get(p["ticker"])
        row = {"ticker": p["ticker"], "name": p["name"], "value": p["value"] or 0.0, "weight_pct": p["weight_pct"],
               "pnl_pct": p["pnl_pct"], "change": None, "change_pct": None, "note": None}
        if before and before["quantity"] > 0 and p["quantity"] > 0 and before["value"] and p["value"]:
            prev_price = before["value"] / before["quantity"]
            price = p["value"] / p["quantity"]
            row["change_pct"] = round((price / prev_price - 1) * 100, 2)
            row["change"] = round((price - prev_price) * p["quantity"], 2)
            if abs(p["quantity"] - before["quantity"]) > snapshots.QTY_EPSILON:
                row["note"] = f"{before['quantity']:g} → {p['quantity']:g} shares"
        elif previous:
            row["note"] = "new position"
        rows.append(row)
    for ticker, before in prev.items():
        if ticker not in {p["ticker"] for p in positions}:
            rows.append({"ticker": ticker, "name": before["name"], "value": 0.0, "weight_pct": None, "pnl_pct": None,
                         "change": None, "change_pct": None, "note": "closed"})
    rows.sort(key=lambda r: -r["value"])
    return rows


# ---------------------------------------------------------------- news


def query_name(name: str) -> str:
    """'Vanguard FTSE All-World (Acc)' -> 'Vanguard FTSE All-World'; 'NVIDIA CORP' -> 'Nvidia Corp'."""
    s = re.sub(r"\s*\([^)]*\)", "", name)
    s = re.sub(r"\s+(UCITS\s+ETF|ETF|ETC)\b.*$", "", s, flags=re.I).strip(" -,")
    return s.title() if s.isupper() else s


def news_targets(positions: list[dict], exposure: dict | None, top_n: int = 10, threshold_pct: float = 5.0) -> list[dict]:
    """The top positions by value, plus any underlying company above the threshold once ETFs are looked through."""
    targets, seen = [], set()

    def add(name: str, isin: str | None, reason: str):
        key = isin or query_name(name).lower()
        if key in seen:
            return
        seen.add(key)
        targets.append({"name": query_name(name), "isin": isin, "reason": reason})

    for p in sorted(positions, key=lambda p: -(p["value"] or 0))[:top_n]:
        add(p["name"], p.get("isin"), f"{p['weight_pct'] or 0:.1f}% of portfolio")
    for s in (exposure or {}).get("all_securities", []):
        if s["pct_of_portfolio"] > threshold_pct:
            add(s["name"], s.get("isin"), f"{s['pct_of_portfolio']:.1f}% of portfolio via look-through")
    return targets


def parse_feed(xml_text: str, now: datetime, max_age: timedelta, limit: int) -> list[dict]:
    items = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if not title or now - published > max_age:
            continue
        if source and title.endswith(f" - {source}"):  # Google appends the source to the title
            title = title[: -len(source) - 3]
        items.append({"title": title, "link": (item.findtext("link") or "").strip(), "source": source,
                      "published": published})
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:limit]


async def fetch_news(targets: list[dict], per_target: int = 3, max_age: timedelta = timedelta(hours=24),
                     now: datetime | None = None) -> list[dict]:
    """[{target, items}] for targets that have recent headlines. A failed feed is skipped, not fatal."""
    now = now or datetime.now(timezone.utc)
    sem = asyncio.Semaphore(4)

    async def one(client: httpx.AsyncClient, target: dict) -> dict:
        url = NEWS_URL.format(q=quote_plus(NEWS_QUERY.format(name=target["name"])))
        try:
            async with sem:
                r = await client.get(url)
            r.raise_for_status()
            items = parse_feed(r.text, now, max_age, per_target)
        except (httpx.HTTPError, ET.ParseError) as e:
            log.warning("news for %s failed: %s", target["name"], e)
            items = []
        return {"target": target, "items": items}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        results = await asyncio.gather(*(one(c, t) for t in targets))
    seen_titles: set[str] = set()
    out = []
    for res in results:  # the same story often matches several holdings or comes from several outlets; show it once
        unique = []
        for i in res["items"]:
            if i["title"].lower() not in seen_titles:
                seen_titles.add(i["title"].lower())
                unique.append(i)
        res["items"] = unique
        if res["items"]:
            out.append(res)
    return out


# ---------------------------------------------------------------- chart


def render_chart(rows: list[dict], max_bars: int = 25) -> bytes | None:
    """Horizontal bars of each holding's % move, largest gain at the top. PNG, or None with nothing to compare."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = [r for r in rows if r["change_pct"] is not None]
    if not data:
        return None
    data = sorted(data, key=lambda r: abs(r["change_pct"]), reverse=True)[:max_bars]
    data.sort(key=lambda r: r["change_pct"])
    labels = [r["name"] if len(r["name"]) <= 28 else r["name"][:27] + "…" for r in data]
    values = [r["change_pct"] for r in data]

    fig, ax = plt.subplots(figsize=(7, 0.34 * len(data) + 0.9), dpi=200)
    ax.barh(range(len(data)), values, height=0.6, color=[GAIN if v >= 0 else LOSS for v in values])
    span = max(abs(v) for v in values) or 1.0
    for i, v in enumerate(values):  # signed value at the bar tip, in text ink so colour is never the only cue
        ax.text(v + (span * 0.02 if v >= 0 else -span * 0.02), i, f"{v:+.2f}%", va="center",
                ha="left" if v >= 0 else "right", fontsize=8, color=INK)
    ax.set_yticks(range(len(data)), labels, fontsize=8, color=INK)
    ax.set_xlim(-span * 1.35 if min(values) < 0 else 0, span * 1.35 if max(values) > 0 else 0)
    ax.axvline(0, color=INK_MUTED, linewidth=0.8)
    ax.xaxis.set_major_formatter(lambda x, _: f"{x:+.1f}%" if x else "0%")
    ax.tick_params(axis="x", labelsize=7, colors=INK_MUTED, length=0)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- email


def _money(v: float | None, sym: str, signed: bool = False) -> str:
    if v is None:
        return "–"
    sign = ("+" if v > 0 else "−" if v < 0 else "") if signed else ("−" if v < 0 else "")
    return f"{sign}{sym}{abs(v):,.2f}"


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.2f}%"


def render_html(summary: dict, rows: list[dict], news: list[dict], previous: dict | None, has_chart: bool,
                now: datetime) -> str:
    sym = CURRENCY.get(summary.get("currency") or "", f"{summary.get('currency') or ''} ")
    e = html.escape
    total = summary["total_value"]
    if previous:
        since = datetime.fromisoformat(previous["taken_at"]).astimezone(now.tzinfo)
        change = total - previous["total_value"]
        headline = (f"{_money(change, sym, signed=True)} since {since:%a %d %b %H:%M} "
                    f"<span style='color:{INK_MUTED}'>(includes deposits and withdrawals)</span>")
    else:
        headline = "First digest: daily changes start from tomorrow's email."
    inv = summary["investments"]

    cell = "padding:6px 8px;border-bottom:1px solid #eeede9;font-size:13px;"
    num = cell + "text-align:right;white-space:nowrap;"
    table_rows = []
    for r in rows:
        weight = "–" if r["weight_pct"] is None else f"{r['weight_pct']:.1f}%"
        name = f"{e(r['name'])}<div style='color:{INK_MUTED};font-size:11px'>{e(r['ticker'])}" + (
            f" · {e(r['note'])}" if r["note"] else "") + "</div>"
        table_rows.append(
            f"<tr><td style='{cell}'>{name}</td><td style='{num}'>{_money(r['value'], sym)}</td>"
            f"<td style='{num}'>{_money(r['change'], sym, signed=True)}</td><td style='{num}'>{_pct(r['change_pct'])}</td>"
            f"<td style='{num}'>{weight}</td>"
            f"<td style='{num}'>{_pct(r['pnl_pct'])}</td></tr>")
    head = "".join(f"<th style='{cell}text-align:{a};color:{INK_MUTED};font-weight:600'>{h}</th>" for h, a in (
        ("Holding", "left"), ("Value", "right"), ("Change", "right"), ("Change %", "right"), ("Weight", "right"),
        ("Total P&amp;L", "right")))

    news_html = []
    for block in news:
        t = block["target"]
        items = "".join(
            f"<li style='margin:4px 0'><a href='{e(i['link'])}' style='color:{GAIN}'>{e(i['title'])}</a>"
            f"<span style='color:{INK_MUTED}'> · {e(i['source'])}, {i['published'].astimezone(now.tzinfo):%H:%M}</span></li>"
            for i in block["items"])
        news_html.append(f"<h3 style='font-size:14px;margin:16px 0 4px'>{e(t['name'])} "
                         f"<span style='color:{INK_MUTED};font-weight:400'>· {e(t['reason'])}</span></h3>"
                         f"<ul style='margin:0;padding-left:18px;font-size:13px'>{items}</ul>")
    news_section = "".join(news_html) or f"<p style='color:{INK_MUTED};font-size:13px'>No headlines in the last 24 hours.</p>"
    chart = (f"<img src='cid:{CHART_CID}' alt='Bar chart of each holding’s change since the last digest' "
             f"style='width:100%;max-width:640px;height:auto'>" if has_chart else "")

    return f"""<!doctype html><html><body style="margin:0;background:#ffffff;color:{INK};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<div style="max-width:680px;margin:0 auto;padding:20px 16px">
<p style="color:{INK_MUTED};font-size:12px;margin:0">Portfolio digest · {now:%A %d %B %Y}</p>
<h1 style="font-size:26px;margin:4px 0">{_money(total, sym)}</h1>
<p style="font-size:14px;margin:0 0 4px">{headline}</p>
<p style="color:{INK_MUTED};font-size:12px;margin:0 0 16px">Invested {_money(inv['current_value'], sym)} · unrealised P&amp;L {_money(inv['unrealized_pnl'], sym, signed=True)} · cash {_money(summary['cash']['available_to_trade'], sym)}</p>
<h2 style="font-size:16px;margin:20px 0 8px">Change by holding</h2>
{chart}
<table style="width:100%;border-collapse:collapse;margin-top:8px"><tr>{head}</tr>{''.join(table_rows)}</table>
<h2 style="font-size:16px;margin:24px 0 0">News for your largest holdings</h2>
{news_section}
<p style="color:{INK_MUTED};font-size:11px;margin-top:28px">Change = price move since the last digest applied to the shares you hold now, in account currency (includes FX). Headlines from Google News, last 24 hours. Not investment advice.</p>
</div></body></html>"""


def subject_line(summary: dict, previous: dict | None, now: datetime) -> str:
    sym = CURRENCY.get(summary.get("currency") or "", "")
    s = f"Portfolio {now:%a %d %b}: {_money(summary['total_value'], sym)}"
    if previous:
        s += f" ({_money(summary['total_value'] - previous['total_value'], sym, signed=True)})"
    return s


async def send_email(settings: Settings, subject: str, html_body: str, chart: bytes | None) -> str:
    payload: dict = {"from": settings.digest_from, "to": [a.strip() for a in settings.digest_to.split(",")],
                     "subject": subject, "html": html_body}
    if chart:
        payload["attachments"] = [{"filename": "holdings.png", "content": base64.b64encode(chart).decode(),
                                   "content_type": "image/png", "content_id": CHART_CID}]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.resend.com/emails", json=payload,
                         headers={"Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}"})
    if r.is_error:
        raise RuntimeError(f"Resend rejected the email: HTTP {r.status_code} {r.text[:300]}")
    return r.json().get("id", "")


# ---------------------------------------------------------------- run


def is_local_hour(hour: int, tz: str, now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).hour == hour


@dataclass
class Digest:
    subject: str
    html: str
    chart: bytes | None
    snapshot: dict


async def build(settings: Settings, tz: str = "Europe/London") -> Digest:
    from .lookthrough.store import Store
    from .server import _fetch_positions, _fetch_summary

    summary, positions = await asyncio.gather(_fetch_summary(), _fetch_positions())
    previous = snapshots.load_latest(settings.database_url, snapshot_key(settings.env))
    rows = holding_rows(positions, previous)
    store = Store(settings.database_url)
    history = store.exposure_history()
    exposure = store.load_exposure(history[-1][1]) if history else None
    news = await fetch_news(news_targets(positions, exposure))
    chart = render_chart(rows)
    now = datetime.now(ZoneInfo(tz))
    return Digest(subject=subject_line(summary, previous, now),
                  html=render_html(summary, rows, news, previous, chart is not None, now),
                  chart=chart, snapshot=snapshots.make_snapshot(summary, positions))


async def run(settings: Settings, preview: Path | None = None) -> str:
    """Send the digest (or write a local preview). The baseline only advances after a successful send."""
    digest = await build(settings)
    if preview is not None:
        body = digest.html
        if digest.chart:
            chart_path = preview.with_suffix(".png")
            chart_path.write_bytes(digest.chart)
            body = body.replace(f"cid:{CHART_CID}", chart_path.name)
        preview.write_text(body)
        return f"Preview written to {preview}"
    missing = [n for n, v in (("T212_RESEND_API_KEY", settings.resend_api_key), ("T212_DIGEST_TO", settings.digest_to))
               if not v]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} to send the digest")
    email_id = await send_email(settings, digest.subject, digest.html, digest.chart)
    snapshots.save(settings.database_url, snapshot_key(settings.env), digest.snapshot)
    return f"Sent '{digest.subject}' (Resend id {email_id})"

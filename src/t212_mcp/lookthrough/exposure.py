"""Combine direct positions and fund look-through holdings into one exposure view. Pure functions."""

import re
from collections import defaultdict

from .countries import country_from_isin
from .extractors import FundHoldings

CASH_PATTERN = re.compile(r"\b(cash|receivable|payable|margin|forward|fx|tax|collateral|other|futures?)\b", re.I)
PAR_VALUE = re.compile(
    r"\s+(ORD\s+)?((USD|EUR|GBP|GBX|CHF|JPY|CAD|AUD|SEK|DKK|NOK|HKD|KRW|TWD)\s?[\d.]+|NPV)\b.*$", re.I
)


# Kept in capitals when title-casing an all-caps issuer name: legal-form suffixes and well-known abbreviations.
KEEP_UPPER = {"SA", "AG", "NV", "SE", "AB", "ASA", "PLC", "SPA", "LLC", "ADR", "ETF", "UK", "US", "USA", "SK", "LG",
              "ASML", "HSBC", "BP", "BHP", "AMD", "IBM", "TSMC", "UBS", "SAP", "BYD", "AIA", "NXP", "KLA", "TJX", "RELX"}
KEEP_LOWER = {"of", "and", "the", "de", "la", "du", "von"}


def _title_word(word: str, first: bool) -> str:
    core = word.strip("().,&-/")
    if core.upper() in KEEP_UPPER:
        return word.upper()
    if not first and core.lower() in KEEP_LOWER:
        return word.lower()
    # Title-case each part, keeping web suffixes lower: AMAZON.COM -> Amazon.com, MERCADOLIBRE -> Mercadolibre
    parts = word.split(".")
    return ".".join(p.lower() if i and p.lower() in ("com", "net", "io") else p.capitalize() for i, p in enumerate(parts))


def clean_name(name: str) -> str:
    s = PAR_VALUE.sub("", name).strip(" -,")
    if not s.isupper():
        return s
    return " ".join(_title_word(w, i == 0) for i, w in enumerate(s.split()))


def _better_name(current: str | None, candidate: str, direct: bool) -> str:
    if current is None or direct:
        return candidate if direct else clean_name(candidate)
    # Prefer mixed-case names (usually the issuer's proper name) over SHOUTING ones.
    if current.isupper() or (not any(c.islower() for c in current) and any(c.islower() for c in candidate)):
        return clean_name(candidate)
    return current


def compute_exposure(
    positions: list[dict],
    fund_isins: set[str],
    funds: dict[str, FundHoldings | None],
    fund_status: dict[str, str],
    top_n: int = 25,
    entities: dict[str, dict] | None = None,
) -> dict:
    """positions: normalized T212 positions (ticker, name, isin, value). funds: holdings per fund ISIN.
    entities: isin -> {key, name}; securities sharing a key (share classes, ADRs of one company) are added up."""
    entities = entities or {}
    invested = sum(p["value"] or 0.0 for p in positions)
    securities: dict[str, dict] = {}
    countries: dict[str, float] = defaultdict(float)
    sectors: dict[str, float] = defaultdict(float)
    cash_inside_funds = 0.0
    uncovered = 0.0
    fund_rows = []

    def add_security(key: str, name: str, value: float, via: str | None, isin: str | None):
        entity = entities.get(isin) if isin else None
        if entity:
            key = f"entity:{entity['key']}"
        entry = securities.setdefault(key, {"name": None, "canonical": None, "direct": 0.0, "via": defaultdict(float),
                                            "members": defaultdict(float)})
        entry["name"] = _better_name(entry["name"], name, direct=via is None)
        if entity and entity.get("name"):
            entry["canonical"] = entity["name"]
        entry["members"][isin] += value
        if via is None:
            entry["direct"] += value
        else:
            entry["via"][via] += value

    for p in positions:
        value = p["value"] or 0.0
        ticker = p["ticker"]
        if p.get("isin") in fund_isins:
            fh = funds.get(p["isin"])
            status = fund_status.get(p["isin"], "missing")
            fund_rows.append(
                {
                    "ticker": ticker,
                    "name": p["name"],
                    "value": round(value, 2),
                    "status": status,
                    "coverage": fh.coverage if fh else None,
                    "as_of": fh.as_of if fh else None,
                    "holdings_count": len(fh.holdings) if fh else 0,
                }
            )
            if fh is None:
                uncovered += value
                countries["Unknown (no fund data)"] += value
                sectors["Unknown (no fund data)"] += value
                continue
            listed = sum(h.weight_pct for h in fh.holdings)
            for h in fh.holdings:
                v = value * h.weight_pct / 100
                if not h.isin and CASH_PATTERN.search(h.name):
                    cash_inside_funds += v
                    continue
                key = h.isin or f"name:{clean_name(h.name).lower()}"
                add_security(key, h.name, v, ticker, h.isin)
                if not fh.countries:
                    countries[h.country or country_from_isin(h.isin) or "Unknown"] += v
                sectors[h.sector or "Unknown"] += v
            if fh.countries:
                total = sum(fh.countries.values()) or 100.0
                for c, w in fh.countries.items():
                    countries[c] += value * (w / total) * min(listed, 100.0) / 100
            if listed < 100:  # partial coverage: the unlisted remainder is unknown
                rest = value * (100 - listed) / 100
                countries["Unknown (not disclosed)"] += rest
                sectors["Unknown (not disclosed)"] += rest
        else:
            add_security(p.get("isin") or f"name:{p['name'].lower()}", p["name"], value, None, p.get("isin"))
            countries[country_from_isin(p.get("isin")) or "Unknown"] += value
            sectors["Unknown"] += value

    def pct(v: float) -> float:
        return round(v / invested * 100, 2) if invested else 0.0

    rows = []
    for key, e in securities.items():
        total = e["direct"] + sum(e["via"].values())
        members = sorted(e["members"].items(), key=lambda kv: -kv[1])
        name = e["canonical"] or e["name"]
        if len(members) > 1 and not e["canonical"] and key.startswith("entity:"):
            name = clean_name(key.split(":", 1)[1])  # the company, not whichever share line was bigger
        row = {
            "name": name,
            "isin": members[0][0],  # the largest line
            "value": round(total, 2),
            "pct_of_portfolio": pct(total),
            "direct": round(e["direct"], 2),
            "via_funds": {k: round(v, 2) for k, v in sorted(e["via"].items(), key=lambda kv: -kv[1])},
        }
        if len(members) > 1:  # several ISINs of one company
            row["members"] = [{"isin": i, "value": round(v, 2)} for i, v in members]
        rows.append(row)
    rows.sort(key=lambda r: -r["value"])

    def ranked(d: dict[str, float]) -> list[dict]:
        return [{"name": k, "value": round(v, 2), "pct": pct(v)} for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if v > 0.005]

    covered = sum(r["value"] for r in fund_rows if r["status"] in ("ok", "stale"))
    fund_total = sum(r["value"] for r in fund_rows)
    return {
        "invested_value": round(invested, 2),
        "securities_count": len(rows),
        "top_securities": rows[:top_n],
        "all_securities": rows,
        "top10_pct": round(sum(r["pct_of_portfolio"] for r in rows[:10]), 2),
        "countries": ranked(countries),
        "sectors": ranked(sectors),
        "cash_inside_funds": round(cash_inside_funds, 2),
        "coverage": {
            "funds_value": round(fund_total, 2),
            "funds_with_data_pct": round(covered / fund_total * 100, 1) if fund_total else None,
            "uncovered_value": round(uncovered, 2),
            "funds": fund_rows,
        },
    }


def exposure_changes(old: dict, new: dict, top_n: int = 10) -> dict:
    def by_key(rows: list[dict], key: str) -> dict[str, dict]:
        return {r[key] or r["name"]: r for r in rows}

    old_sec = by_key(old.get("all_securities", old["top_securities"]), "isin")
    new_sec = by_key(new.get("all_securities", new["top_securities"]), "isin")
    deltas = []
    for k in old_sec.keys() | new_sec.keys():
        o, n = old_sec.get(k), new_sec.get(k)
        d = (n["pct_of_portfolio"] if n else 0.0) - (o["pct_of_portfolio"] if o else 0.0)
        deltas.append({"name": (n or o)["name"], "isin": (n or o)["isin"], "pct_change": round(d, 2),
                       "from_pct": o["pct_of_portfolio"] if o else 0.0, "to_pct": n["pct_of_portfolio"] if n else 0.0})
    deltas.sort(key=lambda x: -abs(x["pct_change"]))
    oc = {c["name"]: c["pct"] for c in old["countries"]}
    nc = {c["name"]: c["pct"] for c in new["countries"]}
    cdeltas = sorted(
        ({"country": k, "pct_change": round(nc.get(k, 0) - oc.get(k, 0), 2), "to_pct": nc.get(k, 0)} for k in oc.keys() | nc.keys()),
        key=lambda x: -abs(x["pct_change"]),
    )
    return {
        "invested_value_change": round(new["invested_value"] - old["invested_value"], 2),
        "biggest_security_shifts": [d for d in deltas[:top_n] if d["pct_change"]],
        "biggest_country_shifts": [d for d in cdeltas[:top_n] if d["pct_change"]],
    }

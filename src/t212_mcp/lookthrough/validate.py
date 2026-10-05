"""Deterministic checks that a recipe's output really is this fund's holdings. The agent cannot override these."""

import re
from datetime import date

from pydantic import BaseModel

from .extractors import FundHoldings

WEIGHT_SUM_RANGE = (97.0, 103.0)
MIN_ISIN_VALID = 0.90
MAX_AGE_DAYS = 10
MIN_HOLDINGS = 10
MAX_TURNOVER = 0.40
STOPWORDS = {"ucits", "etf", "acc", "dist", "usd", "gbp", "eur", "the", "fund", "plc", "index", "and", "of", "(acc)", "(dist)"}


class ValidationReport(BaseModel):
    ok: bool
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict = {}


def isin_checksum_ok(isin: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin or ""):
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(isin[-1])


def turnover(prev: FundHoldings, cur: FundHoldings) -> float | None:
    def weights(fh: FundHoldings) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in fh.holdings:
            if h.isin:
                out[h.isin] = out.get(h.isin, 0.0) + h.weight_pct
        return out

    a, b = weights(prev), weights(cur)
    if not a or not b:
        return None
    total = sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in a.keys() | b.keys())
    return total / 2 / 100


def identity_evidence(fh: FundHoldings, fund_isin: str, ticker: str, fund_name: str) -> str | None:
    haystack = f"{fh.source_url}\n{fh.source_excerpt}".lower()
    if fund_isin.lower() in haystack:
        return "fund ISIN appears in source"
    if ticker and re.search(rf"\b{re.escape(ticker.lower())}\b", haystack):
        return f"ticker {ticker} appears in source"
    tokens = [t for t in re.findall(r"[a-z0-9]+", fund_name.lower()) if t not in STOPWORDS and len(t) > 2]
    if tokens:
        hit = sum(1 for t in tokens if t in haystack)
        if hit / len(tokens) >= 0.6:
            return f"{hit}/{len(tokens)} fund-name words appear in source"
    return None


def validate(
    fh: FundHoldings,
    fund_isin: str,
    ticker: str,
    fund_name: str,
    previous: FundHoldings | None = None,
    today: date | None = None,
) -> ValidationReport:
    today = today or date.today()
    errors: list[str] = []
    warnings: list[str] = []
    is_static = fh.source_url == "static"
    weight_sum = sum(h.weight_pct for h in fh.holdings)
    isins = [h.isin for h in fh.holdings if h.isin]
    valid_isins = sum(1 for i in isins if isin_checksum_ok(i))
    stats = {
        "holdings": len(fh.holdings),
        "weight_sum_pct": round(weight_sum, 2),
        "with_isin": len(isins),
        "isin_checksum_ok": valid_isins,
        "as_of": fh.as_of,
        "top5": [f"{h.name} {h.weight_pct:.2f}%" for h in sorted(fh.holdings, key=lambda h: -h.weight_pct)[:5]],
    }

    if fh.coverage == "full" or is_static:
        lo, hi = WEIGHT_SUM_RANGE
        if not lo <= weight_sum <= hi:
            hint = " (weights look like fractions: set weight_scale='fraction')" if 0.9 <= weight_sum <= 1.1 else ""
            errors.append(f"weights sum to {weight_sum:.2f}%, expected {lo}-{hi}%{hint}")
    else:
        if not 1 <= weight_sum <= 103:
            errors.append(f"partial holdings weights sum to {weight_sum:.2f}%, expected 1-103%")

    if not is_static:
        if fh.coverage == "full" and len(fh.holdings) < MIN_HOLDINGS:
            errors.append(f"only {len(fh.holdings)} holdings; a full holdings list should have at least {MIN_HOLDINGS}")
        if not isins:
            warnings.append("source has no ISINs; holdings cannot be joined across funds")
        elif valid_isins / len(isins) < MIN_ISIN_VALID:
            errors.append(f"only {valid_isins}/{len(isins)} ISINs pass the checksum; the ISIN column is probably wrong")

        if fh.as_of is None:
            warnings.append("holdings date unknown")
        else:
            age = (today - date.fromisoformat(fh.as_of)).days
            stats["age_days"] = age
            if age > MAX_AGE_DAYS:
                errors.append(f"holdings are {age} days old (as of {fh.as_of}); max {MAX_AGE_DAYS}")
            if age < -1:
                errors.append(f"holdings date {fh.as_of} is in the future; as_of is probably parsed from the wrong cell")

        evidence = identity_evidence(fh, fund_isin, ticker, fund_name)
        to = turnover(previous, fh) if previous else None
        if to is not None:
            stats["turnover_vs_previous"] = round(to, 3)
            if to > MAX_TURNOVER:
                errors.append(f"{to:.0%} turnover vs last good snapshot; this may be a different fund")
            elif not evidence:
                evidence = "consistent with previous snapshot"
        if not evidence:
            errors.append(
                "cannot confirm the data belongs to this fund: its ISIN, ticker or name does not appear in the "
                "source URL or file header"
            )
        stats["identity"] = evidence

    return ValidationReport(ok=not errors, errors=errors, warnings=warnings, stats=stats)

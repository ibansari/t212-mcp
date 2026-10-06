"""Issuer sources verified against the live sites. Tried after saved recipes and before the discovery agent (no
LLM needed when one works), and shown to the agent as a starting point when they no longer do."""

import re

from .recipes import ColumnMap, Recipe

KNOWN_SOURCES: list[Recipe] = [
    Recipe(
        kind="http_file", scope="issuer", issuer="HSBC", issuer_match=r"^HSBC\b",
        url="https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/{isin_lower}/gb/en/holdings",
        file_format="xls", header_contains="ISIN", as_of="cell_right_of:Date",
        columns=ColumnMap(name="SecurityName", weight="Weighting", isin="ISIN", country="Country"),
        notes="Daily holdings file per ISIN. Verified 2026-10-06.",
    ),
    Recipe(
        kind="browser_json", scope="issuer", issuer="Invesco", issuer_match=r"^Invesco\b",
        page_url="https://www.invesco.com/uk/en/",
        url="https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/{isin}/holdings/index?idType=isin",
        file_format="json", json_holdings_path="holdings", as_of="json:effectiveDate",
        columns=ColumnMap(name="name", weight="weight", isin="isin"),
        notes=("Invesco's dng-api answers only fetches made from a browser session on invesco.com; plain HTTP, or "
               "a wrong query parameter, gets HTTP 406 with an empty body. Responses are JSON sent as text/plain. "
               "Verified 2026-10-06."),
    ),
]


def matching(fund_name: str) -> list[Recipe]:
    out = []
    for r in KNOWN_SOURCES:
        try:
            if re.search(r.issuer_match, fund_name, re.I):
                out.append(r)
        except re.error:
            continue
    return out


def hint(fund_name: str) -> str:
    """Prompt text describing known sources for this fund's issuer, or ''."""
    recipes = matching(fund_name)
    if not recipes:
        return ""
    lines = ["Known source for this issuer (it worked before; check it first, it may have changed):"]
    for r in recipes:
        lines.append(r.model_dump_json(exclude_none=True, exclude={"scope", "issuer_match", "coverage", "weight_scale"}))
    return "\n".join(lines)

"""Country name normalisation and ISIN-prefix fallback."""

import re

ISIN_PREFIX = {
    "US": "United States", "GB": "United Kingdom", "IE": "Ireland", "NL": "Netherlands", "DE": "Germany",
    "FR": "France", "CH": "Switzerland", "JP": "Japan", "KR": "South Korea", "TW": "Taiwan", "CN": "China",
    "HK": "Hong Kong", "KY": "Cayman Islands", "BM": "Bermuda", "CA": "Canada", "AU": "Australia",
    "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland", "ES": "Spain", "IT": "Italy",
    "BE": "Belgium", "AT": "Austria", "IN": "India", "BR": "Brazil", "SA": "Saudi Arabia",
    "AE": "United Arab Emirates", "ZA": "South Africa", "MX": "Mexico", "SG": "Singapore", "ID": "Indonesia",
    "MY": "Malaysia", "TH": "Thailand", "IL": "Israel", "JE": "Jersey", "LU": "Luxembourg", "PT": "Portugal",
    "NZ": "New Zealand", "QA": "Qatar", "KW": "Kuwait", "TR": "Turkey", "CL": "Chile", "PL": "Poland",
    "GR": "Greece", "PH": "Philippines", "CO": "Colombia", "PE": "Peru", "EG": "Egypt", "HU": "Hungary",
    "CZ": "Czech Republic", "AR": "Argentina", "VG": "British Virgin Islands", "GG": "Guernsey",
    "CW": "Curacao", "PA": "Panama", "LR": "Liberia", "MU": "Mauritius", "CY": "Cyprus",
}

ALIASES = {
    "usa": "United States", "us": "United States", "united states of america": "United States",
    "uk": "United Kingdom", "great britain": "United Kingdom", "korea": "South Korea",
    "korea, republic of": "South Korea", "republic of korea": "South Korea", "korea (south)": "South Korea",
    "taiwan, province of china": "Taiwan", "chinese taipei": "Taiwan", "hongkong": "Hong Kong",
    "uae": "United Arab Emirates", "russian federation": "Russia",
}


def normalize_country(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "none", "-", "n/a"}:
        return None
    if len(s) == 2 and s.upper() in ISIN_PREFIX:
        return ISIN_PREFIX[s.upper()]
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)  # UnitedStates -> United States
    alias = ALIASES.get(s.lower())
    if alias:
        return alias
    return s if not s.islower() and not s.isupper() else s.title()


def country_from_isin(isin: str | None) -> str | None:
    if not isin or len(isin) < 2:
        return None
    return ISIN_PREFIX.get(isin[:2].upper())

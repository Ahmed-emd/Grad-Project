from __future__ import annotations

import re


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\s-]", "", value)
    value = re.sub(r"[\s-]+", "-", value).strip("-")
    return value or "item"


def money_fmt(currency: str, minor: int) -> str:
    # Simple formatting for graduation project (no locale).
    major = minor / 100.0
    if currency.upper() in {"EGP", "USD", "EUR", "GBP"}:
        return f"{currency.upper()} {major:,.2f}"
    return f"{currency.upper()} {major:,.2f}"


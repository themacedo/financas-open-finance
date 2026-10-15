"""Money helpers: integer cents everywhere, BRL formatting and parsing."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def to_cents(value: Any) -> int:
    """JSON number/str -> int cents (no float arithmetic)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value * 100
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"valor inválido: {value!r}") from exc
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> float:
    return round(cents / 100, 2)


def brl(cents: int, signed: bool = False) -> str:
    """12345 -> 'R$ 123,45'; negative -> '-R$ 123,45'."""
    neg = cents < 0
    cents = abs(cents)
    reais, cent = divmod(cents, 100)
    body = f"{reais:,}".replace(",", ".") + f",{cent:02d}"
    if neg:
        return f"-R$ {body}"
    return f"+R$ {body}" if signed and cents else f"R$ {body}"


_BRL_RE = re.compile(r"^\s*(?:R\$\s*)?(-)?\s*([\d.]*\d)(?:,(\d{1,2}))?\s*$")


def parse_brl(text: str) -> int:
    """Accepts '1200', '1.200,50', 'R$ 1.200', '1200.50' -> cents."""
    text = text.strip()
    if re.fullmatch(r"-?\d+\.\d{1,2}", text):
        return to_cents(text)
    m = _BRL_RE.match(text)
    if not m:
        raise ValueError(f"valor inválido: {text!r}")
    sign, whole, frac = m.groups()
    cents = int(whole.replace(".", "")) * 100 + int((frac or "0").ljust(2, "0"))
    return -cents if sign else cents

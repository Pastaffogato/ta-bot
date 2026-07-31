"""Argument parsing helpers for command handlers.

Regex patterns, expiry parsing, price resolution, and relative-pip
conversion. All functions are pure (no I/O or async).
"""

import re
from datetime import datetime, timezone
from typing import Optional

# ── regex patterns ──

_EXPIRY_RE = re.compile(r'^(\d+)([smh])$', re.IGNORECASE)
_REL_RE = re.compile(r'^([+-])(\d+(?:\.\d+)?)$')


# ── expiry ──

def parse_expiry(arg: str) -> tuple[int, str] | None:
    """Parse expiry suffix. Returns (seconds, display_label) or None.

    Examples: "30m" → (1800, "30m"), "2h" → (7200, "2h"), "45s" → (45, "45s").
    """
    m = _EXPIRY_RE.match(arg)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2).lower()
    if unit == 's':
        return val, f"{val}s"
    elif unit == 'm':
        return val * 60, f"{val}m"
    else:  # 'h'
        return val * 3600, f"{val}h"


def fmt_expiry(expires_at: str | None) -> str:
    """Format expiry as relative time. Returns empty string if no expiry."""
    if not expires_at:
        return ""
    try:
        exp_dt = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return ""
    now = datetime.now(timezone.utc)
    remaining = exp_dt - now
    total_s = int(remaining.total_seconds())
    if total_s <= 0:
        return " (expired)"
    if total_s < 60:
        return f" (expires in {total_s}s)"
    if total_s < 3600:
        return f" (expires in {total_s // 60}m)"
    return f" (expires in {total_s // 3600}h{total_s % 3600 // 60}m)"


# ── mark args ──

def parse_mark_args(args: list[str]) -> tuple[list[float], int | None, str | None]:
    """Separate prices and optional expiry suffix from mark args.

    Returns (prices, expiry_seconds, expiry_label).
    """
    prices = []
    expiry_s = None
    expiry_label = None
    for a in args:
        exp = parse_expiry(a)
        if exp is not None:
            expiry_s, expiry_label = exp
        else:
            try:
                prices.append(float(a))
            except ValueError:
                pass  # skip non-price, non-expiry args
    return prices, expiry_s, expiry_label


# ── price alert args ──

def resolve_price_args(
    args: list[str],
    current_price: float,
    pip_size: float,
) -> tuple[str, list[float], int | None, str | None]:
    """Parse price alert args. Returns (alert_type, boundaries, expiry_s, expiry_label).

    - alert_type: "crossing" or "close"
    - boundaries: absolute prices (sorted for close, 1-2 for close, 1+ for crossing)
    - expiry_s, expiry_label: parsed from suffix

    Supports:
      - "close" keyword → close-type alert
      - +N / -N  → relative pips from current_price
      - absolute prices
      - 30m / 2h / 45s → expiry suffix
    """
    alert_type = "crossing"
    expiry_s = None
    expiry_label = None
    raw_prices: list[float] = []
    relative_pips: list[tuple[str, float]] = []

    for a in args:
        if a.lower() == "close":
            alert_type = "close"
            continue

        exp = parse_expiry(a)
        if exp is not None:
            expiry_s, expiry_label = exp
            continue

        rel = _REL_RE.match(a)
        if rel:
            relative_pips.append((rel.group(1), float(rel.group(2))))
            continue

        try:
            raw_prices.append(float(a))
        except ValueError:
            pass  # skip unrecognized

    # Resolve relative prices to absolute
    boundaries = list(raw_prices)
    for sign, pips in relative_pips:
        offset = pips * pip_size
        if sign == "+":
            boundaries.append(current_price + offset)
        else:
            boundaries.append(current_price - offset)

    # For close type: auto-sort, max 2 boundaries
    if alert_type == "close":
        boundaries = sorted(boundaries)
        if len(boundaries) > 2:
            boundaries = boundaries[:2]

    return alert_type, boundaries, expiry_s, expiry_label


# ── relative price ──

def resolve_relative_price(val: str, base_price: float, pip_size: float) -> float:
    """Parse a price value — absolute or relative (+/- pips)."""
    rel = _REL_RE.match(val)
    if rel:
        sign = rel.group(1)
        pips = float(rel.group(2))
        offset = pips * pip_size
        return base_price + offset if sign == "+" else base_price - offset
    try:
        return float(val)
    except ValueError:
        return base_price  # fallback
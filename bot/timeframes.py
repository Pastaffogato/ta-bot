"""Timeframe parsing: normalize user input to minute integers."""

import re
from typing import Optional

# Supported minute-based timeframes
SUPPORTED = {1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440, 10080, 43200}


def parse_tf(raw: str) -> Optional[int]:
    """Parse a timeframe string to minutes.

    Accepts:
      - bare numbers: "3", "5", "15"  → minutes
      - m-prefixed: "m3", "M5", "M15" → minutes
      - h-prefixed: "h1", "H4"        → hours → minutes
      - d-prefixed: "d1"              → days → minutes
      - w-prefixed: "w1"              → weeks → minutes

    Returns None for unrecognized input.
    """
    raw = raw.strip().lower()
    if not raw:
        return None

    # bare number
    if raw.isdigit():
        mins = int(raw)
        return mins if mins in SUPPORTED else None

    # letter + number
    m = re.match(r"^([mhdw])(\d+)$", raw)
    if m:
        unit = m.group(1)
        val = int(m.group(2))
        if unit == "m":
            return val if val in SUPPORTED else None
        elif unit == "h":
            mins = val * 60
            return mins if mins in SUPPORTED else None
        elif unit == "d":
            mins = val * 1440
            return mins if mins in SUPPORTED else None
        elif unit == "w":
            mins = val * 10080
            return mins if mins in SUPPORTED else None

    return None


def tf_label(minutes: int) -> str:
    """Human-readable timeframe label: 3 → 'M3', 60 → 'H1', 1440 → 'D1'."""
    if minutes < 60:
        return f"M{minutes}"
    elif minutes < 1440:
        return f"H{minutes // 60}"
    elif minutes < 10080:
        return f"D{minutes // 1440}"
    else:
        return f"W{minutes // 10080}"
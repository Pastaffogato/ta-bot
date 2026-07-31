"""Candle pattern classification.

Returns a short label and emoji for the current candle's shape.
All detection uses the (incomplete) current bar — patterns may shift
before the candle closes.
"""

from dataclasses import dataclass
from typing import Optional
from bot.mt5_data import Bar


@dataclass
class Pattern:
    emoji: str
    label: str       # e.g. "BULLISH", "DOJI"
    bias: str        # "bullish", "bearish", "neutral"


def classify(bar: Bar, prev_bar: Optional[Bar] = None) -> Pattern:
    """Classify a single candle. If prev_bar is provided, also checks engulfing."""
    o, h, l, c = bar.open, bar.high, bar.low, bar.close

    if h <= l or o <= 0:
        return Pattern("❓", "FLAT", "neutral")

    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    total_range = h - l
    body_ratio = body / total_range if total_range > 0 else 0
    direction = "bullish" if c > o else "bearish"

    # Engulfing: current close engulfs previous open of opposite direction.
    # Require meaningful body (> 5% of range) — dojis can't engulf.
    # Ignore open vs previous close — spread makes that unreliable.
    if body_ratio >= 0.05 and prev_bar and prev_bar.high > prev_bar.low:
        prev_dir = "bullish" if prev_bar.close > prev_bar.open else "bearish"
        # Bearish engulfing: current bearish, previous bullish, close < previous open
        if direction == "bearish" and prev_dir == "bullish":
            if c < prev_bar.open:
                return Pattern("🐻", "BEAR ENGULF", "bearish")
        # Bullish engulfing: current bullish, previous bearish, close > previous open
        if direction == "bullish" and prev_dir == "bearish":
            if c > prev_bar.open:
                return Pattern("🐂", "BULL ENGULF", "bullish")

    # Hammer: long lower shadow, tiny upper shadow
    if lower_shadow > 0.6 * total_range and upper_shadow < 0.15 * total_range:
        return Pattern("🔨", "HAMMER", "bullish")

    # Shooting star: long upper shadow, tiny lower shadow
    if upper_shadow > 0.6 * total_range and lower_shadow < 0.15 * total_range:
        return Pattern("⭐", "SHOOTING STAR", "bearish")

    # Doji: body < 10% of range
    if body_ratio < 0.10:
        return Pattern("🟡", "DOJI", "neutral")

    # Bullish / Bearish
    if direction == "bullish":
        return Pattern("🟢", "BULLISH", "bullish")
    else:
        return Pattern("🔴", "BEARISH", "bearish")
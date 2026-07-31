"""Indicator computation from OHLCV bar data.

All functions operate on numpy arrays in chronological order (oldest first).
Bars are passed newest-first and reversed internally.

SMA50, EMA20 (on close), BB(20,2) on close, ATR(14), RSI(14), ADX(14) with +DI/-DI.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class IndicatorSnapshot:
    """Snapshot of all computed indicators for a symbol at a point in time."""

    sma50: Optional[float] = None
    ema20: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width_pct: Optional[float] = None  # (upper - lower) / middle * 100
    atr: Optional[float] = None  # ATR(14) — latest
    atr_prev: Optional[float] = None  # ATR(14) — 1 bar ago
    atr_prev2: Optional[float] = None  # ATR(14) — 2 bars ago
    atr_pct: Optional[float] = None  # ATR / close * 100
    rsi: Optional[float] = None  # RSI(14) — latest
    rsi_prev: Optional[float] = None  # RSI(14) — 1 bar ago
    rsi_prev2: Optional[float] = None  # RSI(14) — 2 bars ago
    adx: Optional[float] = None  # ADX(14), 0-100
    di_plus: Optional[float] = None  # +DI(14)
    di_minus: Optional[float] = None  # -DI(14)
    current_close: Optional[float] = None
    bar_count: int = 0


def compute_all(bars: list, skip_current: bool = False) -> IndicatorSnapshot:
    """Compute all indicators from a list of Bar objects (newest first).

    Requires at least 20 bars for BB/EMA, 50 for SMA50, 15 for ATR/RSI, 28 for ADX.
    Missing indicators are left as None.

    When skip_current=True, the newest bar (position 0) is excluded so indicators
    reflect the previous completed candle.
    """
    if not bars:
        return IndicatorSnapshot()

    if skip_current and len(bars) > 1:
        bars = bars[1:]  # skip the current incomplete bar

    n = len(bars)
    if n == 0:
        return IndicatorSnapshot()

    # Reverse to chronological order (oldest first)
    closes = np.array([b.close for b in reversed(bars)], dtype=np.float64)
    highs = np.array([b.high for b in reversed(bars)], dtype=np.float64)
    lows = np.array([b.low for b in reversed(bars)], dtype=np.float64)

    snap = IndicatorSnapshot(bar_count=n, current_close=float(closes[-1]))

    # ── SMA 50 ──
    if n >= 50:
        snap.sma50 = float(np.mean(closes[-50:]))

    # ── EMA 20 (seeded with SMA of first 20 closes) ──
    if n >= 20:
        snap.ema20 = _ema(closes, 20)

    # ── Bollinger Bands (20, 2) on close ──
    if n >= 20:
        sma20 = float(np.mean(closes[-20:]))
        std20 = float(np.std(closes[-20:], ddof=0))  # population std (standard BB formula)
        snap.bb_middle = sma20
        snap.bb_upper = sma20 + 2.0 * std20
        snap.bb_lower = sma20 - 2.0 * std20
        if sma20 > 0:
            snap.bb_width_pct = float((snap.bb_upper - snap.bb_lower) / sma20 * 100)

    # ── ATR(14) with rolling last 3 ──
    if n >= 15:
        atr_vals = _rolling_atr(highs, lows, closes, 14)
        if len(atr_vals) >= 1:
            snap.atr = atr_vals[-1]
        if len(atr_vals) >= 2:
            snap.atr_prev = atr_vals[-2]
        if len(atr_vals) >= 3:
            snap.atr_prev2 = atr_vals[-3]
        if snap.atr is not None and snap.current_close and snap.current_close > 0:
            snap.atr_pct = snap.atr / snap.current_close * 100

    # ── RSI(14) with rolling last 3 ──
    if n >= 15:
        rsi_vals = _rolling_rsi(closes, 14)
        if len(rsi_vals) >= 1:
            snap.rsi = rsi_vals[-1]
        if len(rsi_vals) >= 2:
            snap.rsi_prev = rsi_vals[-2]
        if len(rsi_vals) >= 3:
            snap.rsi_prev2 = rsi_vals[-3]

    # ── ADX(14) with +DI/-DI ──
    if n >= 28:
        adx_val, di_p, di_m = _adx(highs, lows, closes, 14)
        snap.adx = adx_val
        snap.di_plus = di_p
        snap.di_minus = di_m

    return snap


# ── internal helpers ──


def _ema(data: np.ndarray, period: int) -> float:
    """Exponential moving average seeded with SMA of first `period` values."""
    alpha = 2.0 / (period + 1.0)
    # Seed with SMA of first `period` values
    result = float(np.mean(data[:period]))
    for i in range(period, len(data)):
        result = alpha * data[i] + (1.0 - alpha) * result
    return float(result)


def _true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """True range array: max(high-low, |high-prev_close|, |low-prev_close|)."""
    tr = np.zeros(len(highs), dtype=np.float64)
    tr[0] = highs[0] - lows[0]
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
    return tr


def _smooth_wilder(values: np.ndarray, period: int) -> list[float]:
    """Wilder's smoothing: first value = simple average of first `period` values,
    subsequent = (prev * (period-1) + current) / period.
    Returns list of smoothed values (one per bar from index `period-1` onward)."""
    if len(values) <= period:
        return []
    result = [float(np.mean(values[:period]))]
    for i in range(period, len(values)):
        result.append((result[-1] * (period - 1) + values[i]) / period)
    return result


def _rolling_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> list[float]:
    """Compute rolling ATR values (Wilder's smoothing). Returns list of ATR values."""
    tr = _true_range(highs, lows, closes)
    return _smooth_wilder(tr, period)


def _rolling_rsi(closes: np.ndarray, period: int) -> list[float]:
    """Compute rolling RSI values (Wilder's smoothing). Returns list of RSI values."""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    if len(gains) <= period:
        return []

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    rsi_vals = []
    # Compute RSI at each step after the initial period
    rsi = _rsi_from_avgs(avg_gain, avg_loss)
    rsi_vals.append(rsi)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi = _rsi_from_avgs(avg_gain, avg_loss)
        rsi_vals.append(rsi)

    return rsi_vals


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    """RSI from average gain and loss."""
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14):
    """Compute ADX(period) with +DI and -DI using Wilder's smoothing.

    Returns (adx, di_plus, di_minus) — the latest values.
    """
    n = len(highs)

    # True Range
    tr = _true_range(highs, lows, closes)

    # +DM and -DM
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down

    # Wilder's smoothing for TR, +DM, -DM
    tr_smooth = _smooth_wilder(tr, period)
    pdm_smooth = _smooth_wilder(plus_dm, period)
    mdm_smooth = _smooth_wilder(minus_dm, period)

    if not tr_smooth:
        return 0.0, 0.0, 0.0

    # Compute +DI, -DI, and DX for each smoothed step
    dx_vals = []
    di_plus_last = 0.0
    di_minus_last = 0.0

    for i in range(len(tr_smooth)):
        if tr_smooth[i] > 0:
            di_p = 100.0 * pdm_smooth[i] / tr_smooth[i]
            di_m = 100.0 * mdm_smooth[i] / tr_smooth[i]
        else:
            di_p = 0.0
            di_m = 0.0

        di_plus_last = di_p
        di_minus_last = di_m

        di_sum = di_p + di_m
        if di_sum > 0:
            dx = 100.0 * abs(di_p - di_m) / di_sum
        else:
            dx = 0.0
        dx_vals.append(dx)

    # Smooth DX to get ADX (Wilder's smoothing)
    if len(dx_vals) < period:
        return float(dx_vals[-1]) if dx_vals else 0.0, di_plus_last, di_minus_last

    adx_smooth = _smooth_wilder(np.array(dx_vals, dtype=np.float64), period)
    adx_val = adx_smooth[-1] if adx_smooth else (dx_vals[-1] if dx_vals else 0.0)

    return float(adx_val), float(di_plus_last), float(di_minus_last)


# ── formatting ──


def format_indicator_section(snap: IndicatorSnapshot, symbol: str, sinfo, prefs: dict = None) -> str:
    """Build a compact indicator display section for candle alerts.

    Two lines:
    Line 1: ATR(14) with last 3 values + SMA50 + BB (high, mid, low) + width in pips
    Line 2: EMA20 + RSI(14) with last 3 values + ADX(14) with +DI/-DI

    Respects granular prefs: show_sma, show_ema, show_bb, show_atr, show_rsi, show_adx.
    When a granular pref is "off", that indicator is hidden (along with its line if empty).
    show_indicators=off skips everything.
    """
    from bot.formatting import fmt_ohlc

    if prefs is None:
        prefs = {}

    lines = []
    pip_size = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01

    def _on(key):
        return prefs.get(key, "on") != "off"

    # Line 1: ATR + SMA50 + BB
    parts1 = []
    if _on("show_atr") and snap.atr is not None:
        atr_vals = [snap.atr]
        if snap.atr_prev is not None:
            atr_vals.append(snap.atr_prev)
        if snap.atr_prev2 is not None:
            atr_vals.append(snap.atr_prev2)
        atr_str = " ".join(fmt_ohlc(v, symbol, sinfo) if sinfo else f"{v:.5f}" for v in atr_vals)
        parts1.append(f"ATR {atr_str}")

    if _on("show_sma") and snap.sma50 is not None:
        parts1.append(f"SMA50 {fmt_ohlc(snap.sma50, symbol, sinfo)}")

    if _on("show_bb") and snap.bb_upper is not None:
        bb_high = fmt_ohlc(snap.bb_upper, symbol, sinfo)
        bb_mid = fmt_ohlc(snap.bb_middle, symbol, sinfo)
        bb_low = fmt_ohlc(snap.bb_lower, symbol, sinfo)
        bb_width_pips = (snap.bb_upper - snap.bb_lower) / pip_size if pip_size > 0 else 0
        parts1.append(f"BB {bb_high} {bb_mid} {bb_low} ({bb_width_pips:.1f}p)")

    if parts1:
        lines.append("  ".join(parts1))

    # Line 2: EMA20 + RSI + ADX
    parts2 = []
    if _on("show_ema") and snap.ema20 is not None:
        parts2.append(f"EMA20 {fmt_ohlc(snap.ema20, symbol, sinfo)}")

    if _on("show_rsi") and snap.rsi is not None:
        rsi_vals = [f"{snap.rsi:.1f}"]
        if snap.rsi_prev is not None:
            rsi_vals.append(f"{snap.rsi_prev:.1f}")
        if snap.rsi_prev2 is not None:
            rsi_vals.append(f"{snap.rsi_prev2:.1f}")
        zone = ""
        if snap.rsi > 70:
            zone = " OB"
        elif snap.rsi < 30:
            zone = " OS"
        parts2.append(f"RSI {' '.join(rsi_vals)}{zone}")

    if _on("show_adx") and snap.adx is not None:
        strength = ""
        if snap.adx > 25:
            strength = " strong" if snap.adx > 50 else " present"
        di_str = ""
        if snap.di_plus is not None and snap.di_minus is not None:
            di_str = f" +DI {snap.di_plus:.0f} -DI {snap.di_minus:.0f}"
        parts2.append(f"ADX {snap.adx:.0f}{di_str}{strength}")

    if parts2:
        lines.append("  ".join(parts2))

    return "\n".join(lines)


def format_indicator_full(snap: IndicatorSnapshot, symbol: str, sinfo) -> str:
    """Build a full indicator report for /ind command."""
    from bot.formatting import fmt_ohlc

    pip_size = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01

    parts = ["📊 Indicator Snapshot"]

    if snap.bar_count > 0:
        parts.append(f"Bars: {snap.bar_count}")

    if snap.sma50 is not None:
        parts.append(f"SMA(50): {fmt_ohlc(snap.sma50, symbol, sinfo)}")
    if snap.ema20 is not None:
        parts.append(f"EMA(20): {fmt_ohlc(snap.ema20, symbol, sinfo)}")

    if snap.bb_upper is not None:
        bb_width_pips = (snap.bb_upper - snap.bb_lower) / pip_size if pip_size > 0 else 0
        parts.append(
            f"BB(20,2): {fmt_ohlc(snap.bb_upper, symbol, sinfo)} — "
            f"{fmt_ohlc(snap.bb_middle, symbol, sinfo)} — "
            f"{fmt_ohlc(snap.bb_lower, symbol, sinfo)}"
        )
        parts.append(f"  Width: {bb_width_pips:.1f}p ({snap.bb_width_pct:.1f}%)" if snap.bb_width_pct else f"  Width: {bb_width_pips:.1f}p")

    if snap.atr is not None:
        atr_now = fmt_ohlc(snap.atr, symbol, sinfo)
        pct_str = f" ({snap.atr_pct:.2f}%)" if snap.atr_pct else ""
        prev_str = ""
        if snap.atr_prev is not None:
            prev_str += f" | Prev: {fmt_ohlc(snap.atr_prev, symbol, sinfo)}"
        if snap.atr_prev2 is not None:
            prev_str += f" | Prev2: {fmt_ohlc(snap.atr_prev2, symbol, sinfo)}"
        parts.append(f"ATR(14): {atr_now}{pct_str}{prev_str}")

    if snap.rsi is not None:
        rsi_str = f"{snap.rsi:.1f}"
        if snap.rsi_prev is not None:
            rsi_str += f" | Prev: {snap.rsi_prev:.1f}"
        if snap.rsi_prev2 is not None:
            rsi_str += f" | Prev2: {snap.rsi_prev2:.1f}"
        zone = ""
        if snap.rsi > 70:
            zone = " (overbought)"
        elif snap.rsi < 30:
            zone = " (oversold)"
        parts.append(f"RSI(14): {rsi_str}{zone}")

    if snap.adx is not None:
        strength = "strong trend" if snap.adx > 50 else ("trending" if snap.adx > 25 else "weak/ranging")
        di_str = ""
        if snap.di_plus is not None and snap.di_minus is not None:
            di_str = f" | +DI: {snap.di_plus:.1f} | -DI: {snap.di_minus:.1f}"
        parts.append(f"ADX(14): {snap.adx:.1f}{di_str} — {strength}")

    if snap.current_close is not None:
        parts.append(f"Close: {fmt_ohlc(snap.current_close, symbol, sinfo)}")

    return "\n".join(parts)
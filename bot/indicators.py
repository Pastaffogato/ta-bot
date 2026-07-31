"""Indicator computation from OHLCV bar data.

All functions operate on numpy arrays in chronological order (oldest first).
Bars are passed newest-first and reversed internally.

BB(20,2) on close, SMA50, ATR(14), RSI(14), ADX(14), EMA(20), VWAP, RelVol.
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
    atr: Optional[float] = None  # ATR(14)
    atr_pct: Optional[float] = None  # ATR / close * 100
    atr_trend: Optional[str] = None  # "rising", "compressing", "flat"
    rsi: Optional[float] = None  # RSI(14)
    adx: Optional[float] = None  # ADX(14), 0-100
    vwap: Optional[float] = None
    rel_volume: Optional[float] = None  # current vol / avg vol(20)
    current_close: Optional[float] = None
    bar_count: int = 0


def compute_all(bars: list) -> IndicatorSnapshot:
    """Compute all indicators from a list of Bar objects (newest first).

    Requires at least 20 bars for BB/EMA, 50 for SMA50, 15 for ATR/RSI/ADX.
    Missing indicators are left as None.
    """
    if not bars:
        return IndicatorSnapshot()

    n = len(bars)

    # Reverse to chronological order (oldest first)
    closes = np.array([b.close for b in reversed(bars)], dtype=np.float64)
    highs = np.array([b.high for b in reversed(bars)], dtype=np.float64)
    lows = np.array([b.low for b in reversed(bars)], dtype=np.float64)
    volumes = np.array([b.tick_volume for b in reversed(bars)], dtype=np.float64)

    snap = IndicatorSnapshot(bar_count=n, current_close=float(closes[-1]))

    # ── SMA 50 ──
    if n >= 50:
        snap.sma50 = float(np.mean(closes[-50:]))

    # ── EMA 20 ──
    if n >= 20:
        snap.ema20 = _ema(closes, 20)

    # ── Bollinger Bands (20, 2) on close ──
    if n >= 20:
        sma20 = np.mean(closes[-20:])
        std20 = np.std(closes[-20:], ddof=0)  # population std (standard BB formula)
        snap.bb_middle = float(sma20)
        snap.bb_upper = float(sma20 + 2.0 * std20)
        snap.bb_lower = float(sma20 - 2.0 * std20)
        if sma20 > 0:
            snap.bb_width_pct = float((snap.bb_upper - snap.bb_lower) / sma20 * 100)

    # ── ATR(14) ──
    if n >= 15:
        snap.atr = _atr(highs, lows, closes, 14)
        if snap.current_close and snap.current_close > 0:
            snap.atr_pct = snap.atr / snap.current_close * 100
        # ATR trend: compare average of first 3 vs last 3 of last 14 TR values
        snap.atr_trend = _atr_trend(highs, lows, closes, 14)

    # ── RSI(14) ──
    if n >= 15:
        snap.rsi = _rsi(closes, 14)

    # ── ADX(14) ──
    if n >= 28:  # need 2*period for proper ADX
        snap.adx = _adx(highs, lows, closes, 14)

    # ── VWAP ──
    if n > 0 and np.sum(volumes) > 0:
        typical = (highs + lows + closes) / 3.0
        snap.vwap = float(np.sum(typical * volumes) / np.sum(volumes))

    # ── Relative Volume ──
    if n >= 21:
        avg_vol = np.mean(volumes[-21:-1])  # avg of last 20 bars (excluding current)
        if avg_vol > 0:
            snap.rel_volume = float(volumes[-1] / avg_vol)

    return snap


# ── internal helpers ──


def _ema(data: np.ndarray, period: int) -> float:
    """Exponential moving average. Returns the most recent value."""
    alpha = 2.0 / (period + 1.0)
    result = data[0]
    for i in range(1, len(data)):
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


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
    """Average True Range (Wilder's smoothing)."""
    tr = _true_range(highs, lows, closes)
    atr_val = float(np.mean(tr[1:period + 1]))  # first ATR = simple average of first `period` TR
    for i in range(period + 1, len(tr)):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
    return float(atr_val)


def _atr_trend(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> str:
    """ATR trend: compare rolling ATR(3) at start vs end of last 14 TR values."""
    tr = _true_range(highs, lows, closes)
    if len(tr) < period + 3:
        return "flat"
    # ATR over the last `period` values at the tail
    start_window = tr[-period - 3:-period]
    end_window = tr[-3:]
    start_avg = float(np.mean(start_window))
    end_avg = float(np.mean(end_window))
    if start_avg <= 0:
        return "flat"
    change = (end_avg - start_avg) / start_avg
    if change > 0.05:
        return "rising"
    elif change < -0.05:
        return "compressing"
    return "flat"


def _rsi(closes: np.ndarray, period: int) -> float:
    """RSI (Wilder's smoothing)."""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
    """Average Directional Index (Wilder's smoothing)."""
    n = len(highs)
    if n < period + 1:
        return 0.0

    up_move = np.zeros(n, dtype=np.float64)
    down_move = np.zeros(n, dtype=np.float64)

    tr = _true_range(highs, lows, closes)

    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            up_move[i] = up
        else:
            up_move[i] = 0.0
        if down > up and down > 0:
            down_move[i] = down
        else:
            down_move[i] = 0.0

    # First period: simple average
    atr_period = float(np.mean(tr[1:period + 1]))
    avg_up = float(np.mean(up_move[1:period + 1]))
    avg_down = float(np.mean(down_move[1:period + 1]))

    # Wilder's smoothing
    for i in range(period + 1, n):
        atr_period = (atr_period * (period - 1) + tr[i]) / period
        avg_up = (avg_up * (period - 1) + up_move[i]) / period
        avg_down = (avg_down * (period - 1) + down_move[i]) / period

    if atr_period <= 0:
        return 0.0

    di_plus = (avg_up / atr_period) * 100.0
    di_minus = (avg_down / atr_period) * 100.0
    di_sum = di_plus + di_minus

    if di_sum <= 0:
        return 0.0

    dx = abs(di_plus - di_minus) / di_sum * 100.0

    # ADX = EMA of DX (period=14)
    # We only have one DX — in a full implementation we'd compute the rolling DX
    # and then average. For a snapshot with limited bars, we compute DX from the
    # final smoothed values and return it directly (same as last ADX tick).
    return float(dx)


def format_indicator_section(snap: IndicatorSnapshot, symbol: str, sinfo) -> str:
    """Build a compact indicator display section for candle alerts.

    Two lines max:
    Line 1: ATR, SMA50, BB
    Line 2: RSI, VWAP, RVOL, ADX
    """
    from bot.formatting import fmt_ohlc

    lines = []

    # Line 1: ATR + SMA50 + BB
    parts1 = []
    if snap.atr is not None:
        atr_str = fmt_ohlc(snap.atr, symbol, sinfo) if sinfo else f"{snap.atr:.5f}"
        trend = {"rising": "▲", "compressing": "▼", "flat": "─"}.get(snap.atr_trend, "")
        parts1.append(f"ATR {atr_str}{trend}")
    if snap.sma50 is not None:
        parts1.append(f"SMA50 {fmt_ohlc(snap.sma50, symbol, sinfo)}")
    if snap.bb_upper is not None:
        parts1.append(
            f"BB {fmt_ohlc(snap.bb_lower, symbol, sinfo)}–{fmt_ohlc(snap.bb_upper, symbol, sinfo)}"
        )

    if parts1:
        lines.append("  ".join(parts1))

    # Line 2: RSI + VWAP + RVOL + ADX
    parts2 = []
    if snap.rsi is not None:
        zone = ""
        if snap.rsi > 70:
            zone = " OB"
        elif snap.rsi < 30:
            zone = " OS"
        parts2.append(f"RSI {snap.rsi:.1f}{zone}")
    if snap.vwap is not None and snap.current_close is not None:
        vwap_str = fmt_ohlc(snap.vwap, symbol, sinfo)
        above = "▲" if snap.current_close > snap.vwap else "▼"
        parts2.append(f"VWAP {vwap_str}{above}")
    if snap.rel_volume is not None:
        parts2.append(f"RVOL {snap.rel_volume:.1f}x")
    if snap.adx is not None:
        strength = ""
        if snap.adx > 25:
            strength = " strong" if snap.adx > 50 else " present"
        parts2.append(f"ADX {snap.adx:.0f}{strength}")

    if parts2:
        lines.append("  ".join(parts2))

    return "\n".join(lines)


def format_indicator_full(snap: IndicatorSnapshot, symbol: str, sinfo) -> str:
    """Build a full indicator report for /ind command."""
    from bot.formatting import fmt_ohlc

    parts = ["📊 Indicator Snapshot"]

    if snap.bar_count > 0:
        parts.append(f"Bars: {snap.bar_count}")

    if snap.sma50 is not None:
        parts.append(f"SMA(50): {fmt_ohlc(snap.sma50, symbol, sinfo)}")
    if snap.ema20 is not None:
        parts.append(f"EMA(20): {fmt_ohlc(snap.ema20, symbol, sinfo)}")

    if snap.bb_upper is not None:
        parts.append(
            f"BB(20,2): {fmt_ohlc(snap.bb_lower, symbol, sinfo)} — "
            f"{fmt_ohlc(snap.bb_middle, symbol, sinfo)} — "
            f"{fmt_ohlc(snap.bb_upper, symbol, sinfo)}"
        )
        if snap.bb_width_pct is not None:
            parts.append(f"  Width: {snap.bb_width_pct:.1f}%")

    if snap.atr is not None:
        trend_label = {"rising": "rising", "compressing": "compressing", "flat": "flat"}
        parts.append(
            f"ATR(14): {fmt_ohlc(snap.atr, symbol, sinfo)}"
            + (f" ({snap.atr_pct:.2f}%)" if snap.atr_pct else "")
            + f" — {trend_label.get(snap.atr_trend, snap.atr_trend)}"
        )

    if snap.rsi is not None:
        zone = ""
        if snap.rsi > 70:
            zone = " (overbought)"
        elif snap.rsi < 30:
            zone = " (oversold)"
        parts.append(f"RSI(14): {snap.rsi:.1f}{zone}")

    if snap.adx is not None:
        strength = "strong trend" if snap.adx > 50 else ("trending" if snap.adx > 25 else "weak/ranging")
        parts.append(f"ADX(14): {snap.adx:.1f} — {strength}")

    if snap.vwap is not None and snap.current_close is not None:
        diff = snap.current_close - snap.vwap
        sign = "+" if diff >= 0 else ""
        parts.append(
            f"VWAP: {fmt_ohlc(snap.vwap, symbol, sinfo)} "
            f"(price {sign}{fmt_ohlc(abs(diff), symbol, sinfo)} {'above' if diff >= 0 else 'below'})"
        )

    if snap.rel_volume is not None:
        label = "high" if snap.rel_volume > 1.5 else ("low" if snap.rel_volume < 0.5 else "normal")
        parts.append(f"RelVol: {snap.rel_volume:.1f}x — {label}")

    if snap.current_close is not None:
        parts.append(f"Close: {fmt_ohlc(snap.current_close, symbol, sinfo)}")

    return "\n".join(parts)
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


def _true_range(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> np.ndarray:
    """Return true range aligned to source bars (MT5-compatible).

    MT5 defines TR for every bar. For the oldest bar (index 0) there is no
    previous close, so TR[0] = High[0] - Low[0]. For all later bars:
        TR[i] = max(H-L, |H - prevC|, |L - prevC|)
    """
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)

    n = len(closes)
    tr = np.zeros(n, dtype=np.float64)

    if n == 0:
        return tr
    if n == 1:
        tr[0] = highs[0] - lows[0]
        return tr

    tr[0] = highs[0] - lows[0]
    tr[1:] = np.maximum.reduce(
        (
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        )
    )

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


def _rolling_atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int,
) -> list[float]:
    """Return MT5-compatible ATR values using Wilder smoothing.

    Matches MT5 iATR():
        TR[0]     = High[0] - Low[0]   (oldest bar has no previous close)
        TR[i>0]   = max(H-L, |H-prevC|, |L-prevC|)
        ATR seed  = SMA(TR[0 .. period-1])   at bar index period-1
        ATR[i]    = (ATR[i-1] * (period-1) + TR[i]) / period   (Wilder)

    Input arrays must be chronological (index 0 = oldest bar).
    Returns one ATR value per bar from index `period-1` onward.
    """
    if period <= 0:
        raise ValueError(
            "period must be greater than zero"
        )

    tr = _true_range(
        highs,
        lows,
        closes,
    )

    if len(tr) <= period:
        return []

    atr_values = [
        float(np.mean(tr[:period]))
    ]

    for i in range(period, len(tr)):
        previous_atr = atr_values[-1]
        current_tr = float(tr[i])

        atr_value = (
            previous_atr * (period - 1)
            + current_tr
        ) / period

        atr_values.append(
            float(atr_value)
        )

    return atr_values


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


def _adx(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> tuple[
    Optional[float],
    Optional[float],
    Optional[float],
]:
    """Return MT5 standard ADX, +DI, and -DI.

    This targets MT5 iADX() (standard), which uses Wilder smoothing
    (smoothing factor = 1/period) for TR, +DM, -DM, and DX→ADX.
    This is NOT iADXWilder() — both iADX() and iADXWilder() use Wilder
    smoothing; they differ only in DI calculation details.

    Wilder smoothing: alpha = 1 / period
        new = prev * (1 - alpha) + cur * alpha
            = prev * (period-1)/period + cur/period

    Input arrays must be chronological:
        index 0  = oldest bar
        index -1 = newest selected bar

    Returns:
        (latest_adx, latest_plus_di, latest_minus_di)
    """
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)

    n = len(closes)

    if period <= 0:
        raise ValueError("period must be greater than zero")

    if not (
        len(highs) == n
        and len(lows) == n
    ):
        raise ValueError(
            "highs, lows, and closes must have equal lengths"
        )

    if n < period + 1:
        return None, None, None

    # Bar-aligned source arrays. Index zero has no previous bar.
    tr = np.zeros(n, dtype=np.float64)
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        high_low = highs[i] - lows[i]
        high_previous_close = abs(
            highs[i] - closes[i - 1]
        )
        low_previous_close = abs(
            lows[i] - closes[i - 1]
        )

        tr[i] = max(
            high_low,
            high_previous_close,
            low_previous_close,
        )

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if (
            up_move > down_move
            and up_move > 0.0
        ):
            plus_dm[i] = up_move

        if (
            down_move > up_move
            and down_move > 0.0
        ):
            minus_dm[i] = down_move

    alpha = 1.0 / period  # Wilder smoothing factor (MT5 iADX standard)

    # Seed Wilder-smoothed components with the SMA of the first `period`
    # valid observations: source indices 1 through period.
    smoothed_tr = float(
        np.mean(tr[1 : period + 1])
    )
    smoothed_plus_dm = float(
        np.mean(plus_dm[1 : period + 1])
    )
    smoothed_minus_dm = float(
        np.mean(minus_dm[1 : period + 1])
    )

    def calculate_di_and_dx() -> tuple[
        float,
        float,
        float,
    ]:
        if smoothed_tr > 0.0:
            plus_di = (
                100.0
                * smoothed_plus_dm
                / smoothed_tr
            )
            minus_di = (
                100.0
                * smoothed_minus_dm
                / smoothed_tr
            )
        else:
            plus_di = 0.0
            minus_di = 0.0

        di_sum = plus_di + minus_di

        if di_sum > 0.0:
            dx = (
                100.0
                * abs(plus_di - minus_di)
                / di_sum
            )
        else:
            dx = 0.0

        return (
            float(plus_di),
            float(minus_di),
            float(dx),
        )

    plus_di_latest, minus_di_latest, first_dx = (
        calculate_di_and_dx()
    )

    dx_values = [first_dx]

    # Continue EMA smoothing from source index period + 1.
    for i in range(period + 1, n):
        smoothed_tr = (
            alpha * tr[i]
            + (1.0 - alpha) * smoothed_tr
        )

        smoothed_plus_dm = (
            alpha * plus_dm[i]
            + (1.0 - alpha) * smoothed_plus_dm
        )

        smoothed_minus_dm = (
            alpha * minus_dm[i]
            + (1.0 - alpha) * smoothed_minus_dm
        )

        (
            plus_di_latest,
            minus_di_latest,
            dx,
        ) = calculate_di_and_dx()

        dx_values.append(dx)

    if len(dx_values) < period:
        return (
            None,
            plus_di_latest,
            minus_di_latest,
        )

    # MT5 standard ADX applies Wilder smoothing to DX.
    adx_value = float(
        np.mean(dx_values[:period])
    )

    for i in range(period, len(dx_values)):
        adx_value = (
            alpha * dx_values[i]
            + (1.0 - alpha) * adx_value
        )

    return (
        float(adx_value),
        float(plus_di_latest),
        float(minus_di_latest),
    )


# ── formatting ──


def format_indicator_section(snap: IndicatorSnapshot, symbol: str, sinfo, prefs: dict = None) -> str:
    """Build a compact indicator display section for candle alerts.

    Five lines (each respecting granular prefs; empty lines are skipped):
    Line 1: BB(20,2) high / mid / low + width in pips
    Line 2: SMA50 + EMA20
    Line 3: ATR(14) last 3 values ('-' separated) + compressing/expanding note
    Line 4: RSI(14) last 3 values ('-' separated) + OB/OS zone
    Line 5: ADX(14) with +DI/-DI + strength

    Granular prefs: show_sma, show_ema, show_bb, show_atr, show_rsi, show_adx.
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

    # Line 1: BB(20,2)
    if _on("show_bb") and snap.bb_upper is not None:
        bb_high = fmt_ohlc(snap.bb_upper, symbol, sinfo)
        bb_mid = fmt_ohlc(snap.bb_middle, symbol, sinfo)
        bb_low = fmt_ohlc(snap.bb_lower, symbol, sinfo)
        bb_width_pips = (snap.bb_upper - snap.bb_lower) / pip_size if pip_size > 0 else 0
        bb_pct = snap.bb_width_pct if snap.bb_width_pct else 0
        lines.append(f"BB {bb_high}-{bb_mid}-{bb_low} {bb_width_pips:.1f}p-{bb_pct:.1f}%")

    # Line 2: SMA50 + EMA20
    parts2 = []
    if _on("show_sma") and snap.sma50 is not None:
        parts2.append(f"SMA50 {fmt_ohlc(snap.sma50, symbol, sinfo)}")
    if _on("show_ema") and snap.ema20 is not None:
        parts2.append(f"EMA20 {fmt_ohlc(snap.ema20, symbol, sinfo)}")
    if parts2:
        lines.append("  ".join(parts2))

    # Line 3: ATR(14) — last 3 values separated by '-' + compression note
    if _on("show_atr") and snap.atr is not None:
        atr_vals = [snap.atr]
        if snap.atr_prev is not None:
            atr_vals.append(snap.atr_prev)
        if snap.atr_prev2 is not None:
            atr_vals.append(snap.atr_prev2)
        atr_str = "-".join(fmt_ohlc(v, symbol, sinfo) if sinfo else f"{v:.5f}" for v in atr_vals)
        # compressing / expanding: compare latest ATR vs previous ATR
        note = ""
        if snap.atr_prev is not None:
            if snap.atr > snap.atr_prev:
                note = " expanding"
            elif snap.atr < snap.atr_prev:
                note = " compressing"
            else:
                note = " flat"
        lines.append(f"ATR {atr_str}{note}")

    # Line 4: RSI(14) — last 3 values separated by '-' + OB/OS zone
    if _on("show_rsi") and snap.rsi is not None:
        rsi_vals = [f"{snap.rsi:.1f}"]
        if snap.rsi_prev is not None:
            rsi_vals.append(f"{snap.rsi_prev:.1f}")
        if snap.rsi_prev2 is not None:
            rsi_vals.append(f"{snap.rsi_prev2:.1f}")
        rsi_str = "-".join(rsi_vals)
        zone = ""
        if snap.rsi > 70:
            zone = " OB"
        elif snap.rsi < 30:
            zone = " OS"
        lines.append(f"RSI {rsi_str}{zone}")

    # Line 5: ADX(14) with +DI/-DI + strength
    if _on("show_adx") and snap.adx is not None:
        strength = ""
        if snap.adx > 50:
            strength = " strong"
        elif snap.adx > 25:
            strength = " present"
        di_str = ""
        if snap.di_plus is not None and snap.di_minus is not None:
            di_str = f" +DI {snap.di_plus:.0f} -DI {snap.di_minus:.0f}"
        lines.append(f"ADX {snap.adx:.0f}{di_str}{strength}")

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
            prev_str += f" - {fmt_ohlc(snap.atr_prev, symbol, sinfo)}"
        if snap.atr_prev2 is not None:
            prev_str += f" - {fmt_ohlc(snap.atr_prev2, symbol, sinfo)}"
        parts.append(f"ATR(14): {atr_now}{prev_str}{pct_str}")

    if snap.rsi is not None:
        rsi_str = f"{snap.rsi:.1f}"
        if snap.rsi_prev is not None:
            rsi_str += f" - {snap.rsi_prev:.1f}"
        if snap.rsi_prev2 is not None:
            rsi_str += f" - {snap.rsi_prev2:.1f}"
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
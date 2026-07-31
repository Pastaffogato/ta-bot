"""Message formatting for candle alerts, price alerts, and paper trade display.

All functions that build Telegram message text live here.
Depends on: config, db, patterns, mt5_data, parsing, timeframes.
"""

from datetime import datetime, timezone
from typing import Optional

from bot import config, db, patterns
from bot.models import PriceAlert
from bot.mt5_data import Bar, SymbolInfo, Tick
from bot.parsing import fmt_expiry
from bot.timeframes import tf_label


# ── display helpers ──

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def display_symbol(broker_symbol: str) -> str:
    """Convert broker symbol to ideal name for display (e.g. XAUUSD.pc → xauusd)."""
    return config.PAIRS_REVERSE.get(broker_symbol.upper(), broker_symbol.lower())


def err(msg: str) -> str:
    return f"❌ {msg}"


# ── price formatting ──

def fmt_price(price: float, symbol: str) -> str:
    """Format price with appropriate decimal places for the symbol."""
    if price >= 1000:
        return f"{price:.2f}"
    elif price >= 100:
        return f"{price:.3f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.5f}"


def fmt_spread(spread: float, sinfo: Optional[SymbolInfo]) -> str:
    if sinfo is None:
        return f"{spread:.5f}"
    points = spread / sinfo.point
    return f"{points:.1f}"


def fmt_ohlc(value: float, symbol: str, sinfo: Optional[SymbolInfo]) -> str:
    """Format OHLC price using symbol digits if available."""
    if sinfo is not None:
        return f"{value:.{sinfo.digits}f}"
    return fmt_price(value, symbol)


# ── candle alert message ──

def format_candle_message(
    symbol: str,
    tf_min: int,
    bar: Optional[Bar],
    prev_bar: Optional[Bar],
    tick: Optional[Tick],
    sinfo: Optional[SymbolInfo],
    close_epoch: float,
    sent_epoch: float,
    chat_id: int,
    ind_snap=None,
) -> str:
    """Build the candle alert message."""
    disp = display_symbol(symbol) if symbol else "timer"
    prefs = db.get_user_prefs(chat_id)

    pat = patterns.classify(bar, prev_bar) if bar else None

    # Header: EMOJI SYMBOL TF BID +PIPS
    bid_str = fmt_ohlc(tick.bid, symbol, sinfo) if tick else "—"
    emoji = pat.emoji if pat else "⏳"
    pip_change = ""
    if bar and tick and sinfo and prev_bar:
        point_size = sinfo.point
        if point_size > 0:
            pip_size = point_size * 10
            change_pips = (tick.bid - prev_bar.close) / pip_size
            sign = "+" if change_pips >= 0 else ""
            pip_change = f" {sign}{change_pips:.1f}p"
    lines = [f"{emoji} {disp.upper()} {tf_label(tf_min)} {bid_str}{pip_change}"]

    if bar:
        if prefs.get("show_pattern", "on") != "off" and pat:
            lines.append(f"{pat.label}")
        if prefs.get("show_ohlc", "on") != "off":
            lines.append(
                f"O {fmt_ohlc(bar.open, symbol, sinfo)}  "
                f"H {fmt_ohlc(bar.high, symbol, sinfo)}  "
                f"L {fmt_ohlc(bar.low, symbol, sinfo)}  "
                f"C {fmt_ohlc(bar.close, symbol, sinfo)}"
            )
        if prefs.get("show_range_body", "on") != "off":
            range_p = bar.high - bar.low
            body_p = abs(bar.close - bar.open)
            tw = bar.high - max(bar.open, bar.close)
            bw = min(bar.open, bar.close) - bar.low
            lines.append(
                f"Range {fmt_ohlc(range_p, symbol, sinfo)}  "
                f"Body {fmt_ohlc(body_p, symbol, sinfo)}  "
                f"TW {fmt_ohlc(tw, symbol, sinfo)}  "
                f"BW {fmt_ohlc(bw, symbol, sinfo)}"
            )

    if tick and prefs.get("show_bid_ask", "off") != "off":
        lines.append(
            f"Bid {fmt_ohlc(tick.bid, symbol, sinfo)}  "
            f"Ask {fmt_ohlc(tick.ask, symbol, sinfo)}  "
            f"Spread {fmt_spread(tick.spread, sinfo)}"
        )

    # Marks — show distance from current price
    if tick and sinfo and symbol and prefs.get("show_marks", "on") != "off":
        marks = db.get_marks(chat_id, symbol)
        if marks:
            pip_size = sinfo.point * 10 if sinfo.point > 0 else 0.01
            for m in marks:
                dist_pips = (tick.bid - m.price) / pip_size
                sign = "+" if dist_pips >= 0 else ""
                lines.append(f"📍 M{m.user_seq} {fmt_ohlc(m.price, symbol, sinfo)}  {sign}{dist_pips:.1f}p")

    # Indicators — show when pref is on and data available
    if ind_snap is not None and prefs.get("show_indicators", "off") != "off":
        from bot.indicators import format_indicator_section
        indicator_text = format_indicator_section(ind_snap, symbol, sinfo, prefs)
        if indicator_text:
            lines.append(indicator_text)

    return "\n".join(lines)


# ── price alert message ──

def format_price_alert_message(
    alert: PriceAlert,
    price: float,
    tick: Tick,
    chat_id: int,
) -> str:
    disp = display_symbol(alert.symbol)
    if alert.alert_type == "close":
        if alert.target_upper is not None:
            msg = f"🔔 <b>{disp.upper()}</b> closed within {alert.target:.2f}–{alert.target_upper:.2f}!\n"
        else:
            msg = f"🔔 <b>{disp.upper()}</b> closed beyond {alert.target:.2f}!\n"
    else:
        dir_str = f"crossed {alert.direction}" if alert.direction else "crossed"
        msg = f"🔔 <b>{disp.upper()}</b> {dir_str} {alert.target}!\n"
    return msg + (
        f"Bid: {fmt_ohlc(tick.bid, alert.symbol, None)}  "
        f"Ask: {fmt_ohlc(tick.ask, alert.symbol, None)}"
    )
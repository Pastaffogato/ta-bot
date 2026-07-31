"""Telegram handlers, message formatting, and bot wiring.

All commands, callback functions for the scheduler, and message builders.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import config, db, mt5_data, patterns, scheduler
from bot.models import CandleAlert, PriceAlert
from bot.mt5_data import Bar, Tick, SymbolInfo
from bot.timeframes import parse_tf, tf_label

logger = logging.getLogger(__name__)


# ============================================================
# Per-user focus pair (session only, not persisted)
# ============================================================

_focus_pairs: dict[int, str] = {}


def _get_focus(chat_id: int) -> Optional[str]:
    return _focus_pairs.get(chat_id)


# ============================================================
# Helpers
# ============================================================

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _display_symbol(broker_symbol: str) -> str:
    """Convert broker symbol to ideal name for display (e.g. XAUUSD.pc → xauusd)."""
    return config.PAIRS_REVERSE.get(broker_symbol.upper(), broker_symbol.lower())


def _err(msg: str) -> str:
    return f"❌ {msg}"


import re

_EXPIRY_RE = re.compile(r'^(\d+)([smh])$', re.IGNORECASE)
_REL_RE = re.compile(r'^([+-])(\d+(?:\.\d+)?)$')


def _parse_expiry(arg: str) -> tuple[int, str] | None:
    """Parse expiry suffix. Returns (seconds, display_label) or None."""
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


def _fmt_expiry(expires_at: str | None) -> str:
    """Format expiry as relative time (UTC+8). Returns empty string if no expiry."""
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


def _parse_mark_args(args: list[str]) -> tuple[list[float], int | None, str | None]:
    """Separate prices and optional expiry suffix from mark args.
    Returns (prices, expiry_seconds, expiry_label)."""
    prices = []
    expiry_s = None
    expiry_label = None
    for a in args:
        exp = _parse_expiry(a)
        if exp is not None:
            expiry_s, expiry_label = exp
        else:
            try:
                prices.append(float(a))
            except ValueError:
                pass  # skip non-price, non-expiry args
    return prices, expiry_s, expiry_label


def _resolve_price_args(
    args: list[str],
    current_price: float,
    pip_size: float,
) -> tuple[str, list[float], int | None, str | None]:
    """Parse price alert args. Returns (alert_type, boundaries, expiry_s, expiry_label).
    - alert_type: "crossing" or "close"
    - boundaries: absolute prices (sorted for close, 1-2 items for close, 1+ for crossing)
    - expiry_s, expiry_label: parsed from suffix
    """
    alert_type = "crossing"
    expiry_s = None
    expiry_label = None
    raw_prices: list[float] = []  # parsed absolute prices (before sorting)
    relative_pips: list[tuple[str, float]] = []  # (sign, pip_amount)

    for a in args:
        # Check for "close" keyword
        if a.lower() == "close":
            alert_type = "close"
            continue

        # Check for expiry suffix
        exp = _parse_expiry(a)
        if exp is not None:
            expiry_s, expiry_label = exp
            continue

        # Check for relative price (+N or -N)
        rel = _REL_RE.match(a)
        if rel:
            sign = rel.group(1)
            pips = float(rel.group(2))
            relative_pips.append((sign, pips))
            continue

        # Try absolute price
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


# ============================================================
# Command handlers
# ============================================================

async def cmd_focus_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the session focus pair. /fp [SYMBOL|off]"""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        current = _get_focus(chat_id)
        if current:
            display = _display_symbol(current)
            await update.message.reply_text(f"🎯 Focus: {display.upper()}\n/fp off to clear")
        else:
            await update.message.reply_text(
                "No focus pair set.\nUsage: /fp XAUUSD\n/fp off to clear"
            )
        return

    arg = args[0].lower()
    if arg in ("off", "clear", "none"):
        _focus_pairs.pop(chat_id, None)
        await update.message.reply_text("✅ Focus cleared")
        return

    resolved = await mt5_data.resolve_symbol(arg)
    if resolved is None:
        suggestions = await mt5_data.suggest_symbols(arg)
        if suggestions:
            s = ", ".join(suggestions[:10])
            await update.message.reply_text(
                f"❌ Symbol '{arg}' not found.\nDid you mean: {s}?"
            )
        else:
            await update.message.reply_text(_err(f"Symbol '{arg}' not found"))
        return

    _focus_pairs[chat_id] = resolved
    display = _display_symbol(resolved)
    await update.message.reply_text(
        f"🎯 Focus set to {display.upper()}\n"
        f"/add 5  →  {display.upper()} M5 alert\n"
        f"/p 2600  →  {display.upper()} price 2600\n"
        f"/fp off to clear"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>ta-bot</b> — trading alert bot\n\n"
        "<b>Session:</b>\n"
        "/fp XAUUSD — set focus pair\n"
        "/fp — show focus\n"
        "/fp off — clear focus\n\n"
        "<b>Candle alerts:</b>\n"
        "/add 5 — timer-only M5 alert\n"
        "/add XAUUSD 5 — M5 alert with OHLC\n"
        "/del — remove all candle alerts\n"
        "/del XAUUSD 5 — remove specific\n"
        "/list — show active alerts\n"
        "/offset 8 — pre-close seconds\n\n"
        "<b>OHLC data:</b>\n"
        "/now XAUUSD 3 — live M3 OHLC\n"
        "/level XAUUSD — yesterday OHLC\n\n"
        "<b>Price alerts:</b>\n"
        "/price XAUUSD 2400 — cross alert\n"
        "/price XAUUSD above 2400 — directional\n"
        "/cancel p7 — remove price alert\n"
        "/cancel — cancel all price alerts\n\n"
        "<b>Marks:</b>\n"
        "/mark XAUUSD 2400 — add mark\n"
        "/mark del — delete all marks\n"
        "/mark del 1 — delete mark M1\n"
        "/mark list — list marks\n"
        "/mkd — shorthand for mark del\n"
        "/mkl — shorthand for mark list\n\n"
        "<b>Paper trade:</b>\n"
        "/entry — list open trades\n"
        "/entry XAUUSD buy +50 -30 — market buy, TP+50, SL-30\n"
        "/entry XAUUSD sell limit 2410 +30 — sell limit\n"
        "/entry XAUUSD buy stop 2420 +20 — buy stop\n"
        "/modify t1 sl 2390 — move stop loss\n"
        "/modify t1 tp 2420 — move take profit\n"
        "/modify t1 close — close at market\n\n"
        "<b>Other:</b>\n"
        "/data — toggle OHLC data sections\n"
        "/clear — clear all alerts + marks\n"
        "/status — bot health\n"
        "/help — this text\n\n"
        "<b>Timeframes:</b> 3, 5, 15, m3, M5, h1, H4\n"
        "<b>Shorthand:</b> /a, /d, /l, /o, /n, /lv, /p, /c, /s, /e, /m, /mk, /dt\n"
        "<b>Focus pair:</b> set /fp, then /a 5 = /a PAIR 5",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args or []

    if not args:
        await update.message.reply_text(_err("Usage: /add [SYMBOL] TIMEFRAME [TIMEFRAME ...]\nExample: /add 5  or  /add XAUUSD 5"))
        return

    focus = _get_focus(chat_id)

    # Multi-arg with focus pair: /add 5 15 30
    if focus and len(args) >= 1:
        tfs = []
        for a in args:
            tf = parse_tf(a)
            if tf is None:
                tfs = None
                break
            tfs.append(tf)
        if tfs:
            display = _display_symbol(focus)
            lines = []
            for tf in tfs:
                try:
                    db.add_candle_alert(chat_id, symbol=focus, timeframe_min=tf)
                    lines.append(f"✅ {display.upper()} {tf_label(tf)}")
                except ValueError as e:
                    lines.append(_err(str(e)))
            scheduler.subscriptions_changed.set()
            lines.append(f"Pre-close offset: {db.get_user(chat_id).default_offset_s}s")
            await update.message.reply_text("\n".join(lines))
            return

    if len(args) == 1:
        # /add TIMEFRAME — timer-only (no focus pair)
        tf = parse_tf(args[0])
        if tf is None:
            await update.message.reply_text(_err(f"Unknown timeframe: {args[0]}"))
            return
        try:
            db.add_candle_alert(chat_id, symbol=None, timeframe_min=tf)
            scheduler.subscriptions_changed.set()
            await update.message.reply_text(
                f"✅ Timer-only {tf_label(tf)} alert\n"
                f"Pre-close offset: {db.get_user(chat_id).default_offset_s}s\n"
                f"/del to remove"
            )
        except ValueError as e:
            await update.message.reply_text(_err(str(e)))
        return

    # /add SYMBOL TIMEFRAME [TIMEFRAME ...]
    symbol = args[0].upper()
    resolved = await mt5_data.resolve_symbol(symbol)
    if resolved is None:
        suggestions = await mt5_data.suggest_symbols(symbol)
        if suggestions:
            s = ", ".join(suggestions[:10])
            await update.message.reply_text(
                f"❌ Symbol '{symbol}' not found.\nDid you mean: {s}?"
            )
        else:
            await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
        return

    tfs = []
    for a in args[1:]:
        tf = parse_tf(a)
        if tf is None:
            await update.message.reply_text(_err(f"Unknown timeframe: {a}"))
            return
        tfs.append(tf)

    display = _display_symbol(resolved)
    lines = []
    for tf in tfs:
        try:
            db.add_candle_alert(chat_id, symbol=resolved, timeframe_min=tf)
            lines.append(f"✅ {display.upper()} {tf_label(tf)}")
        except ValueError as e:
            lines.append(_err(str(e)))
    scheduler.subscriptions_changed.set()
    lines.append(f"Pre-close offset: {db.get_user(chat_id).default_offset_s}s")
    await update.message.reply_text("\n".join(lines))


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        # /del — remove all candle alerts
        n = db.delete_candle_alerts_by(chat_id)
        if n > 0:
            scheduler.subscriptions_changed.set()
            await update.message.reply_text(f"🗑️ Removed {n} candle alert(s)")
        else:
            await update.message.reply_text("No candle alerts to remove")
        return

    focus = _get_focus(chat_id)

    # Multi-arg with focus pair: /del 5 15 30
    if focus and len(args) >= 1:
        tfs = []
        for a in args:
            tf = parse_tf(a)
            if tf is None:
                tfs = None
                break
            tfs.append(tf)
        if tfs:
            display = _display_symbol(focus)
            total = 0
            for tf in tfs:
                n = db.delete_candle_alerts_by(chat_id, symbol=focus, timeframe_min=tf)
                total += n
            if total > 0:
                scheduler.subscriptions_changed.set()
                await update.message.reply_text(f"🗑️ Removed {total} {display.upper()} alert(s)")
            else:
                await update.message.reply_text(f"No {display.upper()} alerts to remove")
            return

    if len(args) == 1:
        # /del TIMEFRAME — remove timer-only alerts with that tf (no focus pair)
        tf = parse_tf(args[0])
        if tf is not None:
            n = db.delete_candle_alerts_by(chat_id, timeframe_min=tf)
            if n > 0:
                scheduler.subscriptions_changed.set()
                await update.message.reply_text(f"🗑️ Removed {n} {tf_label(tf)} alert(s)")
            else:
                await update.message.reply_text(f"No {tf_label(tf)} alerts")
        else:
            await update.message.reply_text(_err(f"Unknown timeframe: {args[0]} — use /del SYMBOL TIMEFRAME"))
        return

    # /del SYMBOL TIMEFRAME [TIMEFRAME ...]
    symbol = args[0].upper()
    resolved = await mt5_data.resolve_symbol(symbol)
    if resolved is None:
        await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
        return

    tfs = []
    for a in args[1:]:
        tf = parse_tf(a)
        if tf is None:
            await update.message.reply_text(_err(f"Unknown timeframe: {a}"))
            return
        tfs.append(tf)

    display = _display_symbol(resolved)
    total = 0
    for tf in tfs:
        n = db.delete_candle_alerts_by(chat_id, symbol=resolved, timeframe_min=tf)
        total += n
    if total > 0:
        scheduler.subscriptions_changed.set()
        await update.message.reply_text(f"🗑️ Removed {total} {display.upper()} alert(s)")
    else:
        await update.message.reply_text(f"No {display.upper()} alerts to remove")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    candle_alerts = db.get_candle_alerts(chat_id)
    price_alerts = db.get_price_alerts(chat_id)

    if not candle_alerts and not price_alerts:
        tz = db.get_user(chat_id).timezone
        await update.message.reply_text(
            f"No active alerts\n"
            f"Offset: {db.get_user(chat_id).default_offset_s}s\n"
            f"Timezone: {tz}"
        )
        return

    lines = []
    if candle_alerts:
        lines.append("<b>Candle alerts:</b>")
        for a in candle_alerts:
            if a.symbol:
                display = _display_symbol(a.symbol)
                lines.append(f"  {display.upper()} {tf_label(a.timeframe_min)}")
            else:
                lines.append(f"  Timer-only {tf_label(a.timeframe_min)}")

    if price_alerts:
        lines.append("")
        lines.append("<b>Price alerts:</b>")
        for a in price_alerts:
            display = _display_symbol(a.symbol)
            dir_str = f" {a.direction}" if a.direction else ""
            lines.append(f"  p{a.user_seq} {display.upper()}{dir_str} {a.target}")

    focus = _get_focus(chat_id)
    if focus:
        lines.append(f"\n🎯 Focus: {_display_symbol(focus).upper()}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_offset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args or []

    if not args:
        user = db.get_user(chat_id)
        await update.message.reply_text(
            f"Pre-close offset: {user.default_offset_s}s\n"
            f"Usage: /offset SECONDS\n"
            f"Example: /offset 8"
        )
        return

    try:
        offset = int(args[0])
    except ValueError:
        await update.message.reply_text(_err(f"Invalid offset: {args[0]} — must be a number"))
        return

    if offset < 0 or offset > 60:
        await update.message.reply_text(_err("Offset must be 0–60 seconds"))
        return

    db.update_user(chat_id, default_offset_s=offset)
    scheduler.subscriptions_changed.set()
    await update.message.reply_text(f"✅ Pre-close offset set to {offset}s")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        # /now with no args: if fp is active, default to M1
        focus = _get_focus(chat_id)
        if focus:
            args = ["1"]
        else:
            await update.message.reply_text(_err("Usage: /now [SYMBOL] TIMEFRAME\nExample: /now xauusd 3\nOr set focus with /fp first, then /now 5"))
            return

    if len(args) == 1:
        focus = _get_focus(chat_id)
        if focus:
            tf = parse_tf(args[0])
            if tf is None:
                await update.message.reply_text(_err(f"Unknown timeframe: {args[0]}"))
                return
            resolved = focus
        else:
            await update.message.reply_text(_err("Usage: /now SYMBOL TIMEFRAME\nOr set focus with /fp first"))
            return
    else:
        symbol = args[0].upper()
        tf = parse_tf(args[1])
        if tf is None:
            await update.message.reply_text(_err(f"Unknown timeframe: {args[1]}"))
            return
        resolved = await mt5_data.resolve_symbol(symbol)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
            return

    tick = await mt5_data.tick(resolved)
    sinfo = await mt5_data.symbol_info(resolved)
    # Use the last completed bar (position 1), not the current incomplete bar
    bar = await mt5_data.previous_bar(resolved, tf)
    prev = await mt5_data.bar_at_offset(resolved, tf, 2)

    if bar is None:
        await update.message.reply_text(_err(f"No data for {_display_symbol(resolved)} {tf_label(tf)}"))
        return

    text = _format_candle_message(resolved, tf, bar, prev, tick, sinfo, bar.time + tf * 60, time.time(), chat_id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        focus = _get_focus(chat_id)
        if focus:
            resolved = focus
        else:
            await update.message.reply_text(_err("Usage: /level SYMBOL\nOr set focus with /fp first\nExample: /level xauusd"))
            return
    else:
        symbol = args[0].lower()
        resolved = await mt5_data.resolve_symbol(symbol)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
            return

    yesterday = await mt5_data.previous_day_bar(resolved)
    today = await mt5_data.today_open_bar(resolved)
    sinfo = await mt5_data.symbol_info(resolved)
    display = _display_symbol(resolved)

    lines = [f"<b>{display.upper()} Key Levels</b>\n"]

    if yesterday:
        lines.append(f"<b>Yesterday:</b>")
        lines.append(f"  O {_fmt_ohlc(yesterday.open, resolved, sinfo)}")
        lines.append(f"  H {_fmt_ohlc(yesterday.high, resolved, sinfo)}")
        lines.append(f"  L {_fmt_ohlc(yesterday.low, resolved, sinfo)}")
        lines.append(f"  C {_fmt_ohlc(yesterday.close, resolved, sinfo)}")
        dt = datetime.fromtimestamp(yesterday.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"  ({dt})\n")

    if today:
        lines.append(f"<b>Today Open:</b> {_fmt_ohlc(today.open, resolved, sinfo)}")
        dt = datetime.fromtimestamp(today.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"  ({dt})")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args or []

    if len(args) < 1:
        await update.message.reply_text(_err(
            "Usage:\n"
            "/price [SYMBOL] <price> [+50] [-30] [30m|2h] [close]\n"
            "  /price 2400 2450 2500 (multi crossing)\n"
            "  /price +50 -30 (relative pips)\n"
            "  /price close 2400 2450 (close range, smart-sorted)\n"
            "  /price close 2400 (cross and close)\n"
            "  /price 2400 30m (expires in 30 minutes)"
        ))
        return

    focus = _get_focus(chat_id)
    resolved = None
    price_args = args

    # Determine if first arg is a symbol (not a number, close, relative, or expiry)
    first = args[0]
    is_symbol = False
    try:
        float(first)
    except ValueError:
        if first.lower() != "close" and not _REL_RE.match(first) and _parse_expiry(first) is None:
            is_symbol = True

    if is_symbol:
        resolved = await mt5_data.resolve_symbol(first)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol not found: {first}"))
            return
        price_args = args[1:]
    elif focus:
        resolved = focus
    else:
        await update.message.reply_text(_err(
            "Usage: /price <symbol> <price> ...\n"
            "Or set focus with /fp first, then /price 2400"
        ))
        return

    if not price_args:
        await update.message.reply_text(_err("No price specified"))
        return

    # Get tick data
    tick = await mt5_data.tick(resolved)
    if tick is None:
        await update.message.reply_text(_err(f"No tick data for {_display_symbol(resolved)}"))
        return

    sinfo = await mt5_data.symbol_info(resolved)
    pip_size = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01

    # Parse args
    alert_type, boundaries, expiry_s, expiry_label = _resolve_price_args(
        price_args, tick.bid, pip_size
    )

    if not boundaries:
        await update.message.reply_text(_err("No valid price specified"))
        return

    # Compute expires_at
    from datetime import timedelta
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expiry_s)).isoformat() if expiry_s else None

    display = _display_symbol(resolved)

    if alert_type == "close":
        # Close type: 1 or 2 boundaries
        lower = boundaries[0]
        upper = boundaries[1] if len(boundaries) > 1 else None

        if upper is not None:
            # Range close: price must close within [lower, upper]
            alert = db.add_price_alert(
                chat_id, resolved, lower,
                alert_type="close", target_upper=upper, expires_at=expires_at,
            )
            current_side = "above" if tick.bid > upper else ("below" if tick.bid < lower else "inside")
            db.update_price_alert(alert.id, last_side=current_side)
            alert.last_side = current_side
            exp_str = f" {_fmt_expiry(expires_at)}" if expires_at else ""
            await update.message.reply_text(
                f"✅ Close alert p{alert.user_seq}{exp_str}\n"
                f"{display.upper()} close {lower:.2f}–{upper:.2f}\n"
                f"Current bid: {_fmt_ohlc(tick.bid, resolved, None)}\n"
                f"/cancel p{alert.user_seq} to remove"
            )
        else:
            # Single boundary close: cross and close
            alert = db.add_price_alert(
                chat_id, resolved, lower,
                alert_type="close", expires_at=expires_at,
            )
            current_side = "above" if tick.bid > lower else "below"
            db.update_price_alert(alert.id, last_side=current_side)
            alert.last_side = current_side
            exp_str = f" {_fmt_expiry(expires_at)}" if expires_at else ""
            await update.message.reply_text(
                f"✅ Close alert p{alert.user_seq}{exp_str}\n"
                f"{display.upper()} close {'above' if current_side == 'below' else 'below'} {lower:.2f}\n"
                f"Current bid: {_fmt_ohlc(tick.bid, resolved, None)}\n"
                f"/cancel p{alert.user_seq} to remove"
            )
    else:
        # Crossing type: one alert per boundary
        lines = []
        for target in boundaries:
            current_side = "above" if tick.bid > target else "below"
            alert = db.add_price_alert(
                chat_id, resolved, target, None,
                alert_type="crossing", expires_at=expires_at,
            )
            db.update_price_alert(alert.id, last_side=current_side)
            lines.append(f"p{alert.user_seq} {display.upper()} crossing {target}")
        exp_str = f" (expires in {expiry_label})" if expiry_label else ""
        lines.append(f"Current bid: {_fmt_ohlc(tick.bid, resolved, None)}")
        await update.message.reply_text("\n".join(["✅ Price alerts:"] + lines) + exp_str)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        # /cancel without parameters → cancel all price alerts
        alerts = db.get_price_alerts(chat_id)
        if not alerts:
            await update.message.reply_text("No active price alerts to cancel")
            return
        n = 0
        for a in alerts:
            db.update_price_alert(a.id, enabled=False)
            n += 1
        await update.message.reply_text(f"🗑️ Cancelled {n} price alert(s)" if n > 1 else "🗑️ Cancelled 1 price alert")
        return

    raw = args[0].lower()
    if raw.startswith("p"):
        try:
            user_seq = int(raw[1:])
        except ValueError:
            await update.message.reply_text(_err(f"Invalid ID: {raw}"))
            return

        alert = db.get_price_alert_by_user_seq(chat_id, user_seq)
        if alert is None:
            await update.message.reply_text(_err(f"Price alert p{user_seq} not found"))
            return

        db.update_price_alert(alert.id, enabled=False)
        await update.message.reply_text(f"🗑️ Removed price alert p{user_seq}")
    else:
        await update.message.reply_text(_err("Use /cancel pID for price alerts\n/del for candle alerts"))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    health = await mt5_data.health()

    if not health["connected"]:
        await update.message.reply_text(
            f"⚠️ MT5 disconnected\n{health.get('error', '')}"
        )
        return

    candle_count = len(db.get_candle_alerts(chat_id))
    price_count = len(db.get_price_alerts(chat_id))
    focus = _get_focus(chat_id)

    lines = [
        f"<b>MT5</b> — connected",
        f"Build {health['build']}",
        f"Server: {health['server']}",
        f"Active alerts: {candle_count} candle, {price_count} price",
        f"Bot time: {_now_utc()} UTC",
    ]
    if focus:
        lines.append(f"Focus pair: {_display_symbol(focus).upper()}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_mark_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shorthand for /mark del [id]."""
    context.args = ["del"] + (context.args or [])
    await cmd_mark(update, context)


async def cmd_mark_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shorthand for /mark list [symbol]."""
    context.args = ["list"] + (context.args or [])
    await cmd_mark(update, context)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all alerts and marks for this user."""
    chat_id = update.effective_chat.id

    n_candle = db.delete_candle_alerts_by(chat_id)
    n_price = 0
    for a in db.get_price_alerts(chat_id):
        db.update_price_alert(a.id, enabled=False)
        n_price += 1
    n_mark = db.delete_all_marks(chat_id)

    if n_candle > 0:
        scheduler.subscriptions_changed.set()

    parts = []
    if n_candle: parts.append(f"{n_candle} candle")
    if n_price: parts.append(f"{n_price} price")
    if n_mark: parts.append(f"{n_mark} mark")

    if parts:
        await update.message.reply_text(f"🧹 Cleared: {', '.join(parts)}")
    else:
        await update.message.reply_text("Nothing to clear")


async def cmd_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open or list paper trades. /entry [SYMBOL] BUY|SELL [LIMIT|STOP PRICE] [+TP] [-SL]"""
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args or []

    # No args or "list" → show open trades
    if len(args) == 0 or (len(args) == 1 and args[0].lower() in ("list", "ls")):
        trades = db.get_paper_trades(chat_id)
        if not trades:
            await update.message.reply_text("No open paper trades")
            return
        lines = []
        for t in trades:
            pnl = await _calc_unrealized(t, chat_id)
            ot = f" {t.order_type}" if t.order_type != "market" else ""
            pending = "⏳" if t.order_type in ("limit", "stop") else ""
            sl_tp = ""
            if t.order_type == "market":
                sl_tp = f" | SL:{_fmt_price(t.stop_loss, t.symbol)} TP:{_fmt_price(t.take_profit, t.symbol)}"
            lines.append(
                f"{pending}t{t.user_seq} {_display_symbol(t.symbol).upper()} {t.direction.upper()}{ot} @ {_fmt_price(t.entry_price, t.symbol)}{sl_tp}"
                f" | {pnl:+.1f}p"
            )
        await update.message.reply_text("\n".join(lines))
        return

    # Parse: [SYMBOL] BUY|SELL [LIMIT|STOP PRICE] [+TP] [-SL]
    focus = _get_focus(chat_id)
    resolved = None
    trade_args = args

    first = args[0].lower()
    if first in ("buy", "sell"):
        if not focus:
            await update.message.reply_text(_err("Usage: /entry SYMBOL BUY|SELL ...\nOr set focus with /fp first"))
            return
        resolved = focus
        direction = first
        trade_args = args[1:]
    else:
        resolved = await mt5_data.resolve_symbol(first)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol not found: {first}"))
            return
        if len(args) < 2 or args[1].lower() not in ("buy", "sell"):
            await update.message.reply_text(_err("Usage: /entry SYMBOL BUY|SELL [LIMIT|STOP PRICE] [+TP] [-SL]"))
            return
        direction = args[1].lower()
        trade_args = args[2:]

    tick = await mt5_data.tick(resolved)
    if tick is None:
        await update.message.reply_text(_err(f"No tick data for {_display_symbol(resolved)}"))
        return

    sinfo = await mt5_data.symbol_info(resolved)
    pip_size = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01

    # Defaults: market order
    order_type = "market"
    entry_price = tick.ask if direction == "buy" else tick.bid
    stop_loss = None
    take_profit = None

    i = 0
    while i < len(trade_args):
        a = trade_args[i].lower()
        if a in ("limit", "stop"):
            if i + 1 >= len(trade_args):
                break
            order_type = a
            try:
                entry_price = float(trade_args[i + 1])
            except ValueError:
                await update.message.reply_text(_err(f"Invalid {a} price: {trade_args[i + 1]}"))
                return
            # Validate
            current = tick.ask if direction == "buy" else tick.bid
            if a == "limit":
                if direction == "buy" and entry_price >= current:
                    await update.message.reply_text(_err(f"Buy limit {entry_price} must be < current {_fmt_price(current, resolved)}"))
                    return
                if direction == "sell" and entry_price <= current:
                    await update.message.reply_text(_err(f"Sell limit {entry_price} must be > current {_fmt_price(current, resolved)}"))
                    return
            else:  # stop
                if direction == "buy" and entry_price <= current:
                    await update.message.reply_text(_err(f"Buy stop {entry_price} must be > current {_fmt_price(current, resolved)}"))
                    return
                if direction == "sell" and entry_price >= current:
                    await update.message.reply_text(_err(f"Sell stop {entry_price} must be < current {_fmt_price(current, resolved)}"))
                    return
            i += 2
        elif _REL_RE.match(a):
            pips = float(a[1:])
            price = entry_price + pips * pip_size if a[0] == "+" else entry_price - pips * pip_size
            if direction == "buy":
                if a[0] == "+":
                    take_profit = price
                else:
                    stop_loss = price
            else:  # sell
                if a[0] == "+":
                    take_profit = entry_price - pips * pip_size
                else:
                    stop_loss = entry_price + pips * pip_size
            i += 1
        else:
            i += 1  # skip unknown

    trade = db.add_paper_trade(chat_id, resolved, direction, order_type, entry_price, stop_loss, take_profit)
    display = _display_symbol(resolved)
    lines = [
        f"📊 t{trade.user_seq} {display.upper()} {direction.upper()} {order_type.upper()} @ {_fmt_price(entry_price, resolved)}",
    ]
    if stop_loss or take_profit:
        pending = " (pending)" if order_type in ("limit", "stop") else ""
        if stop_loss:
            lines.append(f"  SL: {_fmt_price(stop_loss, resolved)}{pending}")
        if take_profit:
            lines.append(f"  TP: {_fmt_price(take_profit, resolved)}{pending}")
    await update.message.reply_text("\n".join(lines))


async def cmd_modify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Modify or close a paper trade. /modify <id> [sl X] [tp Y] [close]"""
    chat_id = update.effective_chat.id
    args = context.args or []

    if len(args) < 2:
        await update.message.reply_text(_err(
            "Usage:\n"
            "/modify t1 sl 2390 (move stop loss)\n"
            "/modify t1 tp 2420 (move take profit)\n"
            "/modify t1 sl +30 tp -20 (relative pips)\n"
            "/modify t1 close (close at market)"
        ))
        return

    # Parse trade ID
    tid = args[0].lower()
    if tid.startswith("t"):
        tid = tid[1:]
    try:
        user_seq = int(tid)
    except ValueError:
        await update.message.reply_text(_err(f"Invalid trade ID: {args[0]}"))
        return

    trade = db.get_paper_trade_by_user_seq(chat_id, user_seq)
    if trade is None:
        await update.message.reply_text(_err(f"Trade t{user_seq} not found"))
        return

    # Get tick for current price
    tick = await mt5_data.tick(trade.symbol)
    if tick is None:
        await update.message.reply_text(_err(f"No tick data for {_display_symbol(trade.symbol)}"))
        return

    sinfo = await mt5_data.symbol_info(trade.symbol)
    pip_size = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01

    mod_args = args[1:]
    i = 0
    while i < len(mod_args):
        a = mod_args[i].lower()
        if a == "sl" and i + 1 < len(mod_args):
            sl_val = mod_args[i + 1]
            new_sl = _resolve_relative_price(sl_val, trade.entry_price, pip_size)
            db.update_paper_trade(trade.id, stop_loss=new_sl)
            trade.stop_loss = new_sl
            await update.message.reply_text(f"✅ t{user_seq} SL → {_fmt_price(new_sl, trade.symbol)}")
            i += 2
        elif a == "tp" and i + 1 < len(mod_args):
            tp_val = mod_args[i + 1]
            new_tp = _resolve_relative_price(tp_val, trade.entry_price, pip_size)
            db.update_paper_trade(trade.id, take_profit=new_tp)
            trade.take_profit = new_tp
            await update.message.reply_text(f"✅ t{user_seq} TP → {_fmt_price(new_tp, trade.symbol)}")
            i += 2
        elif a == "close":
            exit_price = tick.bid if trade.direction == "buy" else tick.ask
            if trade.direction == "buy":
                pnl = (exit_price - trade.entry_price) / pip_size
            else:
                pnl = (trade.entry_price - exit_price) / pip_size
            db.close_paper_trade(trade.id, exit_price, pnl)
            await update.message.reply_text(
                f"🔒 t{user_seq} closed @ {_fmt_price(exit_price, trade.symbol)}\n"
                f"P&L: {pnl:+.1f} pips"
            )
            i += 1
        else:
            i += 1  # skip unknown


def _resolve_relative_price(val: str, base_price: float, pip_size: float) -> float:
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


async def _calc_unrealized(trade: "PaperTrade", chat_id: int) -> float:
    """Calculate unrealized P&L in pips for a paper trade."""
    tick = await mt5_data.tick(trade.symbol)
    if tick is None:
        return 0.0
    sinfo = await mt5_data.symbol_info(trade.symbol)
    pip_size = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01
    if trade.direction == "buy":
        return (tick.bid - trade.entry_price) / pip_size
    else:
        return (trade.entry_price - tick.ask) / pip_size


async def cmd_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle which data sections appear in candle alerts."""
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args

    VALID_PREFS = {"show_pattern", "show_ohlc", "show_range_body", "show_bid_ask", "show_marks"}

    if not args:
        prefs = db.get_user_prefs(chat_id)
        lines = [f"{k}: {prefs.get(k, 'on')}" for k in sorted(VALID_PREFS)]
        await update.message.reply_text("\n".join(lines) or "all on (default)")
        return

    if args[0].lower() == "list":
        prefs = db.get_user_prefs(chat_id)
        lines = [f"{k}: {prefs.get(k, 'on')}" for k in sorted(VALID_PREFS)]
        await update.message.reply_text("\n".join(lines))
        return

    if len(args) < 2:
        await update.message.reply_text(_err("Use: /data on|off <section> [section ...]\n  /data off show_bid_ask show_range"))
        return

    action = args[0].lower()
    if action not in ("on", "off"):
        await update.message.reply_text(_err("Use: /data on|off <section> [section ...]"))
        return

    keys = [a.lower() for a in args[1:]]
    invalid = [k for k in keys if k not in VALID_PREFS]
    if invalid:
        await update.message.reply_text(
            _err(f"Unknown section(s): {', '.join(invalid)}\nOptions: {', '.join(sorted(VALID_PREFS))}")
        )
        return

    for key in keys:
        db.set_user_pref(chat_id, key, action)
    plural = "s" if len(keys) > 1 else ""
    await update.message.reply_text(f"✅ {', '.join(keys)} = {action}")


async def cmd_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a price level, show distance in candle alerts."""
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args

    if not args:
        await update.message.reply_text(_err(
            "Usage:\n"
            "/mark <symbol> <price> [price ...] [30m|2h|45s]\n"
            "/mark del [id] — delete all or specific\n"
            "/mark list [symbol]"
        ))
        return

    if args[0].lower() == "del":
        if len(args) < 2:
            # /mark del — delete all marks
            n = db.delete_all_marks(chat_id)
            if n > 0:
                await update.message.reply_text(f"🗑️ Deleted {n} mark(s)")
            else:
                await update.message.reply_text("No marks to delete")
            return
        try:
            user_seq = int(args[1])
        except ValueError:
            await update.message.reply_text(_err(f"Invalid mark ID: {args[1]}"))
            return
        if db.delete_mark(chat_id, user_seq):
            await update.message.reply_text(f"🗑️ Mark M{user_seq} deleted")
        else:
            await update.message.reply_text(_err(f"Mark M{user_seq} not found"))
        return

    if args[0].lower() == "list":
        symbol = args[1] if len(args) > 1 else None
        if symbol:
            resolved = await mt5_data.resolve_symbol(symbol)
            if resolved is None:
                await update.message.reply_text(_err(f"Symbol not found: {symbol}"))
                return
            symbol = resolved
        elif not symbol:
            symbol = _get_focus(chat_id)
        marks = db.get_marks(chat_id, symbol)
        if not marks:
            await update.message.reply_text("No active marks")
            return
        lines = []
        for m in marks:
            display = _display_symbol(m.symbol)
            lines.append(f"M{m.user_seq}: {display.upper()} {_fmt_price(m.price, m.symbol)}{_fmt_expiry(m.expires_at)}")
        await update.message.reply_text("\n".join(lines))
        return

    # Add mark: /mark [SYMBOL] <price> [price ...] [expiry_suffix]
    # expiry_suffix: e.g. 30m, 2h, 45s
    try:
        float(args[0])
        # First arg is a price → fp mode
        focus = _get_focus(chat_id)
        if not focus:
            await update.message.reply_text(_err("Usage: /mark <symbol> <price> [price ...] [30m|2h|45s]\nOr set focus with /fp first, then /mark 2400.50"))
            return

        # Separate prices from expiry
        prices, expiry_s, expiry_label = _parse_mark_args(args)
        if not prices:
            await update.message.reply_text(_err("No valid price specified"))
            return

        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expiry_s)).isoformat() if expiry_s else None

        display = _display_symbol(focus)
        marks = []
        for p in prices:
            m = db.add_mark(chat_id, focus, p, expires_at)
            marks.append(f"M{m.user_seq}: {display.upper()} {_fmt_price(p, focus)}")
        exp_str = f" (expires in {expiry_label})" if expiry_label else ""
        await update.message.reply_text("📍 " + "\n".join(marks) + exp_str)
        return

    except ValueError:
        # First arg is a symbol
        symbol = args[0]
        resolved = await mt5_data.resolve_symbol(symbol)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol not found: {symbol}"))
            return
        symbol = resolved

        # Separate prices from expiry in args[1:]
        prices, expiry_s, expiry_label = _parse_mark_args(args[1:])
        if not prices:
            await update.message.reply_text(_err("No valid price specified"))
            return

        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expiry_s)).isoformat() if expiry_s else None

        display = _display_symbol(symbol)
        marks = []
        for p in prices:
            m = db.add_mark(chat_id, symbol, p, expires_at)
            marks.append(f"M{m.user_seq}: {display.upper()} {_fmt_price(p, symbol)}")
        exp_str = f" (expires in {expiry_label})" if expiry_label else ""
        await update.message.reply_text("📍 " + "\n".join(marks) + exp_str)
        return


# ============================================================
# Message formatting
# ============================================================

def _format_candle_message(
    symbol: str,
    tf_min: int,
    bar: Optional[Bar],
    prev_bar: Optional[Bar],
    tick: Optional[Tick],
    sinfo: Optional[SymbolInfo],
    close_epoch: float,
    sent_epoch: float,
    chat_id: int,
) -> str:
    """Build the candle alert message."""
    display = _display_symbol(symbol) if symbol else "timer"
    prefs = db.get_user_prefs(chat_id)

    pat = patterns.classify(bar, prev_bar) if bar else None

    # Header: EMOJI SYMBOL TF BID +PIPS
    bid_str = _fmt_ohlc(tick.bid, symbol, sinfo) if tick else "—"
    emoji = pat.emoji if pat else "⏳"
    pip_change = ""
    if bar and tick and sinfo and prev_bar:
        point_size = sinfo.point
        if point_size > 0:
            pip_size = point_size * 10  # 1 pip = 10 points for forex
            change_pips = (tick.bid - prev_bar.close) / pip_size
            sign = "+" if change_pips >= 0 else ""
            pip_change = f" {sign}{change_pips:.1f}p"
    lines = [f"{emoji} {display.upper()} {tf_label(tf_min)} {bid_str}{pip_change}"]

    if bar:
        # Pattern
        if prefs.get("show_pattern", "on") != "off" and pat:
            lines.append(f"{pat.label}")
        # OHLC
        if prefs.get("show_ohlc", "on") != "off":
            lines.append(
                f"O {_fmt_ohlc(bar.open, symbol, sinfo)}  "
                f"H {_fmt_ohlc(bar.high, symbol, sinfo)}  "
                f"L {_fmt_ohlc(bar.low, symbol, sinfo)}  "
                f"C {_fmt_ohlc(bar.close, symbol, sinfo)}"
            )
        # Range + Body + Wicks
        if prefs.get("show_range_body", "on") != "off":
            range_p = bar.high - bar.low
            body_p = abs(bar.close - bar.open)
            tw = bar.high - max(bar.open, bar.close)
            bw = min(bar.open, bar.close) - bar.low
            lines.append(
                f"Range {_fmt_ohlc(range_p, symbol, sinfo)}  "
                f"Body {_fmt_ohlc(body_p, symbol, sinfo)}  "
                f"TW {_fmt_ohlc(tw, symbol, sinfo)}  "
                f"BW {_fmt_ohlc(bw, symbol, sinfo)}"
            )

    if tick and prefs.get("show_bid_ask", "off") != "off":
        spread = tick.spread
        lines.append(
            f"Bid {_fmt_ohlc(tick.bid, symbol, sinfo)}  "
            f"Ask {_fmt_ohlc(tick.ask, symbol, sinfo)}  "
            f"Spread {_fmt_spread(spread, sinfo)}"
        )

    # Marks — show distance from current price
    if tick and sinfo and symbol and prefs.get("show_marks", "on") != "off":
        marks = db.get_marks(chat_id, symbol)
        if marks:
            pip_size = sinfo.point * 10 if sinfo.point > 0 else 0.01
            for m in marks:
                dist_pips = (tick.bid - m.price) / pip_size
                sign = "+" if dist_pips >= 0 else ""
                lines.append(f"📍 M{m.user_seq} {_fmt_ohlc(m.price, symbol, sinfo)}  {sign}{dist_pips:.1f}p")

    return "\n".join(lines)


def _format_price_alert_message(
    alert: PriceAlert,
    price: float,
    tick: Tick,
    chat_id: int,
) -> str:
    display = _display_symbol(alert.symbol)
    if alert.alert_type == "close":
        if alert.target_upper is not None:
            msg = (
                f"🔔 <b>{display.upper()}</b> closed within {alert.target:.2f}–{alert.target_upper:.2f}!\n"
            )
        else:
            msg = f"🔔 <b>{display.upper()}</b> closed beyond {alert.target:.2f}!\n"
    else:
        dir_str = f"crossed {alert.direction}" if alert.direction else "crossed"
        msg = f"🔔 <b>{display.upper()}</b> {dir_str} {alert.target}!\n"
    return msg + (
        f"Bid: {_fmt_ohlc(tick.bid, alert.symbol, None)}  "
        f"Ask: {_fmt_ohlc(tick.ask, alert.symbol, None)}"
    )


def _fmt_price(price: float, symbol: str) -> str:
    """Format price with appropriate decimal places for the symbol."""
    if price >= 1000:
        return f"{price:.2f}"
    elif price >= 100:
        return f"{price:.3f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.5f}"


def _fmt_spread(spread: float, sinfo: Optional[SymbolInfo]) -> str:
    if sinfo is None:
        return f"{spread:.5f}"
    points = spread / sinfo.point
    return f"{points:.1f}"


def _fmt_ohlc(value: float, symbol: str, sinfo: Optional[SymbolInfo]) -> str:
    """Format OHLC price using symbol digits if available."""
    if sinfo is not None:
        return f"{value:.{sinfo.digits}f}"
    return _fmt_price(value, symbol)


# ============================================================
# Scheduler callbacks
# ============================================================

async def _send_candle(
    chat_id: int,
    symbol: Optional[str],
    timeframe_min: int,
    bar: Optional[Bar],
    prev_bar: Optional[Bar],
    tick: Optional[Tick],
    sinfo: Optional[SymbolInfo],
    close_epoch: float,
    sent_epoch: float,
) -> None:
    """Called by the scheduler to deliver a candle alert."""
    if symbol is None:
        # Timer-only: just a countdown notification
        text = (
            f"⏰ {tf_label(timeframe_min)} candle closing\n"
            f"Time: {_now_utc()} UTC"
        )
    else:
        text = _format_candle_message(
            symbol, timeframe_min, bar, prev_bar, tick, sinfo, close_epoch, sent_epoch, chat_id
        )

    # Use bot from context — but we need the app reference
    if _app_ref:
        await _app_ref.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


async def _send_price(
    chat_id: int,
    alert: PriceAlert,
    price: float,
    tick: Tick,
) -> None:
    """Called by the scheduler to deliver a price alert."""
    text = _format_price_alert_message(alert, price, tick, chat_id)
    if _app_ref:
        await _app_ref.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


async def _send_error(chat_id: int, msg: str) -> None:
    if _app_ref:
        try:
            await _app_ref.bot.send_message(chat_id=chat_id, text=f"⚠️ {msg}")
        except Exception:
            pass


async def _send_paper_trade(
    chat_id: int,
    trade,
    event: str,
    price: float,
    pnl: float = 0.0,
) -> None:
    """Called by the scheduler for paper trade events (activated, sl_hit, tp_hit)."""
    display = _display_symbol(trade.symbol)
    dir_str = trade.direction.upper()
    price_str = _fmt_price(price, trade.symbol)

    if event == "activated":
        text = (
            f"✅ t{trade.user_seq} {display.upper()} {dir_str} ACTIVATED @ {price_str}\n"
            f"SL: {_fmt_price(trade.stop_loss, trade.symbol)} | TP: {_fmt_price(trade.take_profit, trade.symbol)}"
        )
    elif event == "sl_hit":
        text = f"🛑 t{trade.user_seq} {display.upper()} {dir_str} SL hit @ {price_str} | {pnl:+.1f}p"
    elif event == "tp_hit":
        text = f"🎯 t{trade.user_seq} {display.upper()} {dir_str} TP hit @ {price_str} | {pnl:+.1f}p"
    else:
        return

    if _app_ref:
        try:
            await _app_ref.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---- app reference for scheduler callbacks ----
_app_ref: "Application | None" = None

# Command dispatch table for dot-prefix MessageHandler
_COMMANDS: dict[str, callable] = {}


async def _handle_dot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle .command messages — strip dot, parse, dispatch to existing handlers."""
    text = update.message.text.strip()
    if not text.startswith("."):
        return
    parts = text[1:].split()
    if not parts:
        return
    cmd = parts[0].lower()
    handler = _COMMANDS.get(cmd)
    if handler is None:
        return  # silently ignore unknown dot commands
    context.args = parts[1:]
    await handler(update, context)


def build_app() -> Application:
    """Build and configure the PTB Application."""
    app = Application.builder().token(config.BOT_TOKEN).build()

    # Store app reference for scheduler callbacks
    global _app_ref
    _app_ref = app

    # Register handlers (full names)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("focus_pair", cmd_focus_pair))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("offset", cmd_offset))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("level", cmd_level))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))

    # Shorthand aliases
    app.add_handler(CommandHandler("fp", cmd_focus_pair))
    app.add_handler(CommandHandler("a", cmd_add))
    app.add_handler(CommandHandler("d", cmd_del))
    app.add_handler(CommandHandler("l", cmd_list))
    app.add_handler(CommandHandler("o", cmd_offset))
    app.add_handler(CommandHandler("n", cmd_now))
    app.add_handler(CommandHandler("lv", cmd_level))
    app.add_handler(CommandHandler("p", cmd_price))
    app.add_handler(CommandHandler("c", cmd_cancel))
    app.add_handler(CommandHandler("s", cmd_status))
    app.add_handler(CommandHandler("data", cmd_data))
    app.add_handler(CommandHandler("dt", cmd_data))
    app.add_handler(CommandHandler("mark", cmd_mark))
    app.add_handler(CommandHandler("mk", cmd_mark))
    app.add_handler(CommandHandler("mkd", cmd_mark_del))
    app.add_handler(CommandHandler("mkl", cmd_mark_list))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("modify", cmd_modify))
    app.add_handler(CommandHandler("e", cmd_entry))
    app.add_handler(CommandHandler("m", cmd_modify))

    # Populate dot-prefix dispatch table
    _COMMANDS.update({
        "help": cmd_help,
        "focus_pair": cmd_focus_pair, "fp": cmd_focus_pair,
        "add": cmd_add, "a": cmd_add,
        "del": cmd_del, "d": cmd_del,
        "list": cmd_list, "l": cmd_list,
        "offset": cmd_offset, "o": cmd_offset,
        "now": cmd_now, "n": cmd_now,
        "level": cmd_level, "lv": cmd_level,
        "price": cmd_price, "p": cmd_price,
        "cancel": cmd_cancel, "c": cmd_cancel,
        "status": cmd_status, "s": cmd_status,
        "data": cmd_data, "dt": cmd_data,
        "mark": cmd_mark, "mk": cmd_mark,
        "mkd": cmd_mark_del, "mkl": cmd_mark_list,
        "clear": cmd_clear,
        "entry": cmd_entry, "e": cmd_entry,
        "modify": cmd_modify, "m": cmd_modify,
    })

    # Dot-prefix MessageHandler (e.g. ".add xauusd 5")
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r'^\.\w+'), _handle_dot_command
    ))

    # Start scheduler in background
    async def post_init(app: Application):
        asyncio.create_task(
            scheduler.scheduler_loop(_send_candle, _send_price, _send_error, _send_paper_trade),
            name="scheduler",
        )

    app.post_init = post_init

    return app
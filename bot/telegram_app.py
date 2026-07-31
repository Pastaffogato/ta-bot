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
from telegram.ext import Application, CommandHandler, ContextTypes

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
        "<b>Other:</b>\n"
        "/status — bot health\n"
        "/help — this text\n\n"
        "<b>Timeframes:</b> 3, 5, 15, m3, M5, h1, H4\n"
        "<b>Shorthand:</b> /a, /d, /l, /o, /n, /lv, /p, /c, /s\n"
        "<b>Focus pair:</b> set /fp, then /a 5 = /a PAIR 5",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args or []

    if not args:
        await update.message.reply_text(_err("Usage: /add [SYMBOL] TIMEFRAME\nExample: /add 5  or  /add XAUUSD 5"))
        return

    if len(args) == 1:
        focus = _get_focus(chat_id)
        if focus:
            # /add TIMEFRAME with focus pair → symbol + tf
            tf = parse_tf(args[0])
            if tf is None:
                await update.message.reply_text(_err(f"Unknown timeframe: {args[0]}"))
                return
            resolved = focus
            display = _display_symbol(resolved)
            try:
                alert = db.add_candle_alert(chat_id, symbol=resolved, timeframe_min=tf)
                scheduler.subscriptions_changed.set()
                await update.message.reply_text(
                    f"✅ {display.upper()} {tf_label(tf)} alert\n"
                    f"Pre-close offset: {db.get_user(chat_id).default_offset_s}s\n"
                    f"/del {display} {tf} to remove"
                )
            except ValueError as e:
                await update.message.reply_text(_err(str(e)))
            return

        # /add TIMEFRAME — timer-only
        tf = parse_tf(args[0])
        if tf is None:
            await update.message.reply_text(_err(f"Unknown timeframe: {args[0]}"))
            return
        try:
            alert = db.add_candle_alert(chat_id, symbol=None, timeframe_min=tf)
            scheduler.subscriptions_changed.set()
            await update.message.reply_text(
                f"✅ Timer-only {tf_label(tf)} alert\n"
                f"Pre-close offset: {db.get_user(chat_id).default_offset_s}s\n"
                f"/del to remove"
            )
        except ValueError as e:
            await update.message.reply_text(_err(str(e)))

    else:
        # /add SYMBOL TIMEFRAME
        symbol = args[0].upper()
        tf = parse_tf(args[1])
        if tf is None:
            await update.message.reply_text(_err(f"Unknown timeframe: {args[1]}"))
            return

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

        try:
            alert = db.add_candle_alert(chat_id, symbol=resolved, timeframe_min=tf)
            scheduler.subscriptions_changed.set()
            display = _display_symbol(resolved)
            await update.message.reply_text(
                f"✅ {display.upper()} {tf_label(tf)} alert\n"
                f"Pre-close offset: {db.get_user(chat_id).default_offset_s}s\n"
                f"/del {display} {tf} to remove"
            )
        except ValueError as e:
            await update.message.reply_text(_err(str(e)))


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

    if len(args) == 1:
        focus = _get_focus(chat_id)
        if focus:
            # /del TIMEFRAME with focus pair → delete focus+tf
            tf = parse_tf(args[0])
            if tf is not None:
                n = db.delete_candle_alerts_by(chat_id, symbol=focus, timeframe_min=tf)
                if n > 0:
                    scheduler.subscriptions_changed.set()
                    display = _display_symbol(focus)
                    await update.message.reply_text(f"🗑️ Removed {n} {display.upper()} {tf_label(tf)} alert(s)")
                else:
                    await update.message.reply_text(f"No {_display_symbol(focus).upper()} {tf_label(tf)} alerts")
                return

        # /del TIMEFRAME — remove timer-only alerts with that tf
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

    # /del SYMBOL TIMEFRAME
    symbol = args[0].upper()
    tf = parse_tf(args[1])
    if tf is None:
        await update.message.reply_text(_err(f"Unknown timeframe: {args[1]}"))
        return

    resolved = await mt5_data.resolve_symbol(symbol)
    if resolved is None:
        await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
        return

    n = db.delete_candle_alerts_by(chat_id, symbol=resolved, timeframe_min=tf)
    if n > 0:
        scheduler.subscriptions_changed.set()
        display = _display_symbol(resolved)
        await update.message.reply_text(f"🗑️ Removed {n} {display.upper()} {tf_label(tf)} alert(s)")
    else:
        await update.message.reply_text(f"No {_display_symbol(resolved).upper()} {tf_label(tf)} alerts")


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
        await update.message.reply_text(_err("Usage: /now [SYMBOL] TIMEFRAME\nExample: /now xauusd 3"))
        return

    if len(args) == 1:
        focus = _get_focus(chat_id)
        if focus:
            # /now TIMEFRAME with focus
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
    bar = await mt5_data.current_bar(resolved, tf)
    prev = await mt5_data.previous_bar(resolved, tf)

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
        await update.message.reply_text(
            _err("Usage: /price [SYMBOL] TARGET [ABOVE/BELOW]\n"
                 "  /price xauusd 2400\n"
                 "  /price 2400 (with focus pair)\n"
                 "  /price xauusd above 2400")
        )
        return

    direction = None
    target = None
    symbol = None

    if len(args) == 1:
        # /price TARGET — needs focus pair
        focus = _get_focus(chat_id)
        if not focus:
            await update.message.reply_text(
                _err("Usage: /price [SYMBOL] TARGET\n"
                     "Or set focus with /fp first, then /price 2600")
            )
            return
        try:
            target = float(args[0])
        except ValueError:
            await update.message.reply_text(_err(f"Invalid price target: {args[0]}"))
            return
        symbol = _display_symbol(focus)
        resolved = focus

    elif len(args) == 2:
        if args[0].lower() in ("above", "below"):
            # /price ABOVE/BELOW TARGET — needs focus pair
            focus = _get_focus(chat_id)
            if not focus:
                await update.message.reply_text(
                    _err("Usage: /price SYMBOL ABOVE TARGET\n"
                         "Or set focus with /fp first, then /price above 2600")
                )
                return
            direction = args[0].lower()
            try:
                target = float(args[1])
            except ValueError:
                await update.message.reply_text(_err(f"Invalid price target: {args[1]}"))
                return
            resolved = focus
            symbol = _display_symbol(focus)
        else:
            # /price SYMBOL TARGET
            try:
                target = float(args[1])
            except ValueError:
                await update.message.reply_text(_err(f"Invalid price target: {args[1]}"))
                return
            symbol = args[0].upper()
            resolved = await mt5_data.resolve_symbol(symbol)
            if resolved is None:
                await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
                return

    else:
        # /price SYMBOL ABOVE/BELOW TARGET
        symbol = args[0].upper()
        if args[1].lower() in ("above", "below"):
            direction = args[1].lower()
            try:
                target = float(args[2])
            except ValueError:
                await update.message.reply_text(_err(f"Invalid price target: {args[2]}"))
                return
        else:
            await update.message.reply_text(_err("Usage: /price SYMBOL [ABOVE/BELOW] TARGET"))
            return

        resolved = await mt5_data.resolve_symbol(symbol)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol '{symbol}' not found"))
            return

    if target is None:
        await update.message.reply_text(_err("Missing price target"))
        return

    tick = await mt5_data.tick(resolved)
    if tick is None:
        await update.message.reply_text(_err(f"No tick data for {_display_symbol(resolved)}"))
        return

    price = tick.bid
    current_side = "above" if price > target else "below"

    alert = db.add_price_alert(chat_id, resolved, target, direction)
    db.update_price_alert(alert.id, last_side=current_side)
    alert.last_side = current_side

    dir_str = direction if direction else "either direction"
    display = _display_symbol(resolved)
    await update.message.reply_text(
        f"✅ Price alert p{alert.user_seq}\n"
        f"{display.upper()} crossing {dir_str} {target}\n"
        f"Current bid: {_fmt_ohlc(price, resolved, None)}\n"
        f"/cancel p{alert.user_seq} to remove"
    )


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
        lines.append(f"{pat.label}")
        # OHLC
        lines.append(
            f"O {_fmt_ohlc(bar.open, symbol, sinfo)}  "
            f"H {_fmt_ohlc(bar.high, symbol, sinfo)}  "
            f"L {_fmt_ohlc(bar.low, symbol, sinfo)}  "
            f"C {_fmt_ohlc(bar.close, symbol, sinfo)}"
        )
        # Range + Body
        range_p = bar.high - bar.low
        body_p = abs(bar.close - bar.open)
        lines.append(f"Range {_fmt_ohlc(range_p, symbol, sinfo)}  Body {_fmt_ohlc(body_p, symbol, sinfo)}")

    if tick:
        spread = tick.spread
        lines.append(
            f"Bid {_fmt_ohlc(tick.bid, symbol, sinfo)}  "
            f"Ask {_fmt_ohlc(tick.ask, symbol, sinfo)}  "
            f"Spread {_fmt_spread(spread, sinfo)}"
        )

    return "\n".join(lines)


def _format_price_alert_message(
    alert: PriceAlert,
    price: float,
    tick: Tick,
    chat_id: int,
) -> str:
    dir_str = f"crossed {alert.direction}" if alert.direction else "crossed"
    display = _display_symbol(alert.symbol)
    return (
        f"🔔 <b>{display.upper()}</b> {dir_str} {alert.target}!\n"
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


# ---- app reference for scheduler callbacks ----
_app_ref: "Application | None" = None


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

    # Start scheduler in background
    async def post_init(app: Application):
        asyncio.create_task(
            scheduler.scheduler_loop(_send_candle, _send_price, _send_error),
            name="scheduler",
        )

    app.post_init = post_init

    return app
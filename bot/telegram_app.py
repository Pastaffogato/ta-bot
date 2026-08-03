"""Telegram command handlers.

All commands live here.  Formatting and argument parsing are in
bot.formatting and bot.parsing respectively.  App wiring is in bot.app.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import config, db, mt5_data, patterns, scheduler
from bot.models import CandleAlert, PriceAlert
from bot.mt5_data import Bar, Tick, SymbolInfo
from bot.timeframes import parse_tf, tf_label
from bot.formatting import (
    display_symbol as _display_symbol,
    err as _err,
    fmt_expiry as _fmt_expiry,
    fmt_ohlc as _fmt_ohlc,
    fmt_price as _fmt_price,
    format_candle_message as _format_candle_message,
    now_utc as _now_utc,
)
from bot.parsing import (
    _EXPIRY_RE,
    _REL_RE,
    parse_expiry as _parse_expiry,
    parse_mark_args as _parse_mark_args,
    resolve_price_args as _resolve_price_args,
    resolve_relative_price as _resolve_relative_price,
)

logger = logging.getLogger(__name__)


# ============================================================
# Per-user focus pair (session only, not persisted)
# ============================================================

_focus_pairs: dict[int, str] = {}


def _get_focus(chat_id: int) -> Optional[str]:
    return _focus_pairs.get(chat_id)


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
        "/level XAUUSD — yesterday OHLC\n"
        "/ind XAUUSD 5 — indicator snapshot\n"
        "/indtf XAUUSD sma50 — one indicator on M1/M3/M5/M15/M30/H1\n"
        "/indtf XAUUSD bb — BB bands, %b, width, Wpct per TF\n"
        "/trend XAUUSD 5 — trend classification\n\n"
        "<b>Price alerts:</b>\n"
        "/price XAUUSD 2400 — cross alert\n"
        "/price XAUUSD above 2400 — directional\n"
        "/price sma50 h1 above — indicator crossing (TF required)\n"
        "/price bb_lower h4 — indicator, either direction\n"
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
        "/entry XAUUSD buy tp sma50@h1 sl bb_lower@h4 — indicator TP/SL\n"
        "/modify t1 sl 2390 — move stop loss\n"
        "/modify t1 tp 2420 — move take profit\n"
        "/modify t1 close — close at market\n\n"
        "<b>Other:</b>\n"
        "/data — toggle OHLC + indicator sections\n"
        "/signals [on|off] — EA signal broadcast opt-in (bare = status)\n"
        "/clear — clear all alerts + marks\n"
        "/status — bot health\n"
        "/help — this text\n\n"
        "<b>Timeframes:</b> 3, 5, 15, m3, M5, h1, H4\n"
        "<b>Shorthand:</b> /a, /d, /l, /o, /n, /lv, /p, /c, /s, /e, /m, /mk, /dt, /tr, /itf, /indtf\n"
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
            if a.indicator:
                from bot.indicators import indicator_display_label
                label = indicator_display_label(a.indicator)
                ind_tf = tf_label(a.indicator_timeframe_min) if a.indicator_timeframe_min else "?"
                tgt = _fmt_price(a.target, a.symbol) if a.target else "—"
                lines.append(f"  p{a.user_seq} {display.upper()}{dir_str} {label} @ {ind_tf} ({tgt})")
            else:
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
    # Use the current running candle (position 0), not the previous completed bar
    bar = await mt5_data.current_bar(resolved, tf)
    prev = await mt5_data.previous_bar(resolved, tf)

    if bar is None:
        await update.message.reply_text(_err(f"No data for {_display_symbol(resolved)} {tf_label(tf)}"))
        return

    # Compute indicators on the current running candle
    ind_snap = None
    bars = None
    try:
        bars = await mt5_data.bars_n(resolved, tf, 500)
        if bars:
            from bot.indicators import compute_all
            ind_snap = compute_all(bars, skip_current=False)
    except Exception:
        pass

    text = _format_candle_message(resolved, tf, bar, prev, tick, sinfo, bar.time + tf * 60, time.time(), chat_id, ind_snap=ind_snap, bars=bars)
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
            "  /price 2400 30m (expires in 30 minutes)\n"
            "  /price sma50 h1 above (indicator crossing — TF required)\n"
            "  /price bb_lower h4 (indicator, either direction)"
        ))
        return

    from bot.indicators import INDICATOR_TARGETS, indicator_display_label, resolve_indicator_target, compute_all

    focus = _get_focus(chat_id)
    resolved = None
    price_args = args

    # Determine if first arg is a symbol (not a number, close, relative, expiry, or indicator)
    first = args[0]
    is_symbol = False
    try:
        float(first)
    except ValueError:
        if (
            first.lower() != "close"
            and not _REL_RE.match(first)
            and _parse_expiry(first) is None
            and first.lower() not in INDICATOR_TARGETS
        ):
            is_symbol = True

    if is_symbol:
        resolved = await mt5_data.resolve_symbol(first)
        if resolved is None:
            await update.message.reply_text(_err(f"Symbol not found: {first}"))
            return
        price_args = args[1:]
    elif focus:
        resolved = focus
    elif first.lower() in INDICATOR_TARGETS:
        await update.message.reply_text(_err("Usage: /price <symbol> <indicator> <TF> [above|below]\nOr set focus with /fp first, then /price sma50 h1 above"))
        return
    else:
        await update.message.reply_text(_err(
            "Usage: /price <symbol> <price> ...\n"
            "Or set focus with /fp first, then /price 2400"
        ))
        return

    if not price_args:
        await update.message.reply_text(_err("No price specified"))
        return

    # ── Indicator-based price alert ──
    # Syntax: /price [SYMBOL] <INDICATOR> <TF> [above|below] [30m|2h]
    # The indicator timeframe is REQUIRED and is independent of any candle-alert TF.
    if price_args[0].lower() in INDICATOR_TARGETS:
        ind_name = price_args[0].lower()

        # TF is mandatory and must come right after the indicator name.
        if len(price_args) < 2:
            await update.message.reply_text(_err(
                f"Indicator alert needs a timeframe.\n"
                f"Usage: /price {_display_symbol(resolved).upper() if resolved else '<symbol>'} {ind_name} <TF> [above|below] [30m|2h]\n"
                f"Example: /price {ind_name} h1 above"
            ))
            return
        ind_tf = parse_tf(price_args[1])
        if ind_tf is None:
            await update.message.reply_text(_err(
                f"Invalid indicator timeframe: {price_args[1]}\n"
                f"Use one of: m1, m5, m15, h1, h4, d1, ..."
            ))
            return

        direction = None
        expiry_s = None
        expiry_label = None
        for a in price_args[2:]:
            if a.lower() in ("above", "below"):
                direction = a.lower()
            else:
                exp = _parse_expiry(a)
                if exp is not None:
                    expiry_s, expiry_label = exp

        # Compute the current indicator value (on the requested TF) to show the user.
        # skip_current=True → use the last completed bar of that TF (stable).
        tick = await mt5_data.tick(resolved)
        if tick is None:
            await update.message.reply_text(_err(f"No tick data for {_display_symbol(resolved)}"))
            return

        try:
            bars = await mt5_data.bars_n(resolved, ind_tf, 500)
            snap = compute_all(bars, skip_current=True) if bars else None
        except Exception:
            snap = None

        current_val = resolve_indicator_target(snap, ind_name) if snap else None
        if current_val is None:
            await update.message.reply_text(_err(f"Indicator {ind_name} not available yet on {tf_label(ind_tf)} (need more bars)"))
            return

        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expiry_s)).isoformat() if expiry_s else None

        # Store with target=current_val (the resolved indicator value) and the indicator TF.
        alert = db.add_price_alert(
            chat_id, resolved, current_val, direction,
            alert_type="crossing", expires_at=expires_at,
            indicator=ind_name, indicator_timeframe_min=ind_tf,
        )
        # Set initial side based on the current indicator value vs the live price.
        current_side = "above" if tick.bid > current_val else "below"
        db.update_price_alert(alert.id, last_side=current_side)
        alert.last_side = current_side

        display = _display_symbol(resolved)
        label = indicator_display_label(ind_name)
        dir_str = f" {direction}" if direction else ""
        exp_str = f" {_fmt_expiry(expires_at)}" if expires_at else ""
        await update.message.reply_text(
            f"✅ Indicator alert p{alert.user_seq}{exp_str}\n"
            f"{display.upper()} cross{dir_str} {label} @ {tf_label(ind_tf)}\n"
            f"Current {label} ({tf_label(ind_tf)}): {_fmt_ohlc(current_val, resolved, None)}\n"
            f"Current bid: {_fmt_ohlc(tick.bid, resolved, None)}\n"
            f"/cancel p{alert.user_seq} to remove",
            parse_mode=ParseMode.HTML,
        )
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


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/signals [on|off] — EA signal broadcast opt-in (default: on). Bare command = status."""
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    arg = (context.args or [""])[0].lower()

    if arg == "on":
        db.set_user_pref(chat_id, "ea_signals", "on")
        await update.message.reply_text("signals: on")
    elif arg == "off":
        db.set_user_pref(chat_id, "ea_signals", "off")
        await update.message.reply_text("signals: off")
    else:
        # bare command (or unknown arg) = status; no recipient count
        state = "off" if db.get_user_prefs(chat_id).get("ea_signals") == "off" else "on"
        await update.message.reply_text(f"signals: {state}")


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
        elif a in ("tp", "sl") and i + 1 < len(trade_args):
            # Explicit tp/sl keyword: /entry buy tp sma50 sl bb_lower
            # or /entry buy tp 2420 sl 2390
            val = trade_args[i + 1]
            resolved_price = await _resolve_tp_sl_value(val, resolved, entry_price, pip_size)
            if resolved_price is None:
                hint = ""
                from bot.indicators import INDICATOR_TARGETS
                if val.lower() in INDICATOR_TARGETS:
                    hint = " — indicator TP/SL needs a timeframe, e.g. sma50@h1"
                await update.message.reply_text(_err(f"Invalid {a.upper()} value: {val}{hint}"))
                return
            if a == "tp":
                take_profit = resolved_price
            else:
                stop_loss = resolved_price
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
            new_sl = await _resolve_tp_sl_value(sl_val, trade.symbol, trade.entry_price, pip_size)
            if new_sl is None:
                await update.message.reply_text(_err(f"Invalid SL value: {sl_val}"))
                i += 2
                continue
            db.update_paper_trade(trade.id, stop_loss=new_sl)
            trade.stop_loss = new_sl
            await update.message.reply_text(f"✅ t{user_seq} SL → {_fmt_price(new_sl, trade.symbol)}")
            i += 2
        elif a == "tp" and i + 1 < len(mod_args):
            tp_val = mod_args[i + 1]
            new_tp = await _resolve_tp_sl_value(tp_val, trade.symbol, trade.entry_price, pip_size)
            if new_tp is None:
                await update.message.reply_text(_err(f"Invalid TP value: {tp_val}"))
                i += 2
                continue
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




async def _resolve_tp_sl_value(
    val: str,
    symbol: str,
    base_price: float,
    pip_size: float,
) -> Optional[float]:
    """Resolve a TP/SL value to an absolute price.

    Supports:
      - Indicator names with a required timeframe: "sma50@h1", "bb_lower@M15"
        → resolved from the indicator snapshot on that timeframe's bars.
        Bare indicator names (e.g. "sma50" without @TF) are rejected so the
        user is never silently handed an M1-based level.
      - Relative pips: "+20", "-10" → base_price ± pips
      - Absolute prices: "2420.50" → 2420.50

    Returns None if the value cannot be resolved.
    """
    from bot.indicators import INDICATOR_TARGETS, resolve_indicator_target, compute_all
    from bot.timeframes import parse_tf

    val_lower = val.lower()

    # Indicator@TF form (e.g. sma50@h1, bb_lower@m15)
    if "@" in val_lower:
        ind_name, tf_str = val_lower.split("@", 1)
        ind_name = ind_name.strip()
        if ind_name in INDICATOR_TARGETS:
            ind_tf = parse_tf(tf_str.strip())
            if ind_tf is None:
                return None
            try:
                bars = await mt5_data.bars_n(symbol, ind_tf, 500)
                if not bars:
                    return None
                snap = compute_all(bars, skip_current=True)
                return resolve_indicator_target(snap, ind_name)
            except Exception:
                return None

    # Bare indicator name without a timeframe — not allowed.
    if val_lower in INDICATOR_TARGETS:
        return None

    return _resolve_relative_price(val, base_price, pip_size)


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

    VALID_PREFS = {
        "show_pattern", "show_ohlc", "show_range_body", "show_bid_ask",
        "show_marks", "show_indicators", "show_trend", "show_progression",
        "show_sma", "show_ema", "show_bb", "show_atr", "show_rsi", "show_adx",
        "show_er", "show_chop",
    }

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


async def cmd_indicator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show indicator data for a symbol/timeframe. /ind <symbol> [tf]"""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        focus = _get_focus(chat_id)
        if not focus:
            await update.message.reply_text(_err("Usage: /ind <symbol> [tf]\nOr set focus with /fp first"))
            return
        symbol = focus
        tf_raw = "5"  # default M5
    elif len(args) == 1:
        # Could be symbol or symbol+tf
        resolved = await mt5_data.resolve_symbol(args[0])
        if resolved:
            symbol = resolved
            tf_raw = "5"
        elif focus := _get_focus(chat_id):
            symbol = focus
            tf_raw = args[0]
        else:
            await update.message.reply_text(_err(f"Symbol not found: {args[0]}"))
            return
    else:
        resolved = await mt5_data.resolve_symbol(args[0])
        if resolved:
            symbol = resolved
        elif focus := _get_focus(chat_id):
            symbol = focus
            tf_raw = args[0]
        else:
            await update.message.reply_text(_err(f"Symbol not found: {args[0]}"))
            return
        tf_raw = args[1] if len(args) > 1 else "5"

    tf_min = parse_tf(tf_raw)
    if tf_min is None:
        await update.message.reply_text(_err(f"Invalid timeframe: {tf_raw}"))
        return

    # Fetch tick + bars
    tick = await mt5_data.tick(symbol)
    if tick is None:
        await update.message.reply_text(_err("No tick data"))
        return

    sinfo = await mt5_data.symbol_info(symbol)

    bars = await mt5_data.bars_n(symbol, tf_min, 500)
    if not bars:
        await update.message.reply_text(_err("No bar data available"))
        return

    from bot.indicators import compute_all, format_indicator_full

    snap = compute_all(bars, skip_current=False)  # current running candle
    display = _display_symbol(symbol)
    header = f"{display.upper()} {tf_label(tf_min)}  Bid: {_fmt_ohlc(tick.bid, symbol, sinfo)}"
    report = format_indicator_full(snap, symbol, sinfo)

    await update.message.reply_text(f"{header}\n\n{report}")


# Registry for /indtf (multi-timeframe indicator snapshot).
# alias -> (IndicatorSnapshot attr, kind) where kind drives formatting:
#   price    -> _fmt_ohlc
#   ratio    -> 2 decimals
#   pct/idx/ratio100 -> 1 decimal
_IND_TF_REGISTRY: dict[str, tuple[str, str]] = {
    "sma50": ("sma50", "price"),
    "ema20": ("ema20", "price"),
    "bb": ("bb", "bb_full"),
    "bb_b": ("bb", "bb_full"),
    "bb_width": ("bb", "bb_full"),
    "bb_pctile": ("bb", "bb_full"),
    "rsi": ("rsi", "idx"),
    "adx": ("adx", "idx"),
    "tratr": ("tr_ratio", "ratio"),
    "tr_atr": ("tr_ratio", "ratio"),
    "atr": ("tr_ratio", "ratio"),
    "er": ("er14", "ratio100"),
    "er14": ("er14", "ratio100"),
    "chop": ("chop14", "idx"),
    "chop14": ("chop14", "idx"),
}

_DEFAULT_IND_TFS = [1, 3, 5, 15, 30, 60]
_IND_TF_SUMMARY = "bb (bands+%b+W+Wpct), sma50, ema20, rsi, adx, tratr (TR/ATR), er, chop"


async def cmd_indicator_tf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Multi-timeframe indicator snapshot. /indtf [SYMBOL] <indicator> [TF ...]"""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        await update.message.reply_text(_err(
            "Usage: /indtf [SYMBOL] <indicator> [TF ...]\n"
            "Indicators: " + _IND_TF_SUMMARY + "\n"
            "Example: /indtf XAUUSD rsi 5 15\n"
            "Or set focus with /fp first, then /indtf rsi 15"
        ))
        return

    # First arg may be a symbol — but ONLY if it's not a known indicator name.
    # resolve_symbol() can substring-match real symbols that collide with
    # indicator names (e.g. "bb" -> symbol "BB"), which would bypass the focus
    # pair. Indicator interpretation wins for /indtf.
    rest = args
    resolved = None
    if args[0].lower() not in _IND_TF_REGISTRY:
        resolved = await mt5_data.resolve_symbol(args[0])
    if resolved:
        symbol = resolved
        rest = args[1:]
    elif focus := _get_focus(chat_id):
        symbol = focus
    else:
        if args[0].lower() in _IND_TF_REGISTRY:
            await update.message.reply_text(_err(
                f"No focus pair set.\nUsage: /indtf [SYMBOL] {args[0].lower()} [TF ...]\n"
                f"Or set focus with /fp first, then /indtf {args[0].lower()}"
            ))
        else:
            await update.message.reply_text(_err(
                f"Symbol not found: {args[0]}\n"
                "Usage: /indtf [SYMBOL] <indicator> [TF ...]\n"
                "Or set focus with /fp first"
            ))
        return

    if not rest:
        await update.message.reply_text(_err(
            "Missing indicator.\nValid: " + _IND_TF_SUMMARY
        ))
        return

    ind_key = rest[0].lower()
    entry = _IND_TF_REGISTRY.get(ind_key)
    if entry is None:
        await update.message.reply_text(_err(
            f"Unknown indicator: {rest[0]}\nValid: " + _IND_TF_SUMMARY
        ))
        return
    attr, kind = entry

    # Trailing args that parse as timeframes override the default TF list.
    tfs = _DEFAULT_IND_TFS
    tf_args = rest[1:]
    if tf_args:
        parsed = []
        for a in tf_args:
            tf_min = parse_tf(a)
            if tf_min is None:
                await update.message.reply_text(_err(f"Invalid timeframe: {a}"))
                return
            parsed.append(tf_min)
        tfs = parsed

    from bot.indicators import compute_all

    sinfo = await mt5_data.symbol_info(symbol)
    lines = []
    for tf in tfs:
        bars = await mt5_data.bars_n(symbol, tf, 500)
        if not bars:
            lines.append(f"{tf_label(tf)} —")
            continue
        snap = compute_all(bars, skip_current=False)  # current running candle
        if kind == "bb_full":
            u, m, lo = snap.bb_upper, snap.bb_middle, snap.bb_lower
            if u is None or m is None or lo is None:
                lines.append(f"{tf_label(tf)} —")
                continue
            pip = sinfo.point * 10 if sinfo and sinfo.point > 0 else 0.01
            width_pips = (u - lo) / pip if pip > 0 else 0.0
            value = (f"U {_fmt_ohlc(u, symbol, sinfo)}  M {_fmt_ohlc(m, symbol, sinfo)}  "
                     f"L {_fmt_ohlc(lo, symbol, sinfo)}")
            if snap.bb_percent_b is not None:
                value += f"  %b {snap.bb_percent_b:.1f}"
            value += f"  W {width_pips:.1f}p"
            if snap.bb_width_pctile is not None:
                value += f"  Wpct {snap.bb_width_pctile:.0f}"
        else:
            val = getattr(snap, attr, None)
            if val is None:
                lines.append(f"{tf_label(tf)} —")
                continue
            if kind == "price":
                value = _fmt_ohlc(val, symbol, sinfo)
            elif kind == "ratio":
                value = f"{val:.2f}"
            else:  # pct, idx, ratio100
                value = f"{val:.1f}"
        lines.append(f"{tf_label(tf)} {value}")

    header = f"📊 {_display_symbol(symbol).upper()} · {ind_key.upper()} · current candle"
    await update.message.reply_text(f"{header}\n" + "\n".join(lines))


async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show trend classification. /trend [SYMBOL] [TF] [LOOKBACK]"""
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        # /trend with no args: if fp is active, default to M5, lookback 20
        focus = _get_focus(chat_id)
        if focus:
            symbol = focus
            tf_raw = "5"
            lookback = 20
        else:
            await update.message.reply_text(_err(
                "Usage: /trend [SYMBOL] [TIMEFRAME] [LOOKBACK]\n"
                "Example: /trend xauusd 5 10\n"
                "Or set focus with /fp first, then /trend 5"
            ))
            return
    else:
        # First arg could be a symbol or a timeframe
        resolved = await mt5_data.resolve_symbol(args[0])
        if resolved:
            symbol = resolved
            tf_raw = args[1] if len(args) > 1 else "5"
            lookback = int(args[2]) if len(args) > 2 else 20
        else:
            # Not a symbol — treat args[0] as a timeframe (needs focus pair)
            focus = _get_focus(chat_id)
            if not focus:
                await update.message.reply_text(_err(
                    f"Symbol '{args[0]}' not found.\n"
                    "Usage: /trend [SYMBOL] [TIMEFRAME] [LOOKBACK]\n"
                    "Or set focus with /fp first, then /trend 5"
                ))
                return
            symbol = focus
            tf_raw = args[0]
            lookback = int(args[1]) if len(args) > 1 else 20

    tf_min = parse_tf(tf_raw)
    if tf_min is None:
        await update.message.reply_text(_err(f"Unknown timeframe: {tf_raw}"))
        return

    # Fetch tick + bars
    sinfo = await mt5_data.symbol_info(symbol)
    bars = await mt5_data.bars_n(symbol, tf_min, 500)
    if not bars:
        await update.message.reply_text(_err("No bar data available"))
        return

    from bot.indicators import format_trend_full

    text = format_trend_full(bars, symbol, sinfo, lookback)
    await update.message.reply_text(text)


# ============================================================
# Shorthand wrappers
# ============================================================


async def cmd_mark_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shorthand: /mkd [id] — delete marks."""
    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    context.args = (["del"] + parts[1:]) if len(parts) > 1 else ["del"]
    await cmd_mark(update, context)


async def cmd_mark_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shorthand: /mkl [symbol] — list marks."""
    context.args = (["list"] + (context.args or []))
    await cmd_mark(update, context)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove all alerts and marks for this chat."""
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)

    n_candle = db.delete_all_candle_alerts(chat_id)
    n_price = db.delete_all_price_alerts(chat_id)
    n_mark = db.delete_all_marks(chat_id)

    if n_candle > 0 or n_price > 0:
        scheduler.subscriptions_changed.set()

    total = n_candle + n_price + n_mark
    if total > 0:
        parts = []
        if n_candle: parts.append(f"{n_candle} candle alerts")
        if n_price: parts.append(f"{n_price} price alerts")
        if n_mark: parts.append(f"{n_mark} marks")
        await update.message.reply_text(f"🧹 Cleared {', '.join(parts)}")
    else:
        await update.message.reply_text("Nothing to clear")
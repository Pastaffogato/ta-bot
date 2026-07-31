"""PTB Application wiring, scheduler callbacks, and dot-command dispatcher.

This module owns the PTB app lifecycle and the four scheduler callbacks
that bridge the scheduler to Telegram message delivery.
"""

import asyncio
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import config, scheduler
from bot.formatting import (
    display_symbol,
    format_candle_message,
    format_price_alert_message,
    fmt_price,
    now_utc,
)
from bot.mt5_data import Bar, Tick
from bot.timeframes import tf_label

logger = logging.getLogger(__name__)

# Globals set/used by build_app() and the scheduler callbacks
_app_ref: "Application | None" = None
_COMMANDS: dict[str, callable] = {}


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
    sinfo,
    close_epoch: float,
    sent_epoch: float,
) -> None:
    """Called by the scheduler to deliver a candle alert."""
    if symbol is None:
        text = (
            f"⏰ {tf_label(timeframe_min)} candle closing\n"
            f"Time: {now_utc()} UTC"
        )
    else:
        text = format_candle_message(
            symbol, timeframe_min, bar, prev_bar, tick, sinfo, close_epoch, sent_epoch, chat_id
        )
    if _app_ref:
        await _app_ref.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


async def _send_price(
    chat_id: int,
    alert,
    price: float,
    tick: Tick,
) -> None:
    """Called by the scheduler to deliver a price alert."""
    text = format_price_alert_message(alert, price, tick, chat_id)
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
    disp = display_symbol(trade.symbol)
    dir_str = trade.direction.upper()
    price_str = fmt_price(price, trade.symbol)

    if event == "activated":
        text = (
            f"✅ t{trade.user_seq} {disp.upper()} {dir_str} ACTIVATED @ {price_str}\n"
            f"SL: {fmt_price(trade.stop_loss, trade.symbol)} | TP: {fmt_price(trade.take_profit, trade.symbol)}"
        )
    elif event == "sl_hit":
        text = f"🛑 t{trade.user_seq} {disp.upper()} {dir_str} SL hit @ {price_str} | {pnl:+.1f}p"
    elif event == "tp_hit":
        text = f"🎯 t{trade.user_seq} {disp.upper()} {dir_str} TP hit @ {price_str} | {pnl:+.1f}p"
    else:
        return

    if _app_ref:
        try:
            await _app_ref.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ============================================================
# Dot-command dispatcher
# ============================================================

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


# ============================================================
# Application builder
# ============================================================

def build_app() -> Application:
    """Build and configure the PTB Application."""
    # Lazy import to avoid circular dependency
    from bot.telegram_app import (
        cmd_add, cmd_cancel, cmd_clear, cmd_data,
        cmd_del, cmd_entry, cmd_focus_pair, cmd_help,
        cmd_level, cmd_list, cmd_mark, cmd_mark_del, cmd_mark_list,
        cmd_modify, cmd_now, cmd_offset, cmd_price, cmd_status,
    )

    app = Application.builder().token(config.BOT_TOKEN).build()

    global _app_ref
    _app_ref = app

    # Register handlers
    handlers = [
        ("help", cmd_help),
        ("focus_pair", cmd_focus_pair), ("fp", cmd_focus_pair),
        ("add", cmd_add), ("a", cmd_add),
        ("del", cmd_del), ("d", cmd_del),
        ("list", cmd_list), ("l", cmd_list),
        ("offset", cmd_offset), ("o", cmd_offset),
        ("now", cmd_now), ("n", cmd_now),
        ("level", cmd_level), ("lv", cmd_level),
        ("price", cmd_price), ("p", cmd_price),
        ("cancel", cmd_cancel), ("c", cmd_cancel),
        ("status", cmd_status), ("s", cmd_status),
        ("data", cmd_data), ("dt", cmd_data),
        ("mark", cmd_mark), ("mk", cmd_mark),
        ("mkd", cmd_mark_del), ("mkl", cmd_mark_list),
        ("clear", cmd_clear),
        ("entry", cmd_entry), ("e", cmd_entry),
        ("modify", cmd_modify), ("m", cmd_modify),
    ]

    for name, func in handlers:
        app.add_handler(CommandHandler(name, func))
        _COMMANDS[name] = func

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
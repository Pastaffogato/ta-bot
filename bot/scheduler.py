"""Scheduler: candle-close and price-crossing alert loops.

Runs as a single background task. Groups candle alerts by (symbol, timeframe)
to minimize MT5 calls, then fans out to subscribed users. Price alerts poll
each symbol once per cycle.

Candle boundaries are calculated from MT5 tick time (UTC), not bar time
(which is broker server time and may differ from UTC).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from bot import config, db, mt5_data
from bot.models import CandleAlert, PriceAlert
from bot.timeframes import tf_label

logger = logging.getLogger(__name__)

# In-process event: set when subscriptions change so the scheduler re-reads
subscriptions_changed = asyncio.Event()


async def scheduler_loop(
    send_candle: callable,
    send_price: callable,
    send_error: callable,
) -> None:
    """Run candle and price schedulers as independent tasks."""
    logger.info("Scheduler started")
    await asyncio.gather(
        _candle_loop(send_candle, send_error),
        _price_loop(send_price),
    )


async def _candle_loop(
    send_candle: callable,
    send_error: callable,
) -> None:
    """Candle scheduling loop — sleeps until next boundary, then fans out."""
    while True:
        try:
            alerts = db.get_candle_alerts()
            if alerts:
                await _process_candle_alerts(alerts, send_candle, send_error)

            if subscriptions_changed.is_set():
                subscriptions_changed.clear()
                logger.debug("Subscriptions changed, re-reading")
                continue

            await asyncio.sleep(0.25)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Candle scheduler error")
            await asyncio.sleep(5)


async def _price_loop(
    send_price: callable,
) -> None:
    """Price polling loop — checks every PRICE_POLL_INTERVAL_S seconds."""
    last_check: dict[str, float] = {}
    while True:
        try:
            price_alerts = db.get_price_alerts()
            if price_alerts:
                await _process_price_alerts(price_alerts, send_price, last_check)

            await asyncio.sleep(config.PRICE_POLL_INTERVAL_S)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Price scheduler error")
            await asyncio.sleep(5)


async def _process_candle_alerts(
    alerts: list[CandleAlert],
    send_candle: callable,
    send_error: callable,
) -> None:
    """Group alerts by (symbol, timeframe, effective_offset), find the nearest
    due time across all groups, sleep once, then process all due groups."""
    now = time.time()

    # Build groups: key=(symbol, tf, offset) → (symbol, tf, offset, [alerts])
    groups: dict[tuple, tuple[Optional[str], int, int, list[CandleAlert]]] = {}
    for alert in alerts:
        user = db.get_user(alert.chat_id)
        if user is None:
            continue
        offset = alert.offset_s if alert.offset_s is not None else user.default_offset_s
        key = (alert.symbol, alert.timeframe_min, offset)
        if key not in groups:
            groups[key] = (alert.symbol, alert.timeframe_min, offset, [])
        groups[key][3].append(alert)

    # Determine close_epoch for each group using wall-clock UTC (time.time())
    # MT5 bar/tick times may be in broker server timezone — unreliable for scheduling.
    # We use time.time() for all boundary calculations, MT5 only for OHLC data.
    group_info: list[dict] = []
    for (symbol, tf_min, offset), (_, _, _, group_alerts) in groups.items():
        interval_s = tf_min * 60
        close_epoch = _next_boundary(now, interval_s)
        candle_open_epoch = close_epoch - interval_s

        group_info.append({
            "symbol": symbol,
            "tf_min": tf_min,
            "offset": offset,
            "close_epoch": close_epoch,
            "candle_open_epoch": candle_open_epoch,
            "alerts": group_alerts,
        })

    if not group_info:
        return

    # Find the nearest due time across all groups
    nearest_due = None
    for g in group_info:
        due = g["close_epoch"] - g["offset"]
        if nearest_due is None or due < nearest_due:
            nearest_due = due

    if nearest_due is None:
        return

    # Sleep until the nearest due time (minus a small buffer).
    # Cap at 5s so subscription changes are picked up quickly.
    sleep_s = nearest_due - time.time()
    if sleep_s > 0.5:
        await asyncio.sleep(min(sleep_s, 5.0))
        if subscriptions_changed.is_set():
            return

    # Process all groups that are now due (within tolerance)
    now2 = time.time()
    for g in group_info:
        due = g["close_epoch"] - g["offset"]
        if now2 < due - config.LATE_SEND_TOLERANCE_S:
            continue  # not due yet
        if now2 > due + config.LATE_SEND_TOLERANCE_S:
            logger.debug("Skipping %s %s — late", g["symbol"] or "timer", tf_label(g["tf_min"]))
            continue

        await _send_group(g, send_candle, send_error)


async def _send_group(
    g: dict,
    send_candle: callable,
    send_error: callable,
) -> None:
    """Fetch data for one group and fan out to all subscribed users."""
    symbol = g["symbol"]
    tf_min = g["tf_min"]
    close_epoch = g["close_epoch"]
    candle_open_epoch = g["candle_open_epoch"]
    offset = g["offset"]
    interval_s = tf_min * 60

    candle_open_utc = datetime.fromtimestamp(candle_open_epoch, tz=timezone.utc).isoformat()
    sent_epoch = time.time()

    if symbol is not None:
        bar = await mt5_data.current_bar(symbol, tf_min)        # position 0
        prev_bar = await mt5_data.previous_bar(symbol, tf_min)  # position 1

        if offset == 0:
            # We want the candle that just closed at close_epoch.
            # bar.time is MT5 server time, candle_open_epoch is UTC.
            # Use tick time to compute the server→UTC offset, then compare
            # in server time.
            tick = await mt5_data.tick(symbol)
            server_offset = round(tick.time - time.time()) if tick else 0
            expected_open_server = int(candle_open_epoch) + server_offset
            if bar is None or abs(bar.time - expected_open_server) > 2:
                # New bar already exists (or no bar) — use previous_bar
                bar = prev_bar
                prev_bar = await mt5_data.bar_at_offset(symbol, tf_min, 2)
        else:
            tick = await mt5_data.tick(symbol)

        if tick is None:
            tick = await mt5_data.tick(symbol)  # retry once if needed

        sinfo = await mt5_data.symbol_info(symbol)
    else:
        bar = None
        prev_bar = None
        tick = None
        sinfo = None

    for alert in g["alerts"]:
        alert_key = f"{symbol or 'timer'}:{tf_min}"

        if not db.record_delivery(alert.chat_id, alert_key, candle_open_utc):
            logger.debug("Already sent %s to chat %d", alert_key, alert.chat_id)
            continue

        try:
            await send_candle(
                chat_id=alert.chat_id,
                symbol=symbol,
                timeframe_min=tf_min,
                bar=bar,
                prev_bar=prev_bar,
                tick=tick,
                sinfo=sinfo,
                close_epoch=close_epoch,
                sent_epoch=sent_epoch,
            )
        except Exception:
            logger.exception("Failed to send candle alert to chat %d", alert.chat_id)
            await send_error(alert.chat_id, "Failed to send candle alert")


async def _process_price_alerts(
    alerts: list[PriceAlert],
    send_price: callable,
    last_check: dict[str, float],
) -> None:
    """Poll each unique symbol once, evaluate all thresholds."""
    symbols = {a.symbol for a in alerts}
    now = time.time()

    for symbol in symbols:
        if symbol in last_check and now - last_check[symbol] < config.PRICE_POLL_INTERVAL_S:
            continue
        last_check[symbol] = now

        tick = await mt5_data.tick(symbol)
        if tick is None:
            continue

        price = tick.bid

        for alert in alerts:
            if alert.symbol != symbol:
                continue

            current_side = "above" if price > alert.target else "below"

            if alert.last_side is None:
                db.update_price_alert(alert.id, last_side=current_side)
                alert.last_side = current_side
                continue

            if alert.last_side != current_side:
                triggered = False
                if alert.direction is None:
                    triggered = True
                elif alert.direction == "above" and current_side == "above":
                    triggered = True
                elif alert.direction == "below" and current_side == "below":
                    triggered = True

                if triggered:
                    try:
                        await send_price(
                            chat_id=alert.chat_id,
                            alert=alert,
                            price=price,
                            tick=tick,
                        )
                    except Exception:
                        logger.exception("Failed to send price alert to chat %d", alert.chat_id)

                    if not alert.repeat:
                        db.update_price_alert(alert.id, enabled=False)
                        alert.enabled = False

                db.update_price_alert(alert.id, last_side=current_side)
                alert.last_side = current_side


def _next_boundary(now: float, interval_s: int) -> float:
    """Next boundary time for a fixed-interval schedule (UTC epoch)."""
    return ((now // interval_s) + 1) * interval_s
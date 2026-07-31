"""Read-only adapter for a local MetaTrader 5 terminal.

All MetaTrader5 calls run through asyncio.to_thread with a lock, so the
Telegram event loop never blocks and only one MT5 call is in flight at a time.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


@dataclass
class Tick:
    symbol: str
    bid: float
    ask: float
    time: float  # MT5 epoch seconds (UTC)
    volume: float = 0.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def age_s(self) -> float:
        return time.time() - self.time


@dataclass
class Bar:
    symbol: str
    timeframe_min: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    time: int  # bar open time, MT5 epoch seconds (UTC)


@dataclass
class SymbolInfo:
    name: str
    digits: int
    point: float
    trade_mode: int  # 0=disabled, 1=long only, 2=short only, 3=close only, 4=full
    description: str = ""


async def _call_mt5(fn, *args, **kwargs):
    """Run a blocking MT5 call in a thread, with serialization lock."""
    async with _lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


async def init(path: Optional[str] = None) -> bool:
    """Initialize MT5 connection. Returns True on success."""
    kwargs = {}
    if path:
        kwargs["path"] = path
    ok = await _call_mt5(mt5.initialize, **kwargs)
    if not ok:
        err = await _call_mt5(mt5.last_error)
        logger.error("MT5 init failed: %s", err)
        return False
    logger.info("MT5 initialized (version %s)", await _call_mt5(mt5.version))
    return True


async def shutdown() -> None:
    await _call_mt5(mt5.shutdown)
    logger.info("MT5 shutdown")


async def health() -> dict:
    """Return connection + account summary for /status."""
    try:
        info = await _call_mt5(mt5.terminal_info)
        acc = await _call_mt5(mt5.account_info)
        if info is None or acc is None:
            return {"connected": False, "error": "terminal_info or account_info returned None"}
        return {
            "connected": info.connected,
            "community_account": info.community_account,
            "build": info.build,
            "account": acc.login,
            "server": acc.server,
            "balance": acc.balance,
            "equity": acc.equity,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}

async def resolve_symbol(hint: str) -> Optional[str]:
    """Resolve a symbol name. Checks pairs.yaml mapping first, then exact match,
    then auto-resolves if exactly one symbol contains the hint (case-insensitive).
    Returns None if ambiguous or not found."""
    from bot.config import PAIRS

    hint_lower = hint.lower().strip()

    # 1. Check pairs.yaml mapping
    if hint_lower in PAIRS:
        mapped = PAIRS[hint_lower]
        # Verify the mapped symbol actually exists in MT5
        all_symbols = await _call_mt5(mt5.symbols_get)
        if all_symbols:
            for s in all_symbols:
                if s.name.upper() == mapped.upper():
                    return s.name
        # Mapped symbol not found — fall through to normal resolution

    hint_upper = hint.upper()
    all_symbols = await _call_mt5(mt5.symbols_get)
    if all_symbols is None:
        return None

    # 2. Exact match
    for s in all_symbols:
        if s.name.upper() == hint_upper:
            return s.name

    # 3. Substring match — only if exactly one result
    matches = [s.name for s in all_symbols if hint_upper in s.name.upper()]
    if len(matches) == 1:
        return matches[0]

    return None


async def suggest_symbols(hint: str) -> list[str]:
    """Return symbols whose name contains hint (case-insensitive)."""
    hint_upper = hint.upper()
    all_symbols = await _call_mt5(mt5.symbols_get)
    if all_symbols is None:
        return []
    return sorted(
        [s.name for s in all_symbols if hint_upper in s.name.upper()],
        key=lambda n: (n.upper() != hint_upper, len(n)),  # exact match first, then shortest
    )


async def symbol_info(symbol: str) -> Optional[SymbolInfo]:
    info = await _call_mt5(mt5.symbol_info, symbol)
    if info is None:
        return None
    return SymbolInfo(
        name=info.name,
        digits=info.digits,
        point=info.point,
        trade_mode=info.trade_mode,
        description=info.description or "",
    )


async def tick(symbol: str) -> Optional[Tick]:
    info = await _call_mt5(mt5.symbol_info_tick, symbol)
    if info is None:
        return None
    return Tick(
        symbol=symbol,
        bid=info.bid,
        ask=info.ask,
        time=info.time,  # MT5 epoch seconds (may be server time, not UTC)
    )


async def current_bar(symbol: str, timeframe_min: int) -> Optional[Bar]:
    """Get the current (incomplete) bar for symbol + timeframe."""
    tf = _mt5_timeframe(timeframe_min)
    if tf is None:
        return None
    bars = await _call_mt5(mt5.copy_rates_from_pos, symbol, tf, 0, 1)
    if bars is None or len(bars) == 0:
        return None
    return _bar_from_row(bars[0], symbol, timeframe_min)


async def previous_bar(symbol: str, timeframe_min: int) -> Optional[Bar]:
    """Get the immediately previous (completed) bar (bar index 1)."""
    return await bar_at_offset(symbol, timeframe_min, 1)


async def bar_at_offset(symbol: str, timeframe_min: int, offset: int) -> Optional[Bar]:
    """Get a bar at a given offset from current (0=current, 1=previous, 2=prev-prev)."""
    tf = _mt5_timeframe(timeframe_min)
    if tf is None:
        return None
    bars = await _call_mt5(mt5.copy_rates_from_pos, symbol, tf, offset, 1)
    if bars is None or len(bars) == 0:
        return None
    return _bar_from_row(bars[0], symbol, timeframe_min)


async def previous_day_bar(symbol: str) -> Optional[Bar]:
    """Get yesterday's completed daily bar."""
    tf = mt5.TIMEFRAME_D1
    bars = await _call_mt5(mt5.copy_rates_from_pos, symbol, tf, 1, 1)
    if bars is None or len(bars) == 0:
        return None
    return _bar_from_row(bars[0], symbol, 1440)


async def today_open_bar(symbol: str) -> Optional[Bar]:
    """Get today's daily bar (incomplete)."""
    tf = mt5.TIMEFRAME_D1
    bars = await _call_mt5(mt5.copy_rates_from_pos, symbol, tf, 0, 1)
    if bars is None or len(bars) == 0:
        return None
    return _bar_from_row(bars[0], symbol, 1440)


async def bars_n(symbol: str, timeframe_min: int, count: int) -> list[Bar]:
    """Fetch the last N bars (0=current, 1=prev, ..., N-1=oldest).

    Returns bars newest-first. An empty list means no data available.
    """
    tf = _mt5_timeframe(timeframe_min)
    if tf is None:
        return []
    bars = await _call_mt5(mt5.copy_rates_from_pos, symbol, tf, 0, count)
    if bars is None or len(bars) == 0:
        return []
    return [_bar_from_row(b, symbol, timeframe_min) for b in bars]


# ---- internal helpers ----

def _bar_from_row(b, symbol: str, timeframe_min: int) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe_min=timeframe_min,
        open=b["open"],
        high=b["high"],
        low=b["low"],
        close=b["close"],
        tick_volume=int(b["tick_volume"]),
        time=int(b["time"]),
    )


def _mt5_timeframe(minutes: int) -> Optional[int]:
    """Map minutes to MT5 timeframe constant."""
    mapping = {
        1: mt5.TIMEFRAME_M1,
        2: mt5.TIMEFRAME_M2,
        3: mt5.TIMEFRAME_M3,
        4: mt5.TIMEFRAME_M4,
        5: mt5.TIMEFRAME_M5,
        6: mt5.TIMEFRAME_M6,
        10: mt5.TIMEFRAME_M10,
        12: mt5.TIMEFRAME_M12,
        15: mt5.TIMEFRAME_M15,
        20: mt5.TIMEFRAME_M20,
        30: mt5.TIMEFRAME_M30,
        60: mt5.TIMEFRAME_H1,
        120: mt5.TIMEFRAME_H2,
        240: mt5.TIMEFRAME_H4,
        180: mt5.TIMEFRAME_H3,
        360: mt5.TIMEFRAME_H6,
        480: mt5.TIMEFRAME_H8,
        720: mt5.TIMEFRAME_H12,
        1440: mt5.TIMEFRAME_D1,
        10080: mt5.TIMEFRAME_W1,
        43200: mt5.TIMEFRAME_MN1,
    }
    return mapping.get(minutes, None)
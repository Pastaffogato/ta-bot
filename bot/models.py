from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    chat_id: int
    timezone: str = "Etc/GMT-8"
    default_offset_s: int = 0
    created_at: str = ""


@dataclass
class CandleAlert:
    id: int = 0
    chat_id: int = 0
    user_seq: int = 0  # per-user sequence number (1, 2, 3…), reuses gaps
    symbol: Optional[str] = None
    timeframe_min: int = 0
    offset_s: Optional[int] = None
    enabled: bool = True


@dataclass
class PriceAlert:
    id: int = 0
    chat_id: int = 0
    user_seq: int = 0  # per-user sequence number (1, 2, 3…)
    symbol: str = ""
    direction: Optional[str] = None
    target: float = 0.0
    target_upper: Optional[float] = None  # close-range upper bound
    alert_type: str = "crossing"  # "crossing" or "close"
    price_source: str = "bid"
    repeat: bool = False
    enabled: bool = True
    last_side: Optional[str] = None
    expires_at: Optional[str] = None  # ISO timestamp, None = indefinite
    indicator: Optional[str] = None  # None = static price; "sma50","ema20","bb_upper","bb_lower","bb_middle" = dynamic
    indicator_timeframe_min: Optional[int] = None  # TF used to compute the indicator value (independent of any candle-alert TF)
    created_at: str = ""


@dataclass
class CandleDelivery:
    chat_id: int = 0
    alert_key: str = ""  # e.g. "XAUUSD:5" or "timer:5"
    candle_open_utc: str = ""
    sent_at: str = ""


@dataclass
class Mark:
    id: int = 0
    chat_id: int = 0
    user_seq: int = 0  # per-user sequence number (1, 2, 3…)
    symbol: str = ""
    price: float = 0.0
    created_at: str = ""
    expires_at: Optional[str] = None  # ISO timestamp, None = GTC
    label: str = ""  # optional user note


@dataclass
class PaperTrade:
    id: int = 0
    chat_id: int = 0
    user_seq: int = 0  # per-user sequence number (1, 2, 3…)
    symbol: str = ""
    direction: str = ""  # "buy" or "sell"
    order_type: str = "market"  # "market", "limit", "stop"
    entry_price: float = 0.0
    position_size: float = 1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: str = "open"  # "open" or "closed"
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    opened_at: str = ""
    closed_at: Optional[str] = None
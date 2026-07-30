from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    chat_id: int
    timezone: str = "Etc/GMT-8"
    default_offset_s: int = 8
    created_at: str = ""


@dataclass
class CandleAlert:
    id: int = 0
    chat_id: int = 0
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
    price_source: str = "bid"
    repeat: bool = False
    enabled: bool = True
    last_side: Optional[str] = None
    created_at: str = ""


@dataclass
class CandleDelivery:
    chat_id: int = 0
    alert_key: str = ""  # e.g. "XAUUSD:5" or "timer:5"
    candle_open_utc: str = ""
    sent_at: str = ""
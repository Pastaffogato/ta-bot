import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from bot.config import DB_PATH
from bot.models import CandleAlert, CandleDelivery, Mark, PriceAlert, User

_conn_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_conn_local, "conn"):
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.row_factory = sqlite3.Row
        _conn_local.conn = c
    return _conn_local.conn


@contextmanager
def _tx():
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id    INTEGER PRIMARY KEY,
            timezone   TEXT NOT NULL DEFAULT 'Etc/GMT-8',
            default_offset_s INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS candle_alerts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER NOT NULL REFERENCES users(chat_id),
            symbol        TEXT,
            timeframe_min INTEGER NOT NULL,
            offset_s      INTEGER,
            enabled       INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS price_alerts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER NOT NULL REFERENCES users(chat_id),
            user_seq      INTEGER NOT NULL DEFAULT 0,
            symbol        TEXT NOT NULL,
            direction     TEXT,
            target        REAL NOT NULL,
            price_source  TEXT NOT NULL DEFAULT 'bid',
            repeat        INTEGER NOT NULL DEFAULT 0,
            enabled       INTEGER NOT NULL DEFAULT 1,
            last_side     TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            chat_id        INTEGER NOT NULL REFERENCES users(chat_id),
            alert_key      TEXT NOT NULL,
            candle_open_utc TEXT NOT NULL,
            sent_at        TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, alert_key, candle_open_utc)
        );

        CREATE TABLE IF NOT EXISTS user_prefs (
            chat_id INTEGER NOT NULL REFERENCES users(chat_id),
            key     TEXT NOT NULL,
            value   TEXT NOT NULL DEFAULT 'on',
            PRIMARY KEY (chat_id, key)
        );

        CREATE TABLE IF NOT EXISTS marks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL REFERENCES users(chat_id),
            user_seq   INTEGER NOT NULL DEFAULT 0,
            symbol     TEXT NOT NULL,
            price      REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT,
            label      TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()

    # Migration: add user_seq column if missing + backfill
    cols = [r[1] for r in conn.execute("PRAGMA table_info(price_alerts)").fetchall()]
    if "user_seq" not in cols:
        conn.execute("ALTER TABLE price_alerts ADD COLUMN user_seq INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    # Backfill any rows with user_seq=0
    rows = conn.execute(
        "SELECT id, chat_id FROM price_alerts WHERE user_seq = 0 ORDER BY id"
    ).fetchall()
    from collections import defaultdict
    counters: dict[int, int] = defaultdict(int)
    for row in rows:
        counters[row["chat_id"]] += 1
        conn.execute(
            "UPDATE price_alerts SET user_seq = ? WHERE id = ?",
            (counters[row["chat_id"]], row["id"]),
        )
    if rows:
        conn.commit()

    # Migration: add user_seq to marks + backfill
    cols = [r[1] for r in conn.execute("PRAGMA table_info(marks)").fetchall()]
    if "user_seq" not in cols:
        conn.execute("ALTER TABLE marks ADD COLUMN user_seq INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    rows = conn.execute(
        "SELECT id, chat_id FROM marks WHERE user_seq = 0 ORDER BY id"
    ).fetchall()
    counters2: dict[int, int] = defaultdict(int)
    for row in rows:
        counters2[row["chat_id"]] += 1
        conn.execute(
            "UPDATE marks SET user_seq = ? WHERE id = ?",
            (counters2[row["chat_id"]], row["id"]),
        )
    if rows:
        conn.commit()


# ---- user helpers ----

def ensure_user(chat_id: int) -> User:
    with _tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id) VALUES (?)",
            (chat_id,),
        )
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return _row_to_user(row)


def update_user(chat_id: int, **kwargs) -> None:
    if not kwargs:
        return
    columns = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [chat_id]
    with _tx() as conn:
        conn.execute(f"UPDATE users SET {columns} WHERE chat_id = ?", values)


def get_user(chat_id: int) -> Optional[User]:
    row = _conn().execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return _row_to_user(row) if row else None


# ---- candle alerts ----

def add_candle_alert(chat_id: int, symbol: Optional[str], timeframe_min: int, offset_s: Optional[int] = None) -> CandleAlert:
    if symbol:
        # check uniqueness: one symbol alert per user/symbol/timeframe
        existing = _conn().execute(
            "SELECT id FROM candle_alerts WHERE chat_id=? AND symbol=? AND timeframe_min=?",
            (chat_id, symbol, timeframe_min),
        ).fetchone()
        if existing:
            raise ValueError(f"Candle alert for {symbol} M{timeframe_min} already exists")
    else:
        # timer-only: one per user/timeframe
        existing = _conn().execute(
            "SELECT id FROM candle_alerts WHERE chat_id=? AND symbol IS NULL AND timeframe_min=?",
            (chat_id, timeframe_min),
        ).fetchone()
        if existing:
            raise ValueError(f"Timer-only M{timeframe_min} alert already exists")

    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO candle_alerts (chat_id, symbol, timeframe_min, offset_s) VALUES (?, ?, ?, ?)",
            (chat_id, symbol, timeframe_min, offset_s),
        )
        row = conn.execute("SELECT * FROM candle_alerts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_candle(row)


def get_candle_alerts(chat_id: Optional[int] = None) -> list[CandleAlert]:
    if chat_id is not None:
        rows = _conn().execute(
            "SELECT * FROM candle_alerts WHERE chat_id=? AND enabled=1 ORDER BY id",
            (chat_id,),
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM candle_alerts WHERE enabled=1 ORDER BY id"
        ).fetchall()
    return [_row_to_candle(r) for r in rows]


def delete_candle_alert(alert_id: int) -> bool:
    with _tx() as conn:
        cur = conn.execute("DELETE FROM candle_alerts WHERE id = ?", (alert_id,))
        return cur.rowcount > 0


def delete_candle_alerts_by(chat_id: int, symbol: Optional[str] = None, timeframe_min: Optional[int] = None) -> int:
    """Delete candle alerts for a user, optionally filtered. Returns count deleted."""
    query = "DELETE FROM candle_alerts WHERE chat_id = ?"
    params: list = [chat_id]
    if symbol is not None:
        query += " AND symbol = ?"
        params.append(symbol)
    if timeframe_min is not None:
        query += " AND timeframe_min = ?"
        params.append(timeframe_min)
    with _tx() as conn:
        cur = conn.execute(query, params)
        return cur.rowcount


# ---- price alerts ----

def add_price_alert(
    chat_id: int,
    symbol: str,
    target: float,
    direction: Optional[str] = None,
) -> PriceAlert:
    with _tx() as conn:
        # Find smallest unused user_seq for this chat_id
        rows = conn.execute(
            "SELECT user_seq FROM price_alerts WHERE chat_id = ? AND enabled = 1 ORDER BY user_seq",
            (chat_id,),
        ).fetchall()
        used = {r[0] for r in rows}
        next_seq = 1
        while next_seq in used:
            next_seq += 1
        cur = conn.execute(
            "INSERT INTO price_alerts (chat_id, user_seq, symbol, direction, target) VALUES (?, ?, ?, ?, ?)",
            (chat_id, next_seq, symbol, direction, target),
        )
        row = conn.execute("SELECT * FROM price_alerts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_price(row)


def get_price_alerts(chat_id: Optional[int] = None) -> list[PriceAlert]:
    if chat_id is not None:
        rows = _conn().execute(
            "SELECT * FROM price_alerts WHERE chat_id=? AND enabled=1 ORDER BY id",
            (chat_id,),
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM price_alerts WHERE enabled=1 ORDER BY id"
        ).fetchall()
    return [_row_to_price(r) for r in rows]


def update_price_alert(alert_id: int, **kwargs) -> None:
    if not kwargs:
        return
    columns = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [alert_id]
    with _tx() as conn:
        conn.execute(f"UPDATE price_alerts SET {columns} WHERE id = ?", values)


def delete_price_alert(alert_id: int) -> bool:
    with _tx() as conn:
        cur = conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
        return cur.rowcount > 0


def get_price_alert_by_user_seq(chat_id: int, user_seq: int) -> Optional[PriceAlert]:
    row = _conn().execute(
        "SELECT * FROM price_alerts WHERE chat_id = ? AND user_seq = ? AND enabled = 1",
        (chat_id, user_seq),
    ).fetchone()
    return _row_to_price(row) if row else None


# ---- deliveries (deduplication) ----

def record_delivery(chat_id: int, alert_key: str, candle_open_utc: str) -> bool:
    """Record a candle delivery. Returns True if it was new, False if already sent."""
    try:
        with _tx() as conn:
            conn.execute(
                "INSERT INTO deliveries (chat_id, alert_key, candle_open_utc) VALUES (?, ?, ?)",
                (chat_id, alert_key, candle_open_utc),
            )
        return True
    except sqlite3.IntegrityError:
        return False


# ---- user preferences ----

def get_user_prefs(chat_id: int) -> dict[str, str]:
    rows = _conn().execute(
        "SELECT key, value FROM user_prefs WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_user_pref(chat_id: int, key: str, value: str) -> None:
    with _tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_prefs (chat_id, key, value) VALUES (?, ?, ?)",
            (chat_id, key, value),
        )


# ---- marks ----

def add_mark(chat_id: int, symbol: str, price: float, expires_at: Optional[str] = None) -> Mark:
    with _tx() as conn:
        # Find smallest unused user_seq for this chat_id
        used = {
            r[0] for r in conn.execute(
                "SELECT user_seq FROM marks WHERE chat_id = ? AND user_seq > 0"
                " AND (expires_at IS NULL OR expires_at > datetime('now'))",
                (chat_id,),
            ).fetchall()
        }
        user_seq = 1
        while user_seq in used:
            user_seq += 1
        cur = conn.execute(
            "INSERT INTO marks (chat_id, user_seq, symbol, price, expires_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_seq, symbol, price, expires_at),
        )
        row = conn.execute("SELECT * FROM marks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_mark(row)


def get_marks(chat_id: int, symbol: Optional[str] = None) -> list[Mark]:
    if symbol:
        rows = _conn().execute(
            "SELECT * FROM marks WHERE chat_id = ? AND symbol = ?"
            " AND (expires_at IS NULL OR expires_at > datetime('now'))"
            " ORDER BY user_seq",
            (chat_id, symbol),
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM marks WHERE chat_id = ?"
            " AND (expires_at IS NULL OR expires_at > datetime('now'))"
            " ORDER BY user_seq",
            (chat_id,),
        ).fetchall()
    return [_row_to_mark(r) for r in rows]


def delete_mark(chat_id: int, user_seq: int) -> bool:
    with _tx() as conn:
        cur = conn.execute(
            "DELETE FROM marks WHERE chat_id = ? AND user_seq = ?",
            (chat_id, user_seq),
        )
        return cur.rowcount > 0


def delete_all_marks(chat_id: int) -> int:
    with _tx() as conn:
        cur = conn.execute("DELETE FROM marks WHERE chat_id = ?", (chat_id,))
        return cur.rowcount


def get_mark_by_user_seq(chat_id: int, user_seq: int) -> Optional[Mark]:
    row = _conn().execute(
        "SELECT * FROM marks WHERE chat_id = ? AND user_seq = ?"
        " AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (chat_id, user_seq),
    ).fetchone()
    return _row_to_mark(row) if row else None


# ---- row converters ----

def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        chat_id=row["chat_id"],
        timezone=row["timezone"],
        default_offset_s=row["default_offset_s"],
        created_at=row["created_at"],
    )


def _row_to_candle(row: sqlite3.Row) -> CandleAlert:
    return CandleAlert(
        id=row["id"],
        chat_id=row["chat_id"],
        symbol=row["symbol"],
        timeframe_min=row["timeframe_min"],
        offset_s=row["offset_s"],
        enabled=bool(row["enabled"]),
    )


def _row_to_price(row: sqlite3.Row) -> PriceAlert:
    return PriceAlert(
        id=row["id"],
        chat_id=row["chat_id"],
        user_seq=row["user_seq"],
        symbol=row["symbol"],
        direction=row["direction"],
        target=row["target"],
        price_source=row["price_source"],
        repeat=bool(row["repeat"]),
        enabled=bool(row["enabled"]),
        last_side=row["last_side"],
        created_at=row["created_at"],
    )


def _row_to_mark(row: sqlite3.Row) -> Mark:
    return Mark(
        id=row["id"],
        chat_id=row["chat_id"],
        user_seq=row["user_seq"],
        symbol=row["symbol"],
        price=row["price"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        label=row["label"],
    )
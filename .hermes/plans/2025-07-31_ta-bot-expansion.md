# ta-bot Feature Expansion — Implementation Plan

> **Goal:** Add 10 features (engulf fix → trend classification) to the Telegram trading alert bot, in dependency order, without breaking existing functionality.

**Architecture:** Same stack — `python-telegram-bot` v20+, `MetaTrader5`, SQLite/WAL, wall-clock UTC scheduling. New `bot/indicators.py` and `bot/parser.py` modules. New DB tables for marks, paper entries, user prefs. MessageHandler for dot-prefix commands. Flexible argument parser for pip-based inputs.

**Tech Stack:** Python 3.11+, python-telegram-bot[job-queue]>=20.0, MetaTrader5>=5.0.45, numpy>=2.2, pyyaml>=6.0

---

## Feature Priority & Dependencies

```
1. Fix bear engulf      (independent) ─┐
2. MessageHandler       (independent)  │
3. TW/BW in alerts      (independent)  │── Phase 1 (no new deps)
4. Modular data         (independent)  │
5. Mark feature         (independent) ─┘
6. Stronger price alert (extends price, independent) ── Phase 2
7. Paper trade entry    (independent)                 ── Phase 2
8. Indicator data       (needs bars)                  ── Phase 3
9. Indicator alerts     (depends on 8)                ── Phase 3
10. Trend characteristic (depends on 8)               ── Phase 3
11. GUI interaction     (stretch, skip for now)
```

**Phase 1 (today):** 1–5 — all independent, no new dependencies, low risk.
**Phase 2:** 6–7 — extend existing systems, new DB tables.
**Phase 3:** 8–10 — indicator computation, most complex.

---

## Shared Design Decisions

### A. Pip Size Configuration

Different symbols have different pip sizes. Currently `pip_size = point * 10` (line 629) works for XAUUSD (point=0.01 → pip=0.10) but NOT for indices (NAS100 point=0.1 → pip=1.0; DJ30 point=1 → pip=10). 

**Solution:** Add `pip_size` to `pairs.yaml`:

```yaml
xauusd: {symbol: XAUUSD.pc, pip_size: 0.10}
nas100: {symbol: NAS100, pip_size: 1.0}
dj30: {symbol: DJ30, pip_size: 1.0}
btcusd: {symbol: BTCUSD.sc, pip_size: 1.0}
```

Load into `config.PIP_SIZES: dict[str, float]` keyed by broker symbol. Helper: `get_pip_size(broker_symbol) -> float` defaults to `point * 10` if not configured.

### B. Flexible Argument Parser (`bot/parser.py`)

Shared parser for commands that accept symbol, price, pips, direction, expiration. Tokenizes args and classifies each token:

```
Bare number (e.g. "2400")     → price target
+N or -N (e.g. "+20", "-10")  → pip offset
above/below                   → direction
expN or bare N at end         → expiration minutes
```

Parsing logic: scan left to right, classify each token. The command handler then validates the combination. This parser is used by price alert, mark, entry, and indicator alert commands.

### C. MessageHandler (Dot-Prefix Commands)

Register a single `MessageHandler` with filter `filters.TEXT & filters.Regex(r'^\.\w+')` that:
1. Strips the leading `.`
2. Splits on whitespace: first word = command name, rest = args
3. Maps command name → handler function (same dict as CommandHandler)
4. Reconstructs `context.args` and calls the same handler

This means ALL existing commands automatically work with `.add`, `.del`, `.p`, etc. Keep CommandHandlers for `/` — both work.

### D. Modular Data (`user_prefs` table)

Per-user toggles for which data sections appear in candle alerts. Three sections:

| Key | Section | Default |
|-----|---------|---------|
| `show_pattern` | Pattern name (DOJI, BEAR ENGULF, etc.) | on |
| `show_ohlc` | O H L C line | on |
| `show_range_body` | Range + Body + TW + BW | on |
| `show_bid_ask` | Bid Ask Spread line | on |
| `show_marks` | Mark distances (if any marks exist) | on |

Commands: `/data on|off <section>`, `/data list`. Stored as `user_prefs(chat_id, key, value)`.

---

## Phase 1 — Independent Features

### Task 1: Fix Bear Engulf Pattern

**Objective:** Prevent doji (c==o) candles from being classified as bear engulf.

**Root cause:** `patterns.py:32` sets `direction = "bearish"` when `c == o` (doji with equal open/close). Then lines 39-45 check `c < prev_bar.open` which can trigger for a doji — but a doji can't be an engulfing pattern by definition.

**Fix:** Add body ratio guard before engulfing checks. A candle with negligible body (< 5% of range) is not an engulfing candle.

**Files:**
- Modify: `bot/patterns.py:36-45`

**Step 1:** In `classify()`, after computing `body_ratio` (line 31), add a guard:

```python
# Engulfing requires a meaningful body (> 5% of range)
if body_ratio >= 0.05 and prev_bar and prev_bar.high > prev_bar.low:
```

Replace the existing `if prev_bar and prev_bar.high > prev_bar.low:` on line 36.

**Step 2:** Verify: `python -m compileall bot/patterns.py`

---

### Task 2: MessageHandler for Dot-Prefix Commands

**Objective:** Allow `.add xauusd 5`, `.d xauusd 5`, `.p 2400`, etc. without typing `/`.

**Files:**
- Modify: `bot/telegram_app.py`

**Step 1:** Add imports at top:

```python
from telegram.ext import MessageHandler, filters
```

**Step 2:** Add a dispatch dict and handler function (before `build_app`):

```python
# Command dispatch table (name → handler)
_COMMANDS: dict[str, callable] = {}

async def _handle_dot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle .command messages — strip dot, parse, dispatch."""
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
    # Set context.args to the remaining tokens
    context.args = parts[1:]
    await handler(update, context)
```

**Step 3:** In `build_app()`, populate `_COMMANDS` and register the MessageHandler:

```python
_COMMANDS.update({
    "help": cmd_help, "focus_pair": cmd_focus_pair, "fp": cmd_focus_pair,
    "add": cmd_add, "a": cmd_add,
    "del": cmd_del, "d": cmd_del,
    "list": cmd_list, "l": cmd_list,
    "offset": cmd_offset, "o": cmd_offset,
    "now": cmd_now, "n": cmd_now,
    "level": cmd_level, "lv": cmd_level,
    "price": cmd_price, "p": cmd_price,
    "cancel": cmd_cancel, "c": cmd_cancel,
    "status": cmd_status, "s": cmd_status,
})

app.add_handler(MessageHandler(
    filters.TEXT & filters.Regex(r'^\.\w+'), _handle_dot_command
))
```

**Step 4:** Verify: `python -m compileall bot/telegram_app.py`

**Pitfall:** `context.args` is normally a list. The handler functions use `context.args` which is `list[str]`. Make sure `_handle_dot_command` passes `parts[1:]` (a list) not a string.

---

### Task 3: Add Top Wick & Bottom Wick to Alerts

**Objective:** Show `TW` and `BW` in the Range+Body line of candle alerts.

**Current format (line 648):**
```
Range 12.50  Body 8.30
```

**New format:**
```
Range 12.50  Body 8.30  TW 2.10  BW 2.10
```

**Files:**
- Modify: `bot/telegram_app.py:_format_candle_message`

**Step 1:** In `_format_candle_message`, after the Range+Body line (line 648), compute wicks:

```python
# Range + Body + Wicks
range_p = bar.high - bar.low
body_p = abs(bar.close - bar.open)
tw = bar.high - max(bar.open, bar.close)   # top wick
bw = min(bar.open, bar.close) - bar.low    # bottom wick
lines.append(
    f"Range {_fmt_ohlc(range_p, symbol, sinfo)}  "
    f"Body {_fmt_ohlc(body_p, symbol, sinfo)}  "
    f"TW {_fmt_ohlc(tw, symbol, sinfo)}  "
    f"BW {_fmt_ohlc(bw, symbol, sinfo)}"
)
```

**Step 2:** Verify: `python -m compileall bot/telegram_app.py`

---

### Task 4: Modular Data Toggles

**Objective:** Let user customize which data sections appear in candle alerts.

**Files:**
- Modify: `bot/db.py` — add `user_prefs` table + CRUD
- Modify: `bot/telegram_app.py` — `/data` command + conditional formatting

**Step 1:** Add `user_prefs` table in `db.py:init_db()`:

```python
CREATE TABLE IF NOT EXISTS user_prefs (
    chat_id INTEGER NOT NULL REFERENCES users(chat_id),
    key     TEXT NOT NULL,
    value   TEXT NOT NULL DEFAULT 'on',
    PRIMARY KEY (chat_id, key)
);
```

**Step 2:** Add CRUD functions in `db.py`:

```python
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
```

**Step 3:** Add `cmd_data` handler in `telegram_app.py`:

```python
VALID_PREFS = {"show_pattern", "show_ohlc", "show_range_body", "show_bid_ask", "show_marks"}

async def cmd_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    args = context.args
    if not args:
        # Show current prefs
        prefs = db.get_user_prefs(chat_id)
        lines = [f"{k}: {prefs.get(k, 'on')}" for k in sorted(VALID_PREFS)]
        await update.message.reply_text("\n".join(lines) or "all on (default)")
        return
    if args[0].lower() == "list":
        prefs = db.get_user_prefs(chat_id)
        lines = [f"{k}: {prefs.get(k, 'on')}" for k in sorted(VALID_PREFS)]
        await update.message.reply_text("\n".join(lines))
        return
    if len(args) >= 2:
        action = args[0].lower()  # on or off
        key = args[1].lower()
        if key not in VALID_PREFS:
            await update.message.reply_text(_err(f"Unknown section: {key}\nOptions: {', '.join(sorted(VALID_PREFS))}"))
            return
        if action not in ("on", "off"):
            await update.message.reply_text(_err("Use: /data on|off <section>"))
            return
        db.set_user_pref(chat_id, key, action)
        await update.message.reply_text(f"✅ {key} = {action}")
```

**Step 4:** Modify `_format_candle_message` to check prefs:

```python
prefs = db.get_user_prefs(chat_id)
# ... build lines conditionally:
if prefs.get("show_pattern", "on") != "off" and pat:
    lines.append(pat.label)
if prefs.get("show_ohlc", "on") != "off":
    lines.append(f"O ... H ... L ... C ...")
# etc.
```

**Step 5:** Register handlers: `CommandHandler("data", cmd_data)`, shorthand `CommandHandler("dt", cmd_data)`, and add to `_COMMANDS`.

**Step 6:** Verify: `python -m compileall bot/db.py bot/telegram_app.py`

---

### Task 5: Mark Feature

**Objective:** Mark a price level, show distance from current price in candle alerts.

**Files:**
- Create: (none — reuse existing modules)
- Modify: `bot/models.py` — add `Mark` dataclass
- Modify: `bot/db.py` — add `marks` table + CRUD
- Modify: `bot/telegram_app.py` — `/mark` command + alert formatting
- Modify: `bot/config.py` — pip size loading from pairs.yaml

**Step 1:** Add `Mark` to `models.py`:

```python
@dataclass
class Mark:
    id: int = 0
    chat_id: int = 0
    symbol: str = ""
    price: float = 0.0
    created_at: str = ""
    expires_at: Optional[str] = None  # ISO timestamp or None = GTC
    label: str = ""  # optional user label
```

**Step 2:** Add `marks` table in `db.py:init_db()`:

```python
CREATE TABLE IF NOT EXISTS marks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES users(chat_id),
    symbol     TEXT NOT NULL,
    price      REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    label      TEXT NOT NULL DEFAULT ''
);
```

**Step 3:** Add CRUD in `db.py`:

```python
def add_mark(chat_id: int, symbol: str, price: float, expires_at: Optional[str] = None, label: str = "") -> Mark:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO marks (chat_id, symbol, price, expires_at, label) VALUES (?, ?, ?, ?, ?)",
            (chat_id, symbol, price, expires_at, label),
        )
        row = conn.execute("SELECT * FROM marks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_mark(row)

def get_marks(chat_id: int, symbol: Optional[str] = None) -> list[Mark]:
    if symbol:
        rows = _conn().execute(
            "SELECT * FROM marks WHERE chat_id = ? AND symbol = ? AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY id",
            (chat_id, symbol),
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM marks WHERE chat_id = ? AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [_row_to_mark(r) for r in rows]

def delete_mark(mark_id: int, chat_id: int) -> bool:
    with _tx() as conn:
        cur = conn.execute("DELETE FROM marks WHERE id = ? AND chat_id = ?", (mark_id, chat_id))
        return cur.rowcount > 0
```

**Step 4:** Add `cmd_mark` handler in `telegram_app.py`:

```
/mark xauusd 2400        → mark at 2400, GTC
/mark xauusd 2400 60     → mark at 2400, expires in 60 min
/mark del 1              → delete mark 1
/mark list               → list active marks
/mark xauusd             → list marks for symbol
```

**Step 5:** In `_format_candle_message`, add marks section (when marks exist and `show_marks` is on):

```python
if prefs.get("show_marks", "on") != "off" and tick:
    marks = db.get_marks(chat_id, symbol)
    if marks:
        pip_size = get_pip_size(symbol, sinfo)
        for m in marks:
            dist_pips = (tick.bid - m.price) / pip_size
            sign = "+" if dist_pips >= 0 else ""
            lines.append(f"📍 M{m.id} {_fmt_ohlc(m.price, symbol, sinfo)}  {sign}{dist_pips:.1f}p")
```

**Step 6:** Add `get_pip_size()` to `config.py`:

```python
# Load pip sizes from pairs.yaml
PIP_SIZES: dict[str, float] = {}
if PAIRS_PATH.exists():
    import yaml as _yaml
    with open(PAIRS_PATH) as f:
        raw = _yaml.safe_load(f) or {}
    for k, v in raw.items():
        if isinstance(v, dict):
            PAIRS[k.lower()] = v.get("symbol", v if isinstance(v, str) else "")
            if "pip_size" in v:
                PIP_SIZES[v["symbol"]] = float(v["pip_size"])

def get_pip_size(broker_symbol: str, sinfo=None) -> float:
    """Return pip size for a broker symbol. Falls back to point*10 if not configured."""
    if broker_symbol in PIP_SIZES:
        return PIP_SIZES[broker_symbol]
    if sinfo and sinfo.point > 0:
        return sinfo.point * 10  # default for forex
    return 0.01  # absolute fallback
```

**Step 7:** Update `pairs.yaml` to new format:

```yaml
xauusd: {symbol: XAUUSD.pc, pip_size: 0.10}
xagusd: {symbol: XAGUSD.pc, pip_size: 0.01}
btcusd: {symbol: BTCUSD.sc, pip_size: 1.0}
us30: {symbol: DJ30, pip_size: 1.0}
us100: {symbol: NAS100, pip_size: 1.0}
nas100: {symbol: NAS100, pip_size: 1.0}
dj30: {symbol: DJ30, pip_size: 1.0}
```

**Step 8:** Register handlers and add to `_COMMANDS`: `mark`, `mk`.

---

## Phase 2 — Extended Features

### Task 6: Stronger Price Alert

**Objective:** Add pip-range alerts, expiration, flexible argument parsing.

**Files:**
- Create: `bot/parser.py` — shared argument parser
- Modify: `bot/models.py` — add fields to `PriceAlert`
- Modify: `bot/db.py` — update `price_alerts` table + CRUD
- Modify: `bot/scheduler.py` — range crossing logic
- Modify: `bot/telegram_app.py` — extended `cmd_price`

**Step 1:** Create `bot/parser.py`:

```python
"""Flexible argument parser for trading commands."""
from dataclasses import dataclass
from typing import Optional

@dataclass
class ParsedArgs:
    symbol: Optional[str] = None
    price: Optional[float] = None
    pip_range: Optional[float] = None  # absolute value, always positive
    direction: Optional[str] = None    # "above", "below", None
    expiration_min: Optional[int] = None

def parse(args: list[str]) -> ParsedArgs:
    """Parse command args. Classifies tokens by pattern."""
    result = ParsedArgs()
    remaining = []
    for token in args:
        t = token.strip()
        if t.lower() in ("above", "below"):
            result.direction = t.lower()
        elif t.startswith("+") or t.startswith("-"):
            try:
                result.pip_range = abs(float(t))
            except ValueError:
                remaining.append(t)
        elif t.lower().startswith("exp"):
            try:
                result.expiration_min = int(t[3:])
            except ValueError:
                remaining.append(t)
        else:
            try:
                val = float(t)
                if val == int(val) and len(remaining) == 0:
                    # Could be price or expiration — defer to handler
                    remaining.append(t)
                else:
                    remaining.append(t)
            except ValueError:
                remaining.append(t)
    return result, remaining
```

Actually, keep the parser simpler. Let the command handler do the parsing inline — it's cleaner for this scale.

**Revised approach:** Extend `cmd_price` directly with token classification:

```python
async def cmd_price(update, context):
    args = context.args
    # Classify tokens
    symbol = None
    target = None
    pip_range = None
    direction = None
    expiration = None
    
    for token in args:
        t = token.strip()
        if t.lower() in ("above", "below"):
            direction = t.lower()
        elif t.startswith("+") or t.startswith("-"):
            pip_range = abs(float(t))
        elif t.lower().startswith("exp"):
            expiration = int(t[3:])
        else:
            try:
                val = float(t)
                if target is None:
                    target = val
                else:
                    expiration = int(val)
            except ValueError:
                if symbol is None:
                    symbol = t
```

**Step 2:** Add fields to `PriceAlert` model:

```python
@dataclass
class PriceAlert:
    # ... existing fields ...
    range_pips: float = 0.0       # 0 = exact price, >0 = range
    expires_at_minutes: Optional[int] = None  # minutes from creation
```

**Step 3:** Add columns to `price_alerts` table in migration:

```python
# In init_db(), after existing migration:
cols = [r[1] for r in conn.execute("PRAGMA table_info(price_alerts)").fetchall()]
if "range_pips" not in cols:
    conn.execute("ALTER TABLE price_alerts ADD COLUMN range_pips REAL NOT NULL DEFAULT 0")
if "expires_at_minutes" not in cols:
    conn.execute("ALTER TABLE price_alerts ADD COLUMN expires_at_minutes INTEGER")
conn.commit()
```

**Step 4:** Update `add_price_alert` signature and insert:

```python
def add_price_alert(chat_id, symbol, target, direction=None, range_pips=0.0, expires_at_minutes=None):
    # ... insert with new fields
```

**Step 5:** Update `_process_price_alerts` in scheduler for range logic:

When `range_pips > 0`, the alert triggers when price is within the range (target ± range_pips in pips). For range alerts, use `tick.bid` vs `[target - pip_range, target + pip_range]`. The alert fires once when price enters the range.

Also add expiration check: if `expires_at_minutes` is set, check `created_at + expires_at_minutes < now` and disable the alert.

**Step 6:** Update `_format_price_alert_message` for range alerts:

```
🔔 XAUUSD entered range 2400.00 ± 2.0p
Bid: 2400.15  Ask: 2400.35
```

---

### Task 7: Paper Trade Entry

**Objective:** Simple paper trade tracking with TP/SL, entry ID, modification.

**Files:**
- Modify: `bot/models.py` — add `PaperEntry` dataclass
- Modify: `bot/db.py` — add `paper_entries` table + CRUD
- Modify: `bot/telegram_app.py` — `/entry`, `/modify`, `/close` commands

**Step 1:** Add `PaperEntry` model:

```python
@dataclass
class PaperEntry:
    id: int = 0
    chat_id: int = 0
    entry_id: int = 0  # per-user short ID (1, 2, 3...)
    symbol: str = ""
    direction: str = ""  # "buy" or "sell"
    entry_type: str = ""  # "market", "limit", "stop"
    entry_price: float = 0.0
    tp_price: float = 0.0
    sl_price: float = 0.0
    status: str = "open"  # "open", "closed", "cancelled"
    created_at: str = ""
    closed_at: Optional[str] = None
```

**Step 2:** Add `paper_entries` table:

```python
CREATE TABLE IF NOT EXISTS paper_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL REFERENCES users(chat_id),
    entry_id    INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    entry_type  TEXT NOT NULL DEFAULT 'market',
    entry_price REAL NOT NULL,
    tp_price    REAL NOT NULL,
    sl_price    REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT,
    UNIQUE(chat_id, entry_id)
);
```

**Step 3:** Commands:

```
/entry buy +20 -10              → market buy, TP +20p, SL -10p
/entry sell +30 -15             → market sell
/entry buy limit 2400 +20 -10   → buy limit at 2400
/entry buy stop 2410 +20 -10    → buy stop at 2410
/modify 1 sl -5                 → modify entry 1, SL to -5 pips from entry
/modify 1 tp 2420               → modify entry 1, TP to absolute price
/modify 1 close                 → close entry 1
/entry list                     → list open entries
```

**Validation logic:**
- Buy: TP must be > entry price, SL must be < entry price
- Sell: TP must be < entry price, SL must be > entry price
- For limit/stop: check if entry price is plausible relative to current bid/ask
- If invalid: "No position placed — invalid TP/SL"

**Entry IDs:** Per-user short IDs (1, 2, 3...) with gap reuse, same pattern as price alert `user_seq`.

---

## Phase 3 — Indicator Features

### Task 8: Advanced Indicator Data

**Objective:** Compute ATR, BB, SMA, EMA, RSI, ADX, VWAP, Relative Volume from bar data.

**Files:**
- Create: `bot/indicators.py`
- Modify: `bot/mt5_data.py` — add `bars_n()` for fetching N bars
- Modify: `bot/telegram_app.py` — indicator section in alerts
- Modify: `bot/db.py` — `user_prefs` for indicator toggles

**Step 1:** Add `bars_n()` to `mt5_data.py`:

```python
async def bars_n(symbol: str, timeframe_min: int, count: int) -> list[Bar]:
    """Fetch the last N bars (0=current, 1=prev, ..., N-1=oldest)."""
    tf = _mt5_timeframe(timeframe_min)
    if tf is None:
        return []
    bars = await _call_mt5(mt5.copy_rates_from_pos, symbol, tf, 0, count)
    if bars is None or len(bars) == 0:
        return []
    return [_bar_from_row(b, symbol, timeframe_min) for b in bars]
```

**Step 2:** Create `bot/indicators.py`:

All indicators computed from OHLCV data. Use numpy for vectorized operations.

```python
"""Indicator computation from bar data."""
import numpy as np
from dataclasses import dataclass
from typing import Optional

@dataclass
class IndicatorSnapshot:
    atr: Optional[float] = None           # ATR(14)
    atr_trend: Optional[str] = None       # "rising", "falling", "flat"
    sma50: Optional[float] = None
    ema20: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    rsi: Optional[float] = None           # RSI(14)
    adx: Optional[float] = None
    vwap: Optional[float] = None
    rel_volume: Optional[float] = None    # current vol / avg vol(20)

def compute_all(bars: list) -> IndicatorSnapshot:
    """Compute all indicators from a list of Bar objects (newest first)."""
    if len(bars) < 50:
        return IndicatorSnapshot()
    
    closes = np.array([b.close for b in reversed(bars)])
    highs = np.array([b.high for b in reversed(bars)])
    lows = np.array([b.low for b in reversed(bars)])
    volumes = np.array([b.tick_volume for b in reversed(bars)], dtype=float)
    
    snap = IndicatorSnapshot()
    
    # ATR(14)
    if len(closes) >= 15:
        snap.atr = _atr(highs, lows, closes, 14)
        # ATR trend: compare last 3 ATR values
        atr_vals = [_atr(highs[:i], lows[:i], closes[:i], 14) for i in range(len(closes)-2, len(closes)+1)]
        if atr_vals[-1] > atr_vals[0] * 1.05:
            snap.atr_trend = "rising"
        elif atr_vals[-1] < atr_vals[0] * 0.95:
            snap.atr_trend = "compressing"
        else:
            snap.atr_trend = "flat"
    
    # SMA 50
    if len(closes) >= 50:
        snap.sma50 = float(np.mean(closes[-50:]))
    
    # EMA 20
    if len(closes) >= 20:
        snap.ema20 = _ema(closes, 20)
    
    # Bollinger Bands (20, 2)
    if len(closes) >= 20:
        sma20 = np.mean(closes[-20:])
        std20 = np.std(closes[-20:])
        snap.bb_middle = float(sma20)
        snap.bb_upper = float(sma20 + 2 * std20)
        snap.bb_lower = float(sma20 - 2 * std20)
    
    # RSI(14)
    if len(closes) >= 15:
        snap.rsi = _rsi(closes, 14)
    
    # ADX(14)
    if len(highs) >= 15:
        snap.adx = _adx(highs, lows, closes, 14)
    
    # VWAP (session — use available bars)
    if len(volumes) > 0 and np.sum(volumes) > 0:
        typical = (highs + lows + closes) / 3
        snap.vwap = float(np.sum(typical * volumes) / np.sum(volumes))
    
    # Relative Volume
    if len(volumes) >= 20:
        avg_vol = np.mean(volumes[-21:-1])  # avg of last 20 (excluding current)
        if avg_vol > 0:
            snap.rel_volume = float(volumes[-1] / avg_vol)
    
    return snap
```

Helper functions (standard TA formulas):
- `_atr(highs, lows, closes, period)` — Average True Range
- `_ema(data, period)` — Exponential Moving Average
- `_rsi(closes, period)` — Relative Strength Index
- `_adx(highs, lows, closes, period)` — Average Directional Index

**Step 3:** Add indicator section to `_format_candle_message`:

When `show_indicators` user pref is on, fetch 50 bars and compute indicators. Add a compact section:

```
📊 ATR 2.35 (compressing)  SMA50 2398.12  RSI 54.2
   BB 2385.40–2402.80  VWAP 2395.10  RVOL 1.2x
```

**Step 4:** Add indicator toggles to `VALID_PREFS`:

```python
VALID_PREFS = {
    # ... existing ...
    "show_indicators": "Indicator data section",
}
```

**Step 5:** For trend-story indicators (ATR, RSI, ADX), show last 3 values:

```
ATR 2.35→2.28→2.15 (compressing)
RSI 58.2→54.5→51.0 (weakening)
```

---

### Task 9: Indicator-Based Price Alerts

**Objective:** Alert when price crosses an indicator value (SMA50, BB Upper, etc.).

**Depends on:** Task 8 (indicator computation).

**Files:**
- Modify: `bot/telegram_app.py` — extend `/price` for indicator targets
- Modify: `bot/scheduler.py` — indicator crossing checks

**Command syntax:**
```
/price xauusd sma50 above     → alert when bid crosses above SMA50
/price xauusd bb_upper         → alert when bid crosses BB upper
/price xauusd rsi70 below      → alert when RSI crosses below 70
```

**Implementation:** Extend `PriceAlert` with `indicator` field (None = price level, "sma50", "bb_upper", etc.). In `_process_price_alerts`, if indicator is set, compute the indicator value each poll cycle and use it as the dynamic target.

**Indicator targets:**
- `sma50`, `sma200` — simple moving average
- `ema20` — exponential moving average
- `bb_upper`, `bb_lower`, `bb_middle` — Bollinger Bands
- `vwap` — volume-weighted average price
- `rsi30`, `rsi70` — RSI levels (price doesn't cross RSI, but we can alert when RSI crosses)

For "price crosses indicator" alerts, the target is dynamic (recomputed each poll). For "RSI crosses level" alerts, compute RSI each poll and check crossing.

---

### Task 10: Trend Characteristic

**Objective:** Looking back N candles, classify trend + supplement with indicator context.

**Depends on:** Task 8 (indicator computation).

**Files:**
- Modify: `bot/indicators.py` — add trend classification
- Modify: `bot/telegram_app.py` — `/trend` command, optional section in candle alerts

**Command:**
```
/trend xauusd 5            → trend on M5 (uses last 20 candles)
/trend xauusd 5 10         → trend using last 10 candles
```

**Classification logic:**
1. Fetch N bars (default 20)
2. Compute linear regression slope on closes
3. Classify: slope > threshold → "UP", slope < -threshold → "DOWN", else "SIDEWAYS"
4. Supplement with indicator snapshots:
   - ATR value + trend (compressing/expanding)
   - RSI (overbought > 70, oversold < 30, neutral)
   - Price relative to SMA50 (above/below)
   - Price relative to BB (near upper/middle/lower)

**Output format:**
```
📈 XAUUSD M5 — UP
   ATR 2.35 (compressing)  RSI 58.2 (neutral)
   Above SMA50 (+12.5p)  BB: upper zone
   RVOL 0.8x (below avg)
```

**In candle alerts:** Optional one-liner when `show_trend` pref is on:
```
Trend M5: UP  ATR compressing  RSI 54.2
```

---

## Task 11: GUI Interaction (Stretch)

Lowest priority. Options:
- Telegram inline keyboards (simple: yes/no buttons, timeframe picker)
- Telegram bot API web apps (complex, not worth it for solo use)

**Recommendation:** Skip for now. Command-based is faster and sufficient for a solo trader. Revisit if typing becomes a bottleneck.

---

## Files Summary

| File | Phase | Action |
|------|-------|--------|
| `bot/patterns.py` | 1 | Fix engulf: add body_ratio guard |
| `bot/telegram_app.py` | 1–3 | MessageHandler, new commands, formatting |
| `bot/db.py` | 1–3 | user_prefs, marks, paper_entries tables |
| `bot/models.py` | 1–3 | Mark, PaperEntry, extended PriceAlert |
| `bot/config.py` | 1 | pip_size loading from pairs.yaml |
| `pairs.yaml` | 1 | New format with pip_size per symbol |
| `bot/parser.py` | 2 | Shared argument parser (new) |
| `bot/scheduler.py` | 2–3 | Range alerts, indicator alerts, expiration |
| `bot/indicators.py` | 3 | Indicator computation (new) |
| `bot/mt5_data.py` | 3 | bars_n() for multi-bar fetch |

## Verification

After each phase:
```bash
python -m compileall bot/
python -c "from bot import config, db, models; db.init_db()"
```

After Phase 1: run bot, test `.add xauusd 5`, `.status`, verify TW/BW in alert, test `/data off pattern`, test `/mark xauusd 2400`.

---

## Key Design Decisions

1. **No new external dependencies** — all indicators computed from OHLCV with numpy (already in requirements.txt).
2. **Pip size from pairs.yaml** — not hardcoded, not computed from point (which varies by broker digit count).
3. **MessageHandler reuses existing handlers** — no code duplication. `_COMMANDS` dict maps names to functions.
4. **User prefs as key-value** — flexible, no schema changes needed for new toggles.
5. **Paper entries are tracked only** — no order execution, no P&L calculation. Just a log.
6. **Indicator alerts reuse price alert infrastructure** — just add dynamic target computation.
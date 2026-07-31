# ta-bot — AI Agent Guide

## Overview

Telegram bot that reads a local MetaTrader 5 terminal (read-only) to deliver:
- **Candle-close alerts** with OHLC, pattern classification, marks, and spread
- **Price-crossing alerts** (bid/ask crossing, close-range detection)
- **On-demand OHLC** (`/now`, `/level`)
- **Mark levels** shown inline in candle alerts
- **Paper trading** (market/limit/stop entries, SL/TP, auto-monitoring)

**Stack:** Python 3.11+, `python-telegram-bot[job-queue]` v20.x, `MetaTrader5`, `numpy`, `pyyaml`, SQLite via stdlib. Windows-only (MT5 requires Windows).

New `bot/indicators.py` module (Phase 3) adds BB(20,2) on close, SMA50, EMA20, ATR(14) with trend, RSI(14), ADX(14), VWAP, and Relative Volume — all computed from OHLCV bar arrays via numpy.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  __main__.py  — entrypoint                       │
│    init DB → init MT5 → health check → polling   │
└──────────────┬───────────────────────────────────┘
               │
    ┌──────────┴──────────────┐
    │         app.py          │  ← PTB wiring, handler registration,
    │  build_app()            │     scheduler callbacks, dot-command dispatch
    │  _send_candle/_price/   │
    │  _error/_paper_trade    │
    └────┬──────────────┬─────┘
         │              │
    ┌────▼─────────┐  ┌▼──────────────────┐
    │ telegram_app │  │    scheduler.py    │  ← background asyncio task
    │  .py         │  │  _candle_loop()    │     groups alerts by (symbol, tf, offset)
    │  handlers    │  │  _price_loop()     │     sleeps to nearest boundary once
    │  11 commands │  │  _paper_trade_loop│
    └──┬───┬───┬───┘  └──┬──────┬──────┬──┘
       │   │   │          │      │      │
       ▼   ▼   ▼          ▼      ▼      ▼
   ┌──────┐ ┌──────┐ ┌──────────┐ ┌──────────┐
   │ pars-│ │format│ │ mt5_data │ │    db    │
   │ ing  │ │ ting │ │  .py     │ │   .py    │
   │ .py  │ │ .py  │ │ async    │ │ SQLite   │
   │      │ │      │ │ adapter  │ │ thread-  │
   │      │ │      │ │          │ │ local    │
   └──────┘ └──────┘ └──────────┘ └──────────┘
   ┌──────┐ ┌──────┐
   │conf- │ │ pat- │
   │ ig   │ │ terns│
   │ .py  │ │ .py  │
   └──────┘ └──────┘
   ┌──────┐
   │ indi-│
   │ cator│
   │ s.py │
   └──────┘
```

## Module Map

### `bot/__main__.py` — Entrypoint
- `main()`: config.setup_logging → config.validate → db.init_db → mt5_data.init → mt5_data.health → build_app → app.run_polling()
- Synchronous init, then PTB takes over the event loop.
- KeyboardInterrupt runs mt5_data.shutdown().

### `bot/config.py` — Configuration
- Manual `.env` parsing (no python-dotenv dep). Reads key=value lines, skips comments.
- `BASE_DIR` = project root, `DB_PATH` = `bot.db`, `PAIRS_PATH` = `pairs.yaml`.
- `PAIRS`: `ideal_name → broker_symbol` (e.g. `xauusd → XAUUSD.pc`).
- `PAIRS_REVERSE`: `broker_symbol → ideal_name` (all uppercase keys).
- `DEFAULT_OFFSET_S = 8`, `DEFAULT_TIMEZONE = "Etc/GMT-8"`, `LATE_SEND_TOLERANCE_S = 3`, `PRICE_POLL_INTERVAL_S = 1.0`.
- Logging: StreamHandler + FileHandler to `bot.log`. Silences httpx and telegram loggers.

### `bot/models.py` — Dataclasses
- `User`, `CandleAlert`, `PriceAlert`, `CandleDelivery`, `Mark`, `PaperTrade`.
- All are plain dataclasses with defaults. No ORM.
- `PriceAlert`: supports `alert_type` ("crossing" or "close"), `target_upper` for close-range, `expires_at` ISO timestamp.
- `PaperTrade`: `order_type` ("market", "limit", "stop"), `direction` ("buy" or "sell"), `status` ("open" or "closed").

### `bot/db.py` — SQLite Persistence
- **Thread-local connections** (`threading.local()`): each thread gets its own `sqlite3.Connection`. WAL mode, foreign keys ON.
- `_tx()` context manager: commit on success, rollback on exception.
- `init_db()`: creates all tables + runs inline migrations (ALTER TABLE for added columns, backfills `user_seq`).
- Key functions (all synchronous):
  - `ensure_user(chat_id)` — upsert
  - `get_user(chat_id)` / `update_user(chat_id, **kwargs)`
  - `add_candle_alert(chat_id, symbol, timeframe_min, offset_s)` — dedupes by (chat_id, symbol, timeframe_min)
  - `get_candle_alerts()` — all enabled alerts
  - `add_price_alert(chat_id, ...)` — auto-assigns `user_seq` (lowest available, reuse gaps)
  - `get_price_alerts(chat_id=None)` — None = all enabled
  - `get_price_alert_by_user_seq(chat_id, user_seq)` — lookup by per-user ID
  - `update_price_alert(alert_id, **kwargs)` — generic update
  - `add_mark(chat_id, symbol, price, expires_at)` — auto-assigns user_seq
  - `get_marks(chat_id, symbol=None)` — filters expired
  - `delete_mark(chat_id, user_seq)` / `delete_all_marks(chat_id)`
  - `add_paper_trade(chat_id, ...)` / `get_paper_trades(chat_id, status)` / `get_all_open_paper_trades()`
  - `update_paper_trade(trade_id, **kwargs)` / `close_paper_trade(trade_id, exit_price, pnl)`
  - `get_user_prefs(chat_id)` / `set_user_pref(chat_id, key, value)` — user_prefs table
  - `was_delivered(chat_id, alert_key, candle_open_utc)` — dedup delivery
  - `record_delivery(chat_id, alert_key, candle_open_utc)` — mark sent
- **user_seq logic**: per-user sequence numbers (1, 2, 3…) for price alerts, marks, paper trades. Reuses gaps (lowest available). Allows users to reference by `p1`, `t1`, `M1`.

### `bot/timeframes.py` — Timeframe Parsing
- `parse_tf(raw)` → `int` (minutes) or `None`.
- Supports: bare numbers (3, 5, 15), m-prefixed (m3, M5), h-prefixed (h1, H4), d-prefixed (d1), w-prefixed (w1).
- `SUPPORTED` set: {1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440, 10080, 43200}.
- `tf_label(minutes)` → "M3", "H1", "D1", "W1".

### `bot/mt5_data.py` — MT5 Read-Only Adapter
- **All MT5 calls are async** via `_call_mt5(fn, *args, **kwargs)` which runs `fn` in a thread (`asyncio.to_thread`) behind an `asyncio.Lock` — serializes access, never blocks the event loop.
- `init(path)` / `shutdown()` / `health()` — connection lifecycle.
- `resolve_symbol(hint)` — 3-step resolution: pairs.yaml mapping → exact match → substring match (only if unique). Falls back to substring if mapped symbol not found in MT5.
- `suggest_symbols(hint)` — all symbols containing hint (case-insensitive).
- `symbol_info(symbol)` → `SymbolInfo` (digits, point, trade_mode).
- `tick(symbol)` → `Tick` (bid, ask, time, spread, age_s).
- `current_bar(symbol, tf)` / `previous_bar(symbol, tf)` / `bar_at_offset(symbol, tf, offset)` — all return `Bar` or `None`.
- `previous_day_bar(symbol)` / `today_open_bar(symbol)` — daily bars.
- `_mt5_timeframe(minutes)` — maps minute ints to MT5 constants (TIMEFRAME_M1, etc.).
- **Important:** `Tick.time` may be broker server time, not UTC. The scheduler uses `time.time()` (wall-clock UTC) for boundary calculations, not MT5 bar/tick times.

### `bot/scheduler.py` — Background Scheduler
- `scheduler_loop(send_candle, send_price, send_error, send_paper_trade)` — spawns 3 independent tasks via `asyncio.gather`.
- `subscriptions_changed` — `asyncio.Event`, set by command handlers when alerts change, causes the candle loop to re-read.

#### Candle Loop (`_candle_loop`)
1. Fetch all enabled candle alerts from DB.
2. Group by `(symbol, timeframe_min, effective_offset)` — each user's offset is resolved from alert.offset_s or user.default_offset_s.
3. For each group, compute `_next_boundary(now, interval_s)` — aligns to UTC wall-clock boundaries (e.g., M5 candles close at :00, :05, :10, :15…).
4. Find the nearest `due_time = close_epoch - offset` across all groups.
5. Sleep until that time (capped at 5s so subscription changes are picked up). Re-check `subscriptions_changed` after wake.
6. Process all groups whose due time is within `LATE_SEND_TOLERANCE_S` (3s).
7. `_send_group()`: fetch bar + previous bar + tick + symbol_info → fan out to all subscribed users. Deduplicates delivery via `was_delivered()` / `record_delivery()`.

#### Price Loop (`_price_loop`)
1. Poll all enabled price alerts every `PRICE_POLL_INTERVAL_S` (1s).
2. Skip expired alerts.
3. For crossing alerts: track `last_side` (above/below). Fire when side flips (e.g., bid was above target, now below).
4. For close alerts: check if bid crossed into/out of the target range. "Close" means the price closed beyond a level (cross and close) or within a range.
5. `_send_price()` callback delivers via Telegram.

#### Paper Trade Loop (`_paper_trade_loop`)
1. Poll all open paper trades every `PRICE_POLL_INTERVAL_S`. Group by symbol.
2. For limit/stop orders: check activation conditions. Buy limit: bid ≤ entry. Sell limit: ask ≥ entry. Buy stop: ask ≥ entry. Sell stop: bid ≤ entry. On activation, update `order_type` to "market", send `activated` event, then fall through to SL/TP check.
3. For market orders (including freshly activated): check SL (buy: bid ≤ SL, sell: ask ≥ SL) and TP (buy: bid ≥ TP, sell: ask ≤ TP). On hit, close trade, calculate PnL in pips, send event.

### `bot/parsing.py` — Argument Parsing (Pure Functions)
- `parse_expiry(arg)` → `(seconds, label)` or `None`. Supports `30m`, `2h`, `45s`.
- `fmt_expiry(expires_at)` → human-readable relative time string.
- `parse_mark_args(args)` → `(prices, expiry_seconds, expiry_label)`.
- `resolve_price_args(args, current_price, pip_size)` → `(alert_type, boundaries, expiry_s, expiry_label)`. Supports `close` keyword, `+N`/`-N` relative pips, absolute prices, expiry suffix.
- `resolve_relative_price(val, base_price, pip_size)` → absolute price.

### `bot/formatting.py` — Message Formatting
- `display_symbol(broker_symbol)` → ideal name via `PAIRS_REVERSE`.
- `fmt_price(price, symbol)` → adaptive decimal places (≥1000: 2dp, ≥100: 3dp, ≥1: 4dp, else 5dp).
- `fmt_ohlc(value, symbol, sinfo)` → uses `sinfo.digits` if available.
- `fmt_spread(spread, sinfo)` → in points.
- `format_candle_message(...)` → builds the multi-line candle alert. Respects user prefs: `show_pattern`, `show_ohlc`, `show_range_body`, `show_bid_ask`, `show_marks`. Includes marks with distance from current bid.
- `format_price_alert_message(alert, price, tick, chat_id)` → price alert notification.

### `bot/patterns.py` — Candle Pattern Classification

### `bot/indicators.py` — Indicator Computation (Phase 3)
- `compute_all(bars)` → `IndicatorSnapshot` — all indicators from a list of Bar objects (newest first). Uses numpy for vectorized operations.
- Indicators: **BB(20,2) on close** (upper/middle/lower, width%), **SMA50**, **EMA20**, **ATR(14)** (with trend: rising/compressing/flat), **RSI(14)** (0-100, Wilder's smoothing), **ADX(14)** (0-100, Wilder's), **VWAP** (cumulative typical price × volume), **RelVol** (current vol / avg vol of last 20).
- `format_indicator_section(snap, symbol, sinfo)` → compact 2-line display for candle alerts.
- `format_indicator_full(snap, symbol, sinfo)` → full report for `/ind` command.
- Requires 50+ bars for SMA50, 20+ for BB/EMA, 15+ for ATR/RSI, 28+ for ADX. Missing indicators are None.
- Bars are reversed internally (newest-first → chronological) before computation.
- `classify(bar, prev_bar)` → `Pattern(emoji, label, bias)`.
- Detection order: Engulfing (bear/bull) → Hammer → Shooting Star → Doji → Bullish/Bearish.
- Engulfing requires: body ≥ 5% of range, opposite direction, close crosses previous open.
- Pattern is evaluated on the **incomplete** current bar — may shift before close.

### `bot/app.py` — Application Builder
- `build_app()` — creates PTB `Application`, registers all `CommandHandler`s (with short aliases: `/a`, `/d`, `/l`, `/p`, `/c`, `/s`, `/e`, `/m`, `/mk`, `/dt`, `/n`, `/lv`, `/o`, `/mkd`, `/mkl`).
- `_COMMANDS` dict: maps command name to handler function — used by dot-command dispatcher.
- `_handle_dot_command()` — `MessageHandler` for `filters.Regex(r'^\.\w+')`. Strips leading dot, routes to same handler as slash command.
- `post_init` hook: starts `scheduler.scheduler_loop()` as a background asyncio task.
- Scheduler callbacks: `_send_candle`, `_send_price`, `_send_error`, `_send_paper_trade` — all call `_app_ref.bot.send_message()`.

### `bot/telegram_app.py` — Command Handlers
- **11 command handlers**: `cmd_focus_pair`, `cmd_help`, `cmd_add`, `cmd_del`, `cmd_list`, `cmd_offset`, `cmd_now`, `cmd_level`, `cmd_price`, `cmd_cancel`, `cmd_status`, `cmd_data`, `cmd_mark`, `cmd_entry`, `cmd_modify`, `cmd_clear`, plus shorthand wrappers (`cmd_mark_del`, `cmd_mark_list`).
- **Focus pair**: `_focus_pairs: dict[int, str]` — per-chat in-memory dict, session-only, not persisted. Resolved via `_get_focus(chat_id)`.
- **Multi-arg support**: `/add 5 15 30`, `/del 5 15 30`, `/price 2400 2450 2500`, `/mark 2400 2450 2500` — all work with focus pair.
- **Paper trade**: `/entry` (list or create), `/modify` (sl/tp/close). Entry syntax: buy/sell, optional limit/stop, +N/-N for SL/TP. Modify uses `sl`/`tp` keywords for absolute prices.
- **Shorthand wrappers**: `cmd_mark_del` delegates to `cmd_mark(["del", ...])`, `cmd_mark_list` delegates to `cmd_mark(["list", ...])`.

## Database Schema

```sql
users (chat_id PK, timezone, default_offset_s, created_at)
candle_alerts (id PK, chat_id FK→users, symbol, timeframe_min, offset_s, enabled)
price_alerts (id PK, chat_id FK→users, user_seq, symbol, direction, target, target_upper, alert_type, price_source, repeat, enabled, last_side, expires_at, created_at)
deliveries (chat_id, alert_key, candle_open_utc — composite PK)
user_prefs (chat_id, key — composite PK, value)
marks (id PK, chat_id FK→users, user_seq, symbol, price, created_at, expires_at, label)
paper_trades (id PK, chat_id FK→users, user_seq, symbol, direction, order_type, entry_price, position_size, stop_loss, take_profit, status, exit_price, pnl, opened_at, closed_at)
```

## Key Design Decisions

1. **Wall-clock UTC for scheduling** — MT5 bar/tick times may be in broker server timezone. Candle boundaries are calculated from `time.time()`, not MT5 data. `_next_boundary()` aligns to UTC boundaries.

2. **Group-then-fan-out** — Candle alerts are grouped by `(symbol, timeframe, offset)` so MT5 data is fetched once per group, then delivered to all subscribers. This minimizes MT5 calls.

3. **Offset per alert or per user** — `alert.offset_s` takes precedence over `user.default_offset_s`. Offsets are seconds before candle close.

4. **Dedup via deliveries table** — prevents duplicate candle alerts. Key is `(chat_id, alert_key, candle_open_utc)`.

5. **user_seq pattern** — All user-facing IDs (p1, t1, M1) are per-user sequence numbers, not global DB IDs. Reuses gaps from deleted items. This keeps IDs short and user-friendly.

6. **Manual .env parsing** — No `python-dotenv` dependency. Simple line-by-line parsing in `config.py`.

7. **Thread-local SQLite** — Each thread gets its own connection. WAL mode for concurrent reads. No connection pooling.

8. **MT5 serialization** — All MT5 calls go through `_call_mt5()` with an asyncio.Lock, ensuring only one call is in flight at a time. This prevents MT5 API threading issues.

9. **Dot-command support** — `.add xauusd 5` works same as `/add xauusd 5`. Implemented via MessageHandler + regex, not CommandHandler.

10. **Inline migrations** — `init_db()` runs ALTER TABLE for columns added after initial schema. Uses PRAGMA table_info to detect missing columns.

## Common Pitfalls

- **MT5 must be running and logged in** — the bot connects to the existing terminal, doesn't launch it.
- **Broker symbol suffixes** — MT5 symbols often have suffixes (`.pc`, `.sc`). Map them in `pairs.yaml`.
- **`time.time()` vs server time** — Never use MT5 bar/tick `.time` for scheduling. Always use `time.time()`.
- **Thread-local DB connections** — Don't share a connection across threads. Use `db._conn()` which is thread-local.
- **`subscriptions_changed` event** — Must be set after any add/delete/enable/disable of candle alerts. Not needed for price alerts (they poll every second anyway).
- **`LATE_SEND_TOLERANCE_S = 3`** — If the scheduler wakes up >3s late, it skips the candle. Prevents stale alerts.
- **`parse_tf` returns `None`** for unsupported timeframes — always check before using.
- **`resolve_symbol` may return different case** than what was passed — MT5 returns exact symbol names from the broker.
- **Paper trade `order_type` transitions** — limit/stop orders become "market" on activation. The scheduler checks this field to decide whether to skip SL/TP for pending orders.

## Command → Handler Mapping

| Command | Handler | Notes |
|---------|---------|-------|
| `/fp` | `cmd_focus_pair` | Session-only, in-memory dict |
| `/help` | `cmd_help` | Static HTML message |
| `/add`, `/a` | `cmd_add` | Multi-arg with focus |
| `/del`, `/d` | `cmd_del` | Multi-arg with focus |
| `/list`, `/l` | `cmd_list` | Shows candle + price alerts |
| `/offset`, `/o` | `cmd_offset` | Default offset_s per user |
| `/now`, `/n` | `cmd_now` | Last completed bar OHLC |
| `/level`, `/lv` | `cmd_level` | Yesterday OHLC + today open |
| `/price`, `/p` | `cmd_price` | Multi-arg, close type, relative pips, expiry |
| `/cancel`, `/c` | `cmd_cancel` | Bare = cancel all, pID = specific |
| `/status`, `/s` | `cmd_status` | MT5 health + alert counts |
| `/data`, `/dt` | `cmd_data` | Toggle show_pattern/show_ohlc/etc |
| `/mark`, `/mk` | `cmd_mark` | Multi-arg, expiry suffix, del/list subcommands |
| `/mkd` | `cmd_mark_del` | Delegates to cmd_mark with "del" |
| `/mkl` | `cmd_mark_list` | Delegates to cmd_mark with "list" |
| `/entry`, `/e` | `cmd_entry` | List or create paper trades |
| `/modify`, `/m` | `cmd_modify` | sl/tp/close for paper trades |
| `/clear` | `cmd_clear` | Remove all alerts + marks |
| `/indicator`, `/ind` | `cmd_indicator` | On-demand indicator data (Phase 3) |

## Development Workflow

```bash
# Activate venv
source .venv/Scripts/activate

# Run
python -m bot

# Requirements
pip install -r requirements.txt  # 4 deps: PTB, MT5, numpy, pyyaml
```

- **No test suite** — the `tests/` directory is empty.
- **No type checker or linter config** — no `pyproject.toml`, `mypy.ini`, or `.flake8`.
- **Single binary DB file** — `bot.db` is gitignored. Migrations run inline in `init_db()`.
- **Logging** — to console + `bot.log`. MITM `logger = logging.getLogger(__name__)` pattern.
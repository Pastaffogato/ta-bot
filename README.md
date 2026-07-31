# ta-bot — Telegram Trading Alert Bot

Lightweight Telegram bot that reads from a local MetaTrader 5 terminal (read-only) to send
candle-close alerts, price-crossing alerts, and on-demand OHLC / key-level data.

## Requirements

- **Python 3.11+**
- **MetaTrader 5** terminal installed and logged into your broker
- **Windows** or **Windows VPS** (MT5 requires Windows)

## Setup (new device / VPS)

### 1. Clone

```bash
git clone https://github.com/Pastaffogato/ta-bot.git
cd ta-bot
```

### 2. Python environment

```bash
python -m venv .venv
source .venv/Scripts/activate   # Git Bash
# or: .venv\Scripts\activate    # cmd
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

Copy the example env file and fill in your bot token:

```bash
cp .env.example .env
```

Edit `.env`:

```
BOT_TOKEN=your_telegram_bot_token_here
LOG_LEVEL=INFO
```

Get a bot token from [@BotFather](https://t.me/BotFather) on Telegram.

### 5. Pair mapping

Edit `pairs.yaml` to match your broker's symbol suffixes:

```yaml
xauusd: XAUUSD.pc
nas100: NAS100
btcusd: BTCUSD.sc
```

### 6. Login to MT5

Open your MetaTrader 5 terminal on the VPS, log into your broker account, and keep it running.
The bot connects to the running terminal — it does NOT launch MT5 itself.

### 7. Run

```bash
python -m bot
```

### 8. Keep it running (VPS)

Use `nssm` (recommended) or Windows Task Scheduler to run the bot as a background service:

```powershell
nssm install ta-bot
# Path: C:\path\to\.venv\Scripts\python.exe
# Arguments: -m bot
# Start directory: C:\path\to\ta-bot
```

## Commands

| Command | Shorthand | Example |
|---------|-----------|---------|
| `/fp` |  | `/fp XAUUSD` — set session focus pair |
| `/fp` |  | `/fp off` — clear focus |
| `/add` | `/a` | `/add 5` — timer-only M5 alert |
| `/add` | `/a` | `/add XAUUSD 5` — M5 alert with OHLC |
| `/del` | `/d` | `/del` — remove all candle alerts |
| `/del` | `/d` | `/del XAUUSD 5` — remove specific alert |
| `/list` | `/l` | show active alerts |
| `/offset` | `/o` | `/offset 8` — set pre-close seconds (0–60) |
| `/now` | `/n` | `/now XAUUSD 3` — last completed M3 OHLC + bid/ask |
| `/now` | `/n` | `/now` (with fp) — last completed M1 OHLC |
| `/level` | `/lv` | `/level XAUUSD` — yesterday OHLC + today open |
| `/price` | `/p` | `/price XAUUSD 2400` — cross alert (any direction) |
| `/price` | `/p` | `/price XAUUSD above 2400` — directional |
| `/cancel` | `/c` | `/cancel p7` — remove price alert by ID |
| `/cancel` | `/c` | `/cancel` — remove ALL price alerts |
| `/data` | `/dt` | `/data` — list toggleable sections |
| `/data` | `/dt` | `/data off pattern` — hide pattern info |
| `/mark` | `/mk` | `/mark XAUUSD 2400.50` — mark a price level |
| `/mark` | `/mk` | `/mark 2400.50` (with fp) — mark on focus pair |
| `/mark` | `/mk` | `/mark 2400.50 60` — mark with 60min expiry |
| `/mark` | `/mk` | `/mark del` — delete ALL marks |
| `/mark` | `/mk` | `/mark del 1` — delete mark by ID |
| `/mark` | `/mk` | `/mark list` — list active marks |
| | `/mkd` | shorthand for `/mark del` |
| | `/mkl` | shorthand for `/mark list` |
| `/data` | `/dt` | `/data off show_bid_ask show_range` — toggle multiple |
| `/clear` | | remove ALL alerts + marks (clean slate) |
| `/add` | `/a` | `/add 5 15 30` (with fp) — add multiple TFs at once |
| `/del` | `/d` | `/del 5 15 30` (with fp) — delete multiple TFs at once |
| `/price` | `/p` | `/price 2400 2450 2500` (with fp) — add multiple at once |
| `/status` | `/s` | bot health, MT5 connection, active alerts |
| `/help` | | command reference |

All commands also work with a dot prefix: `.add xauusd 5`, `.now`, `.status`, `.mk 2400`, etc.

### Focus pair

Set a session focus pair with `/fp XAUUSD`. After that, commands accept shorter forms:

| Without focus | With focus (XAUUSD) |
|--------------|---------------------|
| `/add XAUUSD 5` | `/add 5` (or `/add 5 15 30` for multi) |
| `/del XAUUSD 5` | `/del 5` (or `/del 5 15 30` for multi) |
| `/now XAUUSD 3` | `/now 3` (or `/now` for M1) |
| `/level XAUUSD` | `/level` |
| `/price XAUUSD 2600` | `/price 2600` (or `/price 2400 2450 2500` for multi) |
| `/price XAUUSD above 2600` | `/price above 2600` |
| `/mark XAUUSD 2400` | `/mark 2400` |

Focus is session-only (not persisted). Clear with `/fp off`.

## Timeframes

Bare numbers = minutes: `3`, `5`, `15`. Letter-prefixed: `m3`, `M5`, `h1`, `H4`.

## Project structure

```
ta-bot/
├── bot/
│   ├── __main__.py        # entrypoint
│   ├── config.py          # env loading, paths, PAIRS
│   ├── db.py              # SQLite persistence
│   ├── models.py          # dataclasses
│   ├── mt5_data.py        # read-only MT5 adapter
│   ├── patterns.py        # candle pattern classifier
│   ├── scheduler.py       # alert scheduling & fan-out
│   ├── telegram_app.py    # PTB handlers + formatting
│   └── timeframes.py      # timeframe parsing
├── pairs.yaml             # broker symbol mapping
├── requirements.txt
├── .env.example
└── README.md
```

## Notes

- **Read-only**: the bot never places trades or modifies MT5 state.
- **Public bot**: no allowlist — any Telegram user can add alerts.
- **MT5 must stay logged in**: if the terminal disconnects, alerts stop.
- **Broker timezone**: the bot uses wall-clock UTC for scheduling, not broker server time.
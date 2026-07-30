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
| `/add` | `/a` | `/add 5` — timer-only M5 alert |
| `/add` | `/a` | `/add XAUUSD 5` — M5 alert with OHLC |
| `/del` | `/d` | `/del` — remove all candle alerts |
| `/del` | `/d` | `/del XAUUSD 5` — remove specific alert |
| `/list` | `/l` | show active alerts |
| `/offset` | `/o` | `/offset 8` — set pre-close seconds (0–60) |
| `/now` | `/n` | `/now XAUUSD 3` — live M3 OHLC + bid/ask |
| `/level` | `/lv` | `/level XAUUSD` — yesterday OHLC + today open |
| `/price` | `/p` | `/price XAUUSD 2400` — cross alert (any direction) |
| `/price` | `/p` | `/price XAUUSD above 2400` — directional |
| `/cancel` | `/c` | `/cancel p7` — remove price alert by ID |
| `/cancel` | `/c` | `/cancel` — remove ALL price alerts |
| `/tz` | `/t` | `/tz Asia/Jakarta` — set timezone (default UTC+8) |
| `/status` | `/s` | bot health, MT5 connection, active alerts |
| `/help` | | command reference |

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
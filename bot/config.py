import os
import logging
from pathlib import Path

# ---- load .env ----
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() not in os.environ:
                os.environ[key.strip()] = val.strip()

# ---- paths ----
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = str(BASE_DIR / "bot.db")
PAIRS_PATH = BASE_DIR / "pairs.yaml"

# ---- pair mapping ----
PAIRS: dict[str, str] = {}          # ideal → broker (e.g. "xauusd" → "XAUUSD.pc")
PAIRS_REVERSE: dict[str, str] = {}  # broker → ideal (e.g. "XAUUSD.pc" → "xauusd")
if PAIRS_PATH.exists():
    import yaml as _yaml
    with open(PAIRS_PATH) as f:
        raw = _yaml.safe_load(f) or {}
    PAIRS = {k.lower(): v for k, v in raw.items() if isinstance(v, str)}
    PAIRS_REVERSE = {v.upper(): k.lower() for k, v in PAIRS.items()}

# ---- telegram ----
BOT_TOKEN = os.environ["BOT_TOKEN"]

# ---- mt5 ----
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", None)  # None = use default

# ---- ea file bridge ----
# Optional override for the EA signal file. Default (None) = derive from MT5:
# <terminal data_path>\MQL5\Files\ea_signals.txt
EA_SIGNAL_FILE = os.environ.get("EA_SIGNAL_FILE") or None

# ---- logging ----
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# ---- defaults ----
DEFAULT_OFFSET_S = 0          # seconds before candle close (0 = at close; offsets > 0 make the pattern provisional since the candle is still forming)
DEFAULT_TIMEZONE = "Etc/GMT-8"  # UTC+8
LATE_SEND_TOLERANCE_S = 3    # skip if we're this late
TICK_FRESHNESS_S = 10         # max tick age before "stale"
PRICE_POLL_INTERVAL_S = 1.0   # seconds between price checks
CANDLE_SLEEP_GRANULARITY_S = 0.5  # wake-up resolution near boundaries


def validate() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        ],
    )
    # silence noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
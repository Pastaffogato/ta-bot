"""One-time documented action: reset the stale EA-signal backlog offset.

Rationale (2026-08-02): the 20:51-21:05 bot run delivered ZERO of the 10
blocks appended after offset 2268 because parse_blocks() sniffed the UTF-16
BOM on the tail slice instead of the file head (bug now fixed). Those 10
blocks are duplicated test-era signals; per the no-history-replay contract we
do NOT deliver them. This script moves meta 'ea_signal_offset' to the current
EOF and logs the action in bot.log.

Run: .venv/Scripts/python.exe scripts/one_time_reset_signal_offset.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # repo root needed for `import bot`

from bot import db

SIGNAL_PATH = r"C:\Users\Thinkpad\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\ea_signals.txt"

size = os.path.getsize(SIGNAL_PATH)
old = db.get_meta("ea_signal_offset")
db.set_meta("ea_signal_offset", str(size))
msg = (
    f"{datetime.now():%Y-%m-%d %H:%M:%S} MANUAL  one-time backlog reset: "
    f"ea_signal_offset moved from {old} to {size} (current EOF, file size {size} bytes). "
    f"The 10 stale test-era blocks after offset 2268 are NOT replayed "
    f"(no-history-replay contract). parse_blocks() BOM-sniff fix shipped: "
    f"BOM read from file byte 0, not from the tail slice."
)
with open("bot.log", "a", encoding="utf-8") as f:
    f.write(msg + "\n")
print(msg)
print("meta now:", db.get_meta("ea_signal_offset"))

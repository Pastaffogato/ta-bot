"""E2E helper: backup + truncate ea_signals.txt to 0 bytes and reset offset meta.

This is the documented E2E dummy-test reset (step 2 of the runbook):
  * backup the current file next to it (ea_signals.txt.bak-e2e)
  * truncate the live file to 0 bytes
  * set meta ea_signal_offset = '0' so the FIRST appended dummy block is
    treated as new content (never as history to skip)

Run: .venv/Scripts/python.exe scripts/e2e_reset_file.py
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import db

SIGNAL_PATH = Path(
    r"C:\Users\Thinkpad\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\ea_signals.txt"
)
BACKUP = SIGNAL_PATH.with_name(SIGNAL_PATH.name + ".bak-e2e")

size = os.path.getsize(SIGNAL_PATH)
shutil.copy2(SIGNAL_PATH, BACKUP)
print(f"backup  -> {BACKUP} ({size} bytes)")

with open(SIGNAL_PATH, "r+b") as f:
    f.truncate(0)
print(f"truncate -> {SIGNAL_PATH} to 0 bytes")

db.set_meta("ea_signal_offset", "0")
print(f"meta ea_signal_offset -> 0 (now {db.get_meta('ea_signal_offset')!r})")

"""EA signal file bridge: tail ea_signals.txt and fan out [SIGNAL] blocks.

The EA (SignalNotifier.mq5, phase 2) appends one
    "[SIGNAL]\\n<5-line msg>\\r\\n\\r\\n"
block per qualifying signal to <MT5 data_path>\\MQL5\\Files\\ea_signals.txt.
This loop tails that file by a persisted byte offset (meta key
'ea_signal_offset') and broadcasts each complete block to every opted-in user
(user_prefs key 'ea_signals'; absent pref = on). Blocks are opaque text —
the message is delivered verbatim, nothing is parsed.

Design notes (PLAN-telegram.md §5):
- Poll every 1 s; process ONLY complete blocks (a trailing partial block means
  the EA is mid-write — re-read it on the next tick).
- Offset is persisted AFTER a block is fully read, so a crash never re-sends a
  consumed block; a send that crashed mid-fan-out may re-send one block after a
  restart (accepted, documented).
- First-ever run with a pre-existing file seeds the offset at the current file
  size — history is never replayed (matches phase-1 "no historical signals").
"""

import asyncio
import logging
import os
from typing import Callable, Optional

from bot import config, db, mt5_data

logger = logging.getLogger(__name__)

SEP = b"\r\n\r\n"          # block separator written by the EA
SEP_TXT = SEP.decode("ascii")  # same separator in text space (post-decode)
OFFSET_KEY = "ea_signal_offset"
POLL_INTERVAL_S = 1.0      # tail tick
MAX_FILE_BYTES = 1_000_000  # hygiene: trim fully-consumed files past this size


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-testable without any IO)
# ---------------------------------------------------------------------------

def parse_blocks(raw: bytes, codec: Optional[str] = None) -> tuple[list[str], int]:
    """Split raw bytes into complete signal blocks.

    Tolerates both encodings the EA may have produced:
      - UTF-16LE with BOM (MQL5 FileWriteString default — the original bug that
        made separators invisible and blocked all delivery)
      - plain ANSI/UTF-8 (current EA: FILE_ANSI flag)

    `codec` is the codec of `raw` (e.g. 'utf-16-le', 'utf-8'). When None it is
    auto-detected from `raw` itself — only valid for offset-0 slices that still
    carry the BOM. Non-zero-offset slices NEVER contain the BOM (it lives at
    byte 0 of the FILE), so callers that read from a persisted offset must pass
    the file-sniffed codec (see read_complete_blocks) — otherwise the slice
    decodes as utf-8 and the doubled UTF-16 separators never match.

    Returns (blocks, consumed):
      blocks   — decoded complete blocks (separator stripped, text verbatim)
      consumed — bytes to persist as the new offset; points just past the last
                 complete block. A trailing partial block (no SEP at EOF) is
                 the EA mid-write and is NOT consumed — the next tick re-reads it.
                 Consumed is byte-exact in both cases:
                   * slice starts at the BOM (offset 0): len(head)+2 bytes
                   * slice starts mid-file (offset > 0): len(head) bytes,
                     i.e. NO phantom BOM is re-added on encode.
    """
    if not raw:
        return [], 0
    if codec is None:
        # Auto-detect — valid only when the slice starts at the file head.
        if raw.startswith(b"\xff\xfe"):
            codec = "utf-16-le"
        elif raw.startswith(b"\xfe\xff"):
            codec = "utf-16-be"
        else:
            codec = "utf-8"
    bom_in_slice = raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff")
    text = raw.decode(codec, errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]                # BOM decoded as U+FEFF (utf-16-le/-be keeps it)
    end = text.rfind(SEP_TXT)
    if end < 0:
        return [], 0                      # only a partial block — wait for the rest
    head = text[: end + len(SEP_TXT)]
    blocks = [c for c in head.split(SEP_TXT) if c]
    consumed = len(head.encode(codec, errors="replace"))
    if bom_in_slice:
        consumed += 2                     # the BOM bytes are part of the consumed span
    return blocks, consumed


def _sniff_codec(file_head: bytes) -> str:
    """Codec of the EA signal file, sniffed from its FIRST bytes.

    The BOM (b'\\xff\\xfe' / b'\\xfe\\xff') exists only at byte 0 of the file,
    never inside a non-zero-offset slice — so it must be sniffed from the file
    head, not from the tail slice (the original delivery bug).
    """
    if file_head.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if file_head.startswith(b"\xfe\xff"):
        return "utf-16-be"
    return "utf-8"


def read_complete_blocks(path: str, offset: int) -> tuple[list[str], int, int]:
    """Read from `offset` to EOF.

    Returns (blocks, new_offset, size):
      blocks     — complete blocks found after `offset`
      new_offset — offset advanced past the last complete block
      size       — current file size in bytes
    Raises FileNotFoundError if the file does not exist; OSError on transient
    sharing/lock issues (caller treats both as "wait for the next tick").
    """
    size = os.path.getsize(path)
    if offset >= size:
        return [], size, size
    with open(path, "rb") as f:
        codec = _sniff_codec(f.read(2))   # BOM lives at byte 0 of the FILE, not of the slice
        f.seek(offset)
        raw = f.read()
    if offset % 2 == 1 and codec != "utf-8":
        # ASCII content keeps every UTF-16 offset even; an odd offset means the
        # persisted meta is corrupted/out of sync and the decode will be
        # misaligned (never crashes — errors="replace" absorbs the odd byte).
        logger.warning(
            "ea_signals.txt: odd offset %d into %s file (expected even) — "
            "bytes will decode misaligned", offset, codec
        )
    blocks, consumed = parse_blocks(raw, codec)
    return blocks, offset + consumed, size


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

async def resolve_signal_path() -> Optional[str]:
    """Signal file path: .env override EA_SIGNAL_FILE, else derived from the
    MT5 terminal data path (<data_path>\\MQL5\\Files\\ea_signals.txt)."""
    if config.EA_SIGNAL_FILE:
        return config.EA_SIGNAL_FILE
    data_path = await mt5_data.terminal_data_path()
    if not data_path:
        return None
    return os.path.join(data_path, "MQL5", "Files", "ea_signals.txt")


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

async def _fan_out(block: str, send_signal: Callable) -> None:
    """Send one opaque [SIGNAL] block to every opted-in user.

    Per-user try/except: a blocked/deactivated user is logged and skipped —
    it must never crash the loop.
    """
    recipients = db.get_ea_signal_recipients()
    if not recipients:
        logger.info("ea signal broadcast: no opted-in recipients")
        return
    logger.info("ea signal broadcast: fanning out to %d recipient(s)", len(recipients))
    for chat_id in recipients:
        try:
            await send_signal(chat_id, block)
        except Exception as e:  # noqa: BLE001 — one bad user must not kill the loop
            logger.warning("ea signal to chat %s failed: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Tail loop
# ---------------------------------------------------------------------------

def _trim(path: str) -> None:
    """Hygiene (PLAN-telegram.md §5.6): shrink a fully-consumed oversized file
    to 0 bytes. Only called when every byte has been consumed (new_offset ==
    size), so no block is lost; the EA's seek-to-end append tolerates it.
    The offset meta is reset to 0 to keep the offset == EOF invariant."""
    try:
        with open(path, "r+b") as f:
            f.truncate(0)
        db.set_meta(OFFSET_KEY, "0")
        logger.info("ea_signals.txt trimmed to 0 bytes (offset reset)")
    except OSError:
        logger.debug("ea_signals.txt trim skipped (busy)")


async def _tick(path: str, send_signal: Callable) -> None:
    """One poll cycle: seed or resume the tail offset, fan out complete blocks."""
    offset_raw = db.get_meta(OFFSET_KEY)

    if offset_raw is None:
        # First-ever run. If a file already exists it is pre-boot history —
        # seed at EOF (never replay it). If NO file exists yet, seed offset 0
        # so the FIRST signal that appears after startup IS delivered (a
        # "file appears later" race used to swallow the first fresh signal).
        try:
            size = os.path.getsize(path)
        except OSError:
            db.set_meta(OFFSET_KEY, "0")
            logger.info("ea_signals.txt not present yet — offset 0 (first signal will be delivered)")
            return
        db.set_meta(OFFSET_KEY, str(size))
        logger.info("ea_signals.txt seeded at offset %d (no history replay)", size)
        return

    offset = int(offset_raw)

    try:
        blocks, new_offset, size = read_complete_blocks(path, offset)
    except FileNotFoundError:
        return                          # EA-side file gone — wait for it to come back
    except OSError:
        return                          # transient sharing/lock issue — retry next tick

    prev_offset = offset
    if offset > size:
        # File was truncated/rotated externally — restart at the new EOF.
        logger.warning(
            "ea_signals.txt smaller than persisted offset (%d > %d); reset", offset, size
        )
        new_offset = size

    for block in blocks:
        await _fan_out(block, send_signal)

    if new_offset != prev_offset:
        db.set_meta(OFFSET_KEY, str(new_offset))

    if size > MAX_FILE_BYTES and new_offset == size:
        _trim(path)


async def signal_loop(send_signal: Callable, path_fn: Optional[Callable] = None) -> None:
    """Background task: poll the EA signal file every second, fan out new blocks.

    `path_fn` is a sync or async callable returning the signal file path (or
    None); defaults to resolve_signal_path (MT5-derived, .env overridable).
    Mirrors scheduler.scheduler_loop: started as an asyncio task in build_app().
    """
    logger.info("Signal broadcast loop started")
    resolver = path_fn or resolve_signal_path
    while True:
        try:
            result = resolver()
            path = await result if asyncio.iscoroutine(result) else result
            if path:
                await _tick(path, send_signal)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the tail die
            logger.exception("Signal broadcast loop error")
        await asyncio.sleep(POLL_INTERVAL_S)

"""Unit tests for bot.signal_broadcast (EA file bridge).

Covers: block parser (complete vs trailing partial), offset seed/resume/reset,
fan-out SQL (absent pref = on, off excluded), per-user send-failure resilience,
and the background loop wiring. No live Telegram traffic, no MT5 terminal.
"""

import asyncio
import contextlib
import os

from bot import signal_broadcast as sb
from bot.signal_broadcast import parse_blocks, read_complete_blocks

SEP = b"\r\n\r\n"


def _block(lines: list[str]) -> bytes:
    """One EA-style block: "[SIGNAL]\\n" + 5 lines + separator (verbatim)."""
    return b"[SIGNAL]\n" + "\n".join(lines).encode() + SEP


MSG5 = ["XAUUSD.pc-M15-2026.08.02 12:15",
        "16 Candles before First CROSS DOWN",
        "Close 2651.42 (BB 62%), SMA50 < SMA20 (BUY)",
        "BB: 2655.20 | 2658.10 | 2601.44",
        "SMA50 2640.05 (within) BB"]


# ---------------------------------------------------------------------------
# parse_blocks — complete vs trailing partial
# ---------------------------------------------------------------------------

def test_parse_empty():
    assert parse_blocks(b"") == ([], 0)


def test_parse_partial_only():
    # A lone partial block (no separator at EOF) = EA mid-write -> wait.
    raw = b"[SIGNAL]\nline1\nline2" 
    assert parse_blocks(raw) == ([], 0)


def test_parse_single_complete_block():
    raw = _block(MSG5)
    blocks, consumed = parse_blocks(raw)
    assert blocks == ["[SIGNAL]\n" + "\n".join(MSG5)]
    assert consumed == len(raw)


def test_parse_utf16le_with_bom():
    # Regression: MQL5 FileWriteString writes UTF-16LE (BOM + doubled bytes)
    # unless FILE_ANSI is set. The old parser hunted ASCII separators, found
    # none, and silently dropped every block. The parser must handle both.
    block_txt = "[SIGNAL]\n" + "\n".join(MSG5) + "\r\n\r\n"
    raw = b"\xff\xfe" + block_txt.encode("utf-16-le")
    blocks, consumed = parse_blocks(raw)
    assert blocks == ["[SIGNAL]\n" + "\n".join(MSG5)]
    assert consumed == len(raw)


def test_parse_utf16le_trailing_partial():
    # UTF-16 partial tail (no separator at EOF) must NOT be consumed.
    partial = "[SIGNAL]\nWIP-line".encode("utf-16-le")
    raw = b"\xff\xfe" + _block(MSG5).decode().encode("utf-16-le") + partial
    blocks, consumed = parse_blocks(raw)
    assert blocks == ["[SIGNAL]\n" + "\n".join(MSG5)]
    assert consumed == len(raw) - len(partial)


def test_parse_two_blocks_in_order():
    raw = _block(MSG5) + _block([f"L{i}" for i in range(5)])
    blocks, consumed = parse_blocks(raw)
    assert len(blocks) == 2
    assert blocks[0] == "[SIGNAL]\n" + "\n".join(MSG5)
    assert blocks[1] == "[SIGNAL]\n" + "\n".join(f"L{i}" for i in range(5))
    assert consumed == len(raw)


def test_parse_complete_then_trailing_partial():
    # Complete block followed by a partial block: only the complete one is
    # consumed; the partial tail is left for the next tick.
    partial = b"[SIGNAL]\nWIP-line"
    raw = _block(MSG5) + partial
    blocks, consumed = parse_blocks(raw)
    assert blocks == ["[SIGNAL]\n" + "\n".join(MSG5)]
    assert consumed == len(_block(MSG5))
    # ... and when the EA finishes, the remainder parses to a full block.
    remainder = partial + b"1\nL2\nL3\nL4\nL5" + SEP
    blocks2, consumed2 = parse_blocks(remainder)
    assert blocks2 == ["[SIGNAL]\nWIP-line1\nL2\nL3\nL4\nL5"]
    assert consumed2 == len(remainder)


def test_parse_skips_adjacent_separators():
    # Robustness: stray empty chunks (e.g. a doubled separator) are dropped,
    # not delivered as empty blocks.
    raw = SEP + _block(MSG5) + SEP + SEP
    blocks, consumed = parse_blocks(raw)
    assert blocks == ["[SIGNAL]\n" + "\n".join(MSG5)]
    assert consumed == len(raw)


def test_parse_block_text_verbatim_utf8():
    raw = b"[SIGNAL]\nXAUUSD.pc-M3-2026.08.02 06:00\n42 Candles before First CROSS UP\n" \
          b"Close 2401.50 (BB 8%), SMA50 > SMA20 (SELL)\nBB: 2405.00 | 2402.00 | 2399.00\n" \
          b"SMA50 2400.05 (within) BB (WEAK)" + SEP
    blocks, consumed = parse_blocks(raw)
    assert blocks[0] == raw[: -len(SEP)].decode("utf-8")
    assert consumed == len(raw)


# ---------------------------------------------------------------------------
# read_complete_blocks — offset resume
# ---------------------------------------------------------------------------

def test_read_from_offset(tmp_path):
    p = tmp_path / "ea_signals.txt"
    b1 = _block(MSG5)
    b2 = _block([f"Y{i}" for i in range(5)])
    p.write_bytes(b1 + b2)

    # Resume from the end of the first block -> only the second is delivered.
    blocks, new_offset, size = read_complete_blocks(str(p), len(b1))
    assert blocks == ["[SIGNAL]\n" + "\n".join(f"Y{i}" for i in range(5))]
    assert new_offset == len(b1) + len(b2)
    assert size == len(b1) + len(b2)

    # Offset at EOF -> nothing new.
    blocks, new_offset, size = read_complete_blocks(str(p), len(b1) + len(b2))
    assert blocks == []
    assert new_offset == size


def test_read_from_offset_ansi_nonzero(tmp_path):
    # ANSI (no BOM): resume from a non-zero offset into the file. The codec is
    # sniffed from the file head, which has no BOM -> utf-8.
    p = tmp_path / "ea_signals.txt"
    b1 = _block(MSG5)
    b2 = _block([f"A{i}" for i in range(5)])
    p.write_bytes(b1 + b2)

    blocks, new_offset, size = read_complete_blocks(str(p), len(b1))
    assert blocks == ["[SIGNAL]\n" + "\n".join(f"A{i}" for i in range(5))]
    assert new_offset == len(b1) + len(b2)
    assert size == new_offset


def test_read_from_offset_utf16_zero(tmp_path):
    # UTF-16LE+BOM read starting at offset 0: the slice itself carries the BOM.
    p = tmp_path / "ea_signals.txt"
    b1 = _block(MSG5)
    raw = b"\xff\xfe" + b1.decode().encode("utf-16-le")
    p.write_bytes(raw)

    blocks, new_offset, size = read_complete_blocks(str(p), 0)
    assert blocks == ["[SIGNAL]\n" + "\n".join(MSG5)]
    assert new_offset == len(raw) == size


def test_read_from_offset_utf16_nonzero(tmp_path):
    # THE regression: the BOM exists only at byte 0 of the FILE. A non-zero
    # offset slice has no BOM, so the codec must come from the file head —
    # decoding the slice as utf-8 (old bug) finds no separator and drops every
    # block. Consumed math must stay byte-exact WITHOUT re-adding a phantom BOM.
    p = tmp_path / "ea_signals.txt"
    b1 = _block(MSG5)
    b2 = _block([f"Z{i}" for i in range(5)])
    raw = b"\xff\xfe" + b1.decode().encode("utf-16-le") + b2.decode().encode("utf-16-le")
    p.write_bytes(raw)

    start = 2 + len(b1.decode().encode("utf-16-le"))   # BOM + block1
    blocks, new_offset, size = read_complete_blocks(str(p), start)
    assert blocks == ["[SIGNAL]\n" + "\n".join(f"Z{i}" for i in range(5))]
    assert new_offset == len(raw) == size

    # ... and the persisted new_offset is exactly EOF, so the NEXT tick sees
    # nothing new (no re-read / no re-send of the BOM bytes as garbage).
    blocks2, new_offset2, _ = read_complete_blocks(str(p), new_offset)
    assert blocks2 == []
    assert new_offset2 == size


def test_parse_utf16_midfile_slice_explicit_codec():
    # Unit-level check of the same contract: a BOM-less mid-file slice must be
    # decoded with the file-sniffed codec; consumed == len(slice) exactly.
    b1 = _block(MSG5)
    b2 = _block([f"Q{i}" for i in range(5)])
    whole = b"\xff\xfe" + b1.decode().encode("utf-16-le") + b2.decode().encode("utf-16-le")
    mid = 2 + len(b1.decode().encode("utf-16-le"))
    slice_ = whole[mid:]                                   # no BOM in the slice
    assert not slice_.startswith(b"\xff\xfe")

    blocks, consumed = parse_blocks(slice_, codec="utf-16-le")
    assert blocks == ["[SIGNAL]\n" + "\n".join(f"Q{i}" for i in range(5))]
    assert consumed == len(slice_)

    # The old auto-detect (codec=None) on this BOM-less slice is the bug: it
    # picks utf-8, finds no separator, and drops everything.
    assert parse_blocks(slice_) == ([], 0)


def test_read_missing_file(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        read_complete_blocks(str(tmp_path / "nope.txt"), 0)


# ---------------------------------------------------------------------------
# _tick — seeding, resume, reset, trim
# ---------------------------------------------------------------------------

def test_seed_never_replays_history(fresh_db, tmp_path):
    p = tmp_path / "ea_signals.txt"
    p.write_bytes(_block(MSG5))            # pre-existing history (pre-bot)

    sent = []
    async def send(chat_id, text):
        sent.append((chat_id, text))

    asyncio.run(sb._tick(str(p), send))

    assert fresh_db.get_meta("ea_signal_offset") == str(len(_block(MSG5)))
    assert sent == []                       # nothing replayed


def test_seed_waits_when_file_missing(fresh_db, tmp_path):
    p = tmp_path / "ea_signals.txt"         # does not exist yet
    async def send(chat_id, text):
        raise AssertionError("must not send")

    asyncio.run(sb._tick(str(p), send))

    # Seeded at 0 (not left unset): the FIRST signal that appears after startup
    # must be delivered, not swallowed as "history" (the old race).
    assert fresh_db.get_meta("ea_signal_offset") == "0"


def test_resume_delivers_new_block(fresh_db, tmp_path):
    p = tmp_path / "ea_signals.txt"
    p.write_bytes(b"")
    fresh_db.ensure_user(111)               # absent pref = on

    sent = []
    async def send(chat_id, text):
        sent.append((chat_id, text))

    asyncio.run(sb._tick(str(p), send))     # seed at 0
    p.write_bytes(_block(MSG5))             # EA appends a signal
    asyncio.run(sb._tick(str(p), send))

    assert sent == [(111, "[SIGNAL]\n" + "\n".join(MSG5))]
    assert fresh_db.get_meta("ea_signal_offset") == str(len(_block(MSG5)))


def test_offset_persisted_after_partial_tail(fresh_db, tmp_path):
    # Complete block + partial tail: only the complete block is consumed and
    # the offset is persisted at its end; the tail is picked up next tick.
    p = tmp_path / "ea_signals.txt"
    partial = b"[SIGNAL]\nWIP"
    p.write_bytes(_block(MSG5) + partial)
    fresh_db.ensure_user(7)
    fresh_db.set_meta("ea_signal_offset", "0")   # already seeded (not first-ever run)

    sent = []
    async def send(chat_id, text):
        sent.append((chat_id, text))

    asyncio.run(sb._tick(str(p), send))
    assert sent == [(7, "[SIGNAL]\n" + "\n".join(MSG5))]
    assert fresh_db.get_meta("ea_signal_offset") == str(len(_block(MSG5)))

    # EA finishes writing; next tick delivers the rest.
    p.write_bytes(_block(MSG5) + partial + b"-x1\nx2\nx3\nx4\nx5" + SEP)
    asyncio.run(sb._tick(str(p), send))
    assert sent[-1] == (7, "[SIGNAL]\nWIP-x1\nx2\nx3\nx4\nx5")
    assert fresh_db.get_meta("ea_signal_offset") == str(len(_block(MSG5) + partial + b"-x1\nx2\nx3\nx4\nx5" + SEP))


def test_offset_reset_when_file_truncated(fresh_db, tmp_path):
    p = tmp_path / "ea_signals.txt"
    p.write_bytes(_block(MSG5))
    fresh_db.set_meta("ea_signal_offset", "9999")   # stale offset (file rotated)

    sent = []
    async def send(chat_id, text):
        sent.append((chat_id, text))

    asyncio.run(sb._tick(str(p), send))

    assert fresh_db.get_meta("ea_signal_offset") == str(len(_block(MSG5)))
    assert sent == []                                # no phantom sends


def test_trim_oversized_fully_consumed_file(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_FILE_BYTES", 32)    # shrink threshold for the test
    p = tmp_path / "ea_signals.txt"
    p.write_bytes(_block(MSG5))                      # > 32 bytes, fully consumed
    fresh_db.ensure_user(1)
    fresh_db.set_meta("ea_signal_offset", "0")       # already seeded (not first-ever run)

    async def send(chat_id, text):
        pass

    asyncio.run(sb._tick(str(p), send))

    assert os.path.getsize(p) == 0                   # trimmed
    assert fresh_db.get_meta("ea_signal_offset") == "0"


# ---------------------------------------------------------------------------
# Fan-out SQL — absent = on, off excluded, on included
# ---------------------------------------------------------------------------

def test_fanout_sql_semantics(fresh_db):
    fresh_db.ensure_user(1)                       # absent pref -> ON
    fresh_db.ensure_user(2)
    fresh_db.set_user_pref(2, "ea_signals", "off")  # OFF -> excluded
    fresh_db.ensure_user(3)
    fresh_db.set_user_pref(3, "ea_signals", "on")   # ON -> included
    fresh_db.ensure_user(4)
    fresh_db.set_user_pref(4, "ea_signals", "banana")  # unknown value -> excluded

    assert sorted(fresh_db.get_ea_signal_recipients()) == [1, 3]


def test_fanout_off_then_back_on(fresh_db):
    fresh_db.ensure_user(1)
    fresh_db.set_user_pref(1, "ea_signals", "off")
    assert fresh_db.get_ea_signal_recipients() == []
    fresh_db.set_user_pref(1, "ea_signals", "on")
    assert fresh_db.get_ea_signal_recipients() == [1]


# ---------------------------------------------------------------------------
# _fan_out — per-user failure isolation
# ---------------------------------------------------------------------------

def test_fanout_skips_blocked_user_without_crash(fresh_db):
    fresh_db.ensure_user(1)
    fresh_db.ensure_user(2)
    fresh_db.set_user_pref(2, "ea_signals", "off")   # not a recipient

    sent = []
    async def send(chat_id, text):
        if chat_id == 1:
            raise RuntimeError("user blocked / deactivated")   # first user fails
        sent.append((chat_id, text))

    asyncio.run(sb._fan_out("opaque block", send))

    assert sent == []                                # no crash, loop lives on


def test_fanout_delivers_to_all_recipients(fresh_db):
    for cid in (11, 22, 33):
        fresh_db.ensure_user(cid)
    fresh_db.set_user_pref(22, "ea_signals", "off")

    sent = []
    async def send(chat_id, text):
        sent.append((chat_id, text))

    asyncio.run(sb._fan_out("opaque block", send))

    assert sent == [(11, "opaque block"), (33, "opaque block")]


# ---------------------------------------------------------------------------
# signal_loop — end-to-end background task wiring
# ---------------------------------------------------------------------------

def test_signal_loop_end_to_end(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "POLL_INTERVAL_S", 0.05)  # fast ticks for the test
    p = tmp_path / "ea_signals.txt"
    p.write_bytes(b"")                                # EA-created, empty
    fresh_db.ensure_user(111)                         # opted in by default

    sent = []
    async def send(chat_id, text):
        sent.append((chat_id, text))

    async def scenario():
        task = asyncio.create_task(sb.signal_loop(send, path_fn=lambda: str(p)))
        try:
            await asyncio.sleep(0.15)                 # seed tick
            p.write_bytes(_block(MSG5))               # EA appends a signal
            await asyncio.sleep(0.3)                  # a few more ticks
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())

    assert sent == [(111, "[SIGNAL]\n" + "\n".join(MSG5))]
    assert fresh_db.get_meta("ea_signal_offset") == str(len(_block(MSG5)))

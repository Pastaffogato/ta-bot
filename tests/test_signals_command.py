"""Unit tests for the /signals command handler (cmd_signals).

Handlers are invoked directly with fake update/context objects — the live bot
is never started, so no real Telegram traffic can be emitted.
"""

import asyncio
import types

from bot import db
from bot.telegram_app import cmd_signals


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, parse_mode=None):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id: int):
        self.effective_chat = types.SimpleNamespace(id=chat_id)
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args


def _run(chat_id: int, args):
    update = FakeUpdate(chat_id)
    asyncio.run(cmd_signals(update, FakeContext(args)))
    return update.message.replies


def test_signals_on(fresh_db):
    replies = _run(42, ["on"])
    assert replies == ["signals: on"]
    assert fresh_db.get_user_prefs(42)["ea_signals"] == "on"
    assert 42 in fresh_db.get_ea_signal_recipients()


def test_signals_off(fresh_db):
    fresh_db.ensure_user(99)                       # another user, still on
    replies = _run(42, ["off"])
    assert replies == ["signals: off"]
    assert fresh_db.get_user_prefs(42)["ea_signals"] == "off"
    assert 42 not in fresh_db.get_ea_signal_recipients()


def test_signals_status_default_on_when_pref_absent(fresh_db):
    # No pref row at all -> default ON (no /start dependency).
    replies = _run(42, ["status"])
    assert replies[0].startswith("signals: on")


def test_signals_status_after_off(fresh_db):
    fresh_db.ensure_user(42)
    fresh_db.set_user_pref(42, "ea_signals", "off")
    replies = _run(42, ["status"])
    assert replies[0].startswith("signals: off")


def test_signals_no_args_shows_status(fresh_db):
    replies = _run(42, [])
    assert replies == ["signals: on"]


def test_signals_unknown_arg_defaults_to_status(fresh_db):
    replies = _run(42, ["banana"])
    assert replies[0].startswith("signals: on")


def test_signals_works_without_start(fresh_db):
    # ensure_user() is called inside the handler — a brand-new chat_id gets
    # created on the fly; no /start prerequisite.
    _run(777, ["on"])
    assert db.get_user(777) is not None

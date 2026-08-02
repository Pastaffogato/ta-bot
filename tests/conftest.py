"""Shared fixtures for ta-bot tests.

All DB tests run against a fresh TEMP database — the real bot.db is never
opened, modified, or deleted. BOT_TOKEN is defaulted before any bot module
import because bot.config requires it at import time (the real .env also
provides it, but tests must not depend on that).
"""

import os

os.environ.setdefault("BOT_TOKEN", "test-token")

import pytest

import bot.db as db


def _reset_conn() -> None:
    """Close and drop the thread-local connection so the next _conn() call
    reconnects to the (possibly monkeypatched) DB_PATH."""
    conn = getattr(db._conn_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._conn_local.conn


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point bot.db at a brand-new temp SQLite file and run init_db().

    Yields the bot.db module. The real bot.db stays untouched.
    """
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    _reset_conn()
    db.init_db()
    yield db
    _reset_conn()

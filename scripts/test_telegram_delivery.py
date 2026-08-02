"""Direct Telegram sendMessage delivery probe for ta-bot.

One-off verification that the BOT_TOKEN in .env plus the chat_id stored in
bot.db can actually reach Telegram. Uses only the stdlib (urllib) — no extra
dependencies. sendMessage does not conflict with getUpdates polling, so this
is safe to run even while another instance polls.

Usage (from repo root):
    .venv/Scripts/python.exe scripts/test_telegram_delivery.py
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # repo root needed for `import bot`

from bot import db  # noqa: E402


def load_token() -> str:
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(f"FATAL: {env_file} not found")
    token = None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("BOT_TOKEN="):
            token = line.partition("=")[2].strip()
            break
    if not token:
        sys.exit("FATAL: BOT_TOKEN missing from .env")
    return token


def resolve_chat_id() -> int:
    """The ONLY real user is 1057071700; read from bot.db rather than hardcode."""
    rows = db._conn().execute("SELECT chat_id FROM users ORDER BY chat_id").fetchall()
    if not rows:
        sys.exit("FATAL: users table is empty — nothing to deliver to")
    ids = [r["chat_id"] for r in rows]
    if len(ids) == 1:
        return ids[0]
    if 1057071700 in ids:               # the known real user (multi-user safety)
        return 1057071700
    sys.exit(f"FATAL: multiple users {ids} and none is the known real user")


def main() -> None:
    token = load_token()
    chat_id = resolve_chat_id()
    print(f"target chat_id: {chat_id} (from bot.db users table)")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"🔧 ta-bot delivery test {ts} — if you see this, the token+chat path works"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()

    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        print(f"FAILED (HTTP {e.code}): {err}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"FAILED (network): {e}")
        sys.exit(1)

    if body.get("ok") is not True:
        print(f"FAILED (HTTP {status}): {json.dumps(body, ensure_ascii=False)}")
        sys.exit(1)

    msg_id = body["result"].get("message_id")
    print(f"OK  HTTP {status} message_id={msg_id} chat_id={chat_id}")
    print(f"    text: {text}")


if __name__ == "__main__":
    main()

"""ta-bot: Telegram Trading Alert Bot.

Usage:  python -m bot
"""

import asyncio
import logging
import sys

from bot import config, db, mt5_data
from bot.telegram_app import build_app

logger = logging.getLogger(__name__)


def main() -> None:
    config.setup_logging()
    config.validate()

    logger.info("Starting ta-bot...")

    # Initialize database
    db.init_db()

    # Initialize MT5
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ok = loop.run_until_complete(mt5_data.init(config.MT5_TERMINAL_PATH))
    if not ok:
        logger.error("MT5 initialization failed. Exiting.")
        sys.exit(1)

    # Health check
    health = loop.run_until_complete(mt5_data.health())
    if not health["connected"]:
        logger.error("MT5 not connected to broker. Exiting.")
        loop.run_until_complete(mt5_data.shutdown())
        sys.exit(1)

    logger.info("MT5 ok — server %s, account %s", health["server"], health["account"])

    # Build and run Telegram bot
    app = build_app()
    logger.info("Bot starting — polling...")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(mt5_data.shutdown())
        sys.exit(0)
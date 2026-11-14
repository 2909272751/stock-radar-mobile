from __future__ import annotations

import asyncio
import logging
import signal

from .config import Settings
from .database import Database
from .monitoring import MonitorService
from .quote_provider import build_quote_provider
from .repository import MonitorRepository


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main() -> None:
    settings = Settings.from_env()
    settings.ensure_directories()

    database = Database(settings.sqlite_path)
    repository = MonitorRepository(database)
    quote_provider = build_quote_provider(settings.quote_provider)
    monitor_service = MonitorService(repository, quote_provider, settings.poll_interval_seconds)

    database.init_db()
    await monitor_service.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.getLogger(__name__).info("Monitor worker stopping")
    finally:
        await monitor_service.stop()


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import logging
import signal

from .database import Database
from .notifier import NotificationTarget, Notifier
from .repository import MonitorRepository
from .config import Settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(settings.sqlite_path)
    database.init_db()
    repository = MonitorRepository(database)
    notifier = Notifier()
    await notifier.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    try:
        while not stop_event.is_set():
            jobs = await asyncio.to_thread(repository.claim_notification_jobs, 20)
            if not jobs:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            notification_settings = await asyncio.to_thread(repository.get_notification_settings)
            target = NotificationTarget(
                channel=notification_settings.channel,
                webhook_url=notification_settings.webhook_url,
                bark_url=notification_settings.bark_url,
            )
            for job in jobs:
                title = await asyncio.to_thread(repository.get_job_title, job.id)
                try:
                    await notifier.send(target, title, job.message)
                except Exception as exc:
                    attempt = job.attempt_count + 1
                    error = str(exc)[:800]
                    if attempt >= job.max_attempts:
                        await asyncio.to_thread(repository.mark_job_failed, job.id, job.alert_event_id, attempt, error)
                        logger.warning("Notification failed permanently job=%s: %s", job.id, error)
                    else:
                        delay = min(30 * (2 ** (attempt - 1)), 300)
                        await asyncio.to_thread(repository.mark_job_retry, job.id, job.alert_event_id, attempt, error, delay)
                        logger.info("Notification retry scheduled job=%s attempt=%s error=%s", job.id, attempt, error)
                else:
                    await asyncio.to_thread(repository.mark_job_sent, job.id, job.alert_event_id, job.attempt_count + 1)
                    logger.info("Notification sent job=%s", job.id)
    finally:
        await notifier.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

"""Automatic daily backup (v1.34.0).

The API takes a full database backup into R2 every day at BACKUP_SCHEDULE_TIME (default 00:00), read in APP_TIMEZONE
(Asia/Kolkata), so by default it runs at midnight IST. It is the same backup as POST /admin/backup/cloud: the same
pg_dump, the same encryption (when BACKUP_ENCRYPTION_KEY is set), the same R2 copy and the same pruning
(KEEP_CLOUD_BACKUPS) -- there is no separate password, token or cron job.

Several uvicorn workers each run this loop. Only one of them may take the backup:
  * a Postgres advisory lock is held while a scheduled run is in progress (a worker that cannot get it skips the run),
  * and a worker that gets it still skips the run when a scheduled backup was already logged in the last 6 hours
    (a fast backup could finish before a slower worker wakes up).

A failed backup is retried BACKUP_SCHEDULE_RETRIES times, BACKUP_SCHEDULE_RETRY_MINUTES apart. Both outcomes go to the
platform admin's audit log ("Scheduler" as the actor), where the admin app's history screen shows them.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

from app.config import settings
from app.db import get_pool
from app.utils.audit import log_event
from app.utils.dates import app_tz

logger = logging.getLogger("app")

_SCHEDULE_LOCK_KEY = 727100  # bk._LOCK_KEY (727099) is the "one backup / restore at a time" lock
_RECENT_HOURS = 6
_MAX_SLEEP_SECONDS = 300  # sleep in short steps so a clock change or a long suspend can't make us miss the time
SCHEDULER_ACTOR = {"id": None, "name": "Scheduler", "role": "ADMIN"}


def next_run_after(now: datetime, hour: int, minute: int) -> datetime:
    """The next moment strictly after `now` whose wall-clock time in now's own time zone is hour:minute."""
    tz = now.tzinfo
    local = now.astimezone(tz) if tz else now
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = (local + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate


def _now() -> datetime:
    return datetime.now(app_tz())


async def _sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - _now()).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _MAX_SLEEP_SECONDS))


async def _scheduled_backup_done_recently(conn) -> bool:
    return bool(await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM audit_logs
                           WHERE action = 'BACKUP' AND details->>'trigger' = 'scheduled'
                             AND created_at > now() - make_interval(hours => $1))""",
        _RECENT_HOURS,
    ))


async def run_scheduled_backup() -> str:
    """Takes one scheduled backup. Returns "done", "skipped" (another worker has it / it was just done) or raises."""
    from app.routers import admin  # late import: the router module imports a lot, and this module is imported by main

    pool = get_pool()
    async with pool.acquire() as lock_conn:
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", _SCHEDULE_LOCK_KEY):
            return "skipped"
        try:
            if await _scheduled_backup_done_recently(lock_conn):
                return "skipped"
            path, name, encrypted = await admin._make_backup_file()
            size = os.path.getsize(path)
            admin._remove(path)
            await log_event(
                SCHEDULER_ACTOR, "BACKUP", "DATABASE",
                summary="Scheduled daily backup saved to cloud storage",
                details={"trigger": "scheduled", "stored_in": "r2", "name": name, "encrypted": encrypted, "size_bytes": size},
            )
            logger.info("Scheduled backup saved: %s (%s bytes)", name, size)
            return "done"
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", _SCHEDULE_LOCK_KEY)


async def _run_with_retries() -> None:
    attempts = 1 + settings.backup_schedule_retries
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await run_scheduled_backup()
            if result == "skipped":
                logger.info("Scheduled backup skipped: another worker is taking it, or it was just done.")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # BackupError, HTTPException (R2), pg_dump missing... -- try again later
            last_error = exc
            logger.error("Scheduled backup failed (attempt %s of %s): %r", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(settings.backup_schedule_retry_minutes * 60)
    await log_event(
        SCHEDULER_ACTOR, "BACKUP_FAILED", "DATABASE",
        summary=f"Scheduled daily backup FAILED after {attempts} attempt(s)",
        details={"trigger": "scheduled", "error": str(getattr(last_error, "detail", last_error))[:500]},
    )


async def _loop() -> None:
    hour, minute = settings.backup_schedule_hm
    logger.info("Daily backup scheduled for %02d:%02d %s.", hour, minute, settings.app_timezone)
    while True:
        target = next_run_after(_now(), hour, minute)
        await _sleep_until(target)
        try:
            await _run_with_retries()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled backup loop error")
        # Move past the target second so one run can't fire twice for the same minute.
        await asyncio.sleep(1)


def start_scheduler() -> asyncio.Task | None:
    """Starts the daily backup loop on the running event loop (called from the app's lifespan)."""
    if not settings.backup_schedule_enabled:
        logger.info("Automatic daily backup is off (BACKUP_SCHEDULE_ENABLED=false).")
        return None
    return asyncio.create_task(_loop(), name="daily-backup-scheduler")


async def stop_scheduler(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

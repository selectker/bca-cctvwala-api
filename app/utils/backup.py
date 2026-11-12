"""Whole-database backup / restore with PostgreSQL's own pg_dump and pg_restore (v1.14.0).

A backup is a pg_dump "custom format" archive of the `public` schema: table definitions, indexes, constraints,
triggers, functions, sequence values AND the data. So a backup can be restored into a fresh database that only has
the app's schema (and one platform admin, to be able to sign in) -- everything in it is replaced by the backup.

    backup   pg_dump --format=custom --schema=public --no-owner --no-privileges
             --exclude-table-data=auth_sessions --exclude-table-data=login_failures
    restore  pg_restore --clean --if-exists --single-transaction --exit-on-error --no-owner --no-privileges
             --use-list=<archive contents without the schema / extension entries>

* Restore runs in ONE transaction: it either fully replaces the database or changes nothing.
* The schema and extension entries are skipped on restore: the `public` schema and the extensions (pgcrypto,
  pg_trgm) belong to the database, and dropping them would fail on RDS.
* auth_sessions (live sign-ins) and login_failures (lock-out counters) keep their table definitions in the backup but
  not their rows: a backup must not hold working session handles, and a restore must not bring back sign-ins that were
  ended after it was taken. After a restore every user signs in again.
* audit_logs IS part of the backup and is replaced like everything else. The RESTORE entry itself is written
  after the restore, so it is the newest line of the restored log.
* pg_dump / pg_restore must be installed on the API server, and their major version must be the same as or newer
  than the server's (PG_DUMP_PATH / PG_RESTORE_PATH can point at a specific version).
"""
import asyncio
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from app.config import settings
from app.utils.dates import now_local

logger = logging.getLogger("app")

PGDMP_MAGIC = b"PGDMP"
SNAPSHOT_RE = re.compile(r"^pre-restore-\d{8}-\d{6}(-\d+)?\.dump(\.enc)?$")
BACKUP_NAME_RE = re.compile(r"^bcacctvwala-backup-\d{8}-\d{6}\.dump(\.enc)?$")
# Tables whose rows are never part of a backup (their definitions still are).
EXCLUDED_TABLE_DATA = ("auth_sessions", "login_failures")
# A backup that lacks these is not one of ours: refuse it before anything is touched.
REQUIRED_TABLES = ("users", "audit_logs", "store_profile")
# Extensions the schema needs; created if missing before a restore (a no-op when they exist).
EXTENSIONS = ("pgcrypto", "pg_trgm")
_LOCK_KEY = 727099

_SKIP_ON_RESTORE = re.compile(r"^\d+; \d+ \d+ (SCHEMA|EXTENSION) |^\d+; \d+ \d+ COMMENT - (SCHEMA|EXTENSION) ")
_TABLE_ENTRY = re.compile(r"^\d+; \d+ \d+ TABLE public (\S+) ", re.MULTILINE)
_CREATED = re.compile(r"^;\s+Archive created at (.+)$", re.MULTILINE)


class BackupError(Exception):
    """A problem with the backup file or the server's backup tools, shown to the admin. `status` is the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


# --------------------------------------------------------------------------- running the PostgreSQL tools
def _tool_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["TZ"] = "UTC"  # the "Archive created at" line of a dump is then in UTC
    env["PGPASSWORD"] = settings.db_password  # never on the command line, where `ps` would show it
    env["PGCONNECT_TIMEOUT"] = "15"
    if settings.db_ssl:
        env["PGSSLMODE"] = "verify-full"
        env["PGSSLROOTCERT"] = settings.db_ssl_ca_file.strip()
    else:
        env["PGSSLMODE"] = "disable"
    return env


def _connection_args() -> list[str]:
    return ["-w", "-h", settings.db_host, "-p", str(settings.db_port), "-U", settings.db_user, "-d", settings.db_name]


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join([ln for ln in text.strip().splitlines() if ln.strip()][-lines:])


async def _run(binary: str, args: list[str], what: str, failure_status: int = 500) -> str:
    """Runs a PostgreSQL tool and returns its stdout. Raises BackupError if it is missing, hangs or fails; the full
    stderr goes to the server log, the last lines to the admin."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args, env=_tool_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise BackupError(
            f"{binary} is not installed on the server. Install the PostgreSQL client tools (postgresql-client, same "
            "major version as the database or newer) or set PG_DUMP_PATH / PG_RESTORE_PATH.", 500,
        )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.backup_timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackupError(f"{what} took longer than {settings.backup_timeout_seconds} seconds and was stopped", 500)
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if proc.returncode != 0:
        logger.error("%s failed (exit %s): %s", binary, proc.returncode, stderr.strip())
        raise BackupError(f"{what} failed: {_tail(stderr) or 'exit code ' + str(proc.returncode)}", failure_status)
    return stdout


# --------------------------------------------------------------------------- backup
async def create_dump_file(path: str) -> None:
    """Writes a consistent (single-snapshot) pg_dump archive of the whole database to `path`."""
    args = [
        "--format=custom", "--compress=6", "--schema=public", "--no-owner", "--no-privileges",
        *[f"--exclude-table-data={t}" for t in EXCLUDED_TABLE_DATA],
        "--file", path, *_connection_args(),
    ]
    await _run(settings.pg_dump_path, args, "The backup")


def new_backup_name(encrypted: bool) -> str:
    """The time in the name is the store's local time (APP_TIMEZONE, IST by default), so a midnight backup says 0000."""
    return f"bcacctvwala-backup-{now_local().strftime('%Y%m%d-%H%M%S')}.dump" + (".enc" if encrypted else "")


def new_snapshot_name(encrypted: bool) -> str:
    """Safety snapshots are stored in R2 under snapshots/<name>. Microseconds keep two snapshots in one second apart."""
    now = now_local()
    return f"pre-restore-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond}.dump" + (".enc" if encrypted else "")


# --------------------------------------------------------------------------- restore
def is_dump(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(len(PGDMP_MAGIC)) == PGDMP_MAGIC


async def inspect_dump(path: str) -> dict:
    """Checks that `path` is a readable pg_dump archive of this app, without touching the database.
    Returns {"list": <pg_restore --list text>, "created_at": <str|None>}."""
    if not await asyncio.to_thread(is_dump, path):
        raise BackupError("This is not a BCAcctvWala backup file")
    try:
        listing = await _run(settings.pg_restore_path, ["--list", path], "Reading the backup", failure_status=400)
    except BackupError as exc:
        if exc.status == 400:
            raise BackupError("The backup file is damaged, or was made by a newer PostgreSQL than this server's pg_restore")
        raise
    tables = set(_TABLE_ENTRY.findall(listing))
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise BackupError(f"This is not a BCAcctvWala backup (it has no {missing[0]} table)")
    return {"list": listing, "created_at": _created_at(listing)}


def _created_at(listing: str) -> str | None:
    """The time the archive was made, as an ISO string (UTC for backups made by this server)."""
    m = _CREATED.search(listing)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return datetime.strptime(" ".join(raw.split()[:2]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return raw


@asynccontextmanager
async def exclusive(pool):
    """Only one backup or restore at a time, across all workers (a Postgres advisory lock held on one connection)."""
    conn = await pool.acquire()
    try:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK_KEY):
            raise BackupError("Another backup or restore is already running. Try again in a moment.", 409)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    finally:
        await pool.release(conn)


async def restore_dump_file(pool, path: str, listing: str) -> None:
    """Replaces everything in the database with the archive, atomically. `listing` comes from inspect_dump."""
    async with pool.acquire() as conn:
        for ext in EXTENSIONS:
            try:
                await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {_q(ext)}")
            except Exception as exc:  # the restore will then say precisely what is missing
                logger.warning("Could not create extension %s before the restore: %s", ext, exc)

    fd, list_path = tempfile.mkstemp(suffix=".list")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in listing.splitlines():
                if not _SKIP_ON_RESTORE.match(line):
                    f.write(line + "\n")
        args = [
            "--clean", "--if-exists", "--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
            "--use-list", list_path, *_connection_args(), path,
        ]
        await _run(settings.pg_restore_path, args, "The restore")
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    # Tables were dropped and recreated: connections still hold prepared statements for the old ones.
    await pool.expire_connections()


# --------------------------------------------------------------------------- what is in the database
async def table_counts(conn) -> dict[str, int]:
    """Row counts of every table a backup carries data for (live sign-ins are not part of a backup)."""
    names = [
        r["table_name"]
        for r in await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
        )
        if r["table_name"] not in EXCLUDED_TABLE_DATA
    ]
    return {t: await conn.fetchval(f"SELECT COUNT(*) FROM {_q(t)}") for t in names}


async def missing_after_restore(conn) -> list[str]:
    """Tables the running app needs that a restored (older) backup did not bring along."""
    needed = ("users", "auth_sessions", "login_failures", "audit_logs", "expenses")
    present = {
        r["table_name"]
        for r in await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    }
    return [t for t in needed if t not in present]

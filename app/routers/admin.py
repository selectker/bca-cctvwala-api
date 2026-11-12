"""Platform-admin-only endpoints: full database backup (pg_dump) and restore (pg_restore)."""
import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.config import settings
from app.db import get_pool
from app.security import require_platform_admin
from app.utils import backup as bk
from app.utils import crypto_backup as crypto
from app.utils import storage
from app.utils.audit import AuditFilters, fetch_logs, log_event
from app.utils.pagination import Page, paged_response

logger = logging.getLogger("app")

router = APIRouter(prefix="/admin", tags=["platform-admin"], dependencies=[Depends(require_platform_admin)])

CONFIRM_WORD = "RESTORE"
BACKUP_PREFIX = "backups/"
SNAPSHOT_PREFIX = "snapshots/"
BACKUP_NAME_RE = bk.BACKUP_NAME_RE


def _bad_request(exc: bk.BackupError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@asynccontextmanager
async def _errors():
    """Backup problems (bad file, missing pg_dump, another run in progress...) become HTTP errors with a clear message."""
    try:
        yield
    except bk.BackupError as exc:
        raise _bad_request(exc)


def _storage_error(exc: Exception) -> HTTPException:
    # The raw error carries the storage endpoint (with the Cloudflare account id), the bucket and request ids: log it,
    # but tell the client only that storage failed.
    logger.error("Cloud storage (R2) request failed", exc_info=exc)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Cloud storage (R2) is unavailable or not set up correctly. The server log has the details.",
    )


async def _list(prefix: str, name_re) -> list[dict]:
    try:
        items = await asyncio.to_thread(storage.list_objects, prefix)
    except Exception as exc:
        raise _storage_error(exc)
    return [i for i in items if name_re.match(i["name"])]


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _dump_to_temp() -> tuple[str, bool]:
    """pg_dump of the whole database into a temp file; encrypted when BACKUP_ENCRYPTION_KEY is set (the plain file
    then exists only while it is being encrypted). Returns (path, encrypted); the caller removes the file."""
    encrypted = bool(settings.backup_keys)
    fd, plain = tempfile.mkstemp(suffix=".dump")
    os.close(fd)
    try:
        await bk.create_dump_file(plain)
        if not encrypted:
            return plain, False
        fd, enc = tempfile.mkstemp(suffix=".dump.enc")
        os.close(fd)
        try:
            await asyncio.to_thread(crypto.encrypt_file, plain, enc)
        except Exception:
            _remove(enc)
            raise
        _remove(plain)
        return enc, True
    except Exception:
        _remove(plain)
        raise


async def _make_backup_file() -> tuple[str, str, bool]:
    """Takes a backup into a temp file and stores a copy in R2 (backups/). Returns (temp path, name, encrypted)."""
    async with bk.exclusive(get_pool()):
        path, encrypted = await _dump_to_temp()
        name = bk.new_backup_name(encrypted)
        try:
            await asyncio.to_thread(storage.put_file, BACKUP_PREFIX + name, path, "application/octet-stream")
            await asyncio.to_thread(storage.prune, BACKUP_PREFIX, settings.keep_cloud_backups)
        except Exception as exc:
            _remove(path)
            raise _storage_error(exc)
    return path, name, encrypted


@router.get("/summary")
async def summary():
    """What a backup contains (row counts per table), database size, and the backups / snapshots kept in R2."""
    pool = get_pool()
    async with pool.acquire() as conn:
        counts = await bk.table_counts(conn)
        size = await conn.fetchval("SELECT pg_database_size(current_database())")
    return {
        "tables": counts,
        "total_rows": sum(counts.values()),
        "database_size_bytes": size,
        "snapshots": await _list(SNAPSHOT_PREFIX, bk.SNAPSHOT_RE),
        "cloud_backups": await _list(BACKUP_PREFIX, BACKUP_NAME_RE),
    }


@router.get("/backup")
async def download_backup(request: Request, admin=Depends(require_platform_admin)):
    """Streams a complete, consistent pg_dump backup of the database as one file (.dump, or .dump.enc when
    BACKUP_ENCRYPTION_KEY is set). A copy is also kept in R2 (backups/); if that upload fails the call fails, so a
    backup is never silently missing."""
    async with _errors():
        path, name, encrypted = await _make_backup_file()
    await log_event(admin, "BACKUP", "DATABASE", request=request, details={"stored_in": "r2", "name": name, "encrypted": encrypted})
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
        background=BackgroundTask(os.remove, path),
        headers={"X-Backup-Stored": f"r2:{BACKUP_PREFIX}{name}"},
    )


@router.post("/backup/cloud")
async def backup_to_cloud(request: Request, admin=Depends(require_platform_admin)):
    """Takes a backup and stores it in R2 without downloading it (handy for a nightly cron job)."""
    async with _errors():
        path, name, encrypted = await _make_backup_file()
    size = os.path.getsize(path)
    _remove(path)
    await log_event(admin, "BACKUP", "DATABASE", request=request, details={"stored_in": "r2", "name": name, "encrypted": encrypted})
    return {"name": name, "key": BACKUP_PREFIX + name, "size_bytes": size}


@router.get("/backups")
async def cloud_backups():
    """Backups stored in R2, newest first."""
    return await _list(BACKUP_PREFIX, BACKUP_NAME_RE)


def _require_confirm(confirm: str) -> None:
    if confirm != CONFIRM_WORD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Restoring replaces ALL data. Pass confirm={CONFIRM_WORD} to proceed",
        )


async def _open_backup(path: str) -> tuple[str, bool]:
    """Returns (path of a plain pg_dump archive, is_temporary). An encrypted file is decrypted (and authenticated)
    into a temp file the caller must remove; a plain archive is used as it is."""
    if await asyncio.to_thread(crypto.is_encrypted, path):
        if not settings.backup_keys:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This backup is encrypted, but this server has no BACKUP_ENCRYPTION_KEY. Set the key the backup was made with.",
            )
        fd, plain = tempfile.mkstemp(suffix=".dump")
        os.close(fd)
        try:
            await asyncio.to_thread(crypto.decrypt_file, path, plain)
        except bk.BackupError as exc:
            _remove(plain)
            raise _bad_request(exc)
        except Exception:
            _remove(plain)
            raise
        return plain, True
    return path, False


async def _save_snapshot() -> str:
    """Safety snapshot of the CURRENT data, written to R2 before a restore. If it can't be saved, nothing is restored."""
    path, encrypted = await _dump_to_temp()
    name = bk.new_snapshot_name(encrypted)
    try:
        await asyncio.to_thread(storage.put_file, SNAPSHOT_PREFIX + name, path, "application/octet-stream")
    except Exception as exc:
        logger.error("Could not save the pre-restore safety snapshot to R2", exc_info=exc)
        raise HTTPException(
            status_code=502,
            detail="Couldn't save the safety snapshot to cloud storage, so nothing was changed. The server log has the details.",
        )
    finally:
        _remove(path)
    return name


async def _restore_from_path(path: str) -> dict:
    """Decrypts if needed, validates, saves a safety snapshot of the CURRENT data to R2, then restores (atomic)."""
    pool = get_pool()
    async with _errors():
        async with bk.exclusive(pool):
            plain, temporary = await _open_backup(path)
            try:
                info = await bk.inspect_dump(plain)  # reject bad files before touching anything
                snapshot_name = await _save_snapshot()
                await bk.restore_dump_file(pool, plain, info["list"])
            finally:
                if temporary:
                    _remove(plain)
            async with pool.acquire() as conn:
                counts = await bk.table_counts(conn)
                missing = await bk.missing_after_restore(conn)
    try:
        await asyncio.to_thread(storage.prune, SNAPSHOT_PREFIX, settings.keep_snapshots)
    except Exception as exc:  # the restore itself succeeded
        logger.error("Could not prune old safety snapshots", exc_info=exc)
    result = {
        "restored": counts,
        "backup_created_at": info["created_at"],
        "backup_app_version": None,
        "snapshot": snapshot_name,
    }
    if missing:
        result["warning"] = (
            "The backup comes from an older version and has no " + ", ".join(missing) +
            ". Run `alembic upgrade head` (or the db/migrations scripts) on the server before using the app."
        )
    return result


async def _restore_stored(prefix: str, name: str) -> dict:
    """Downloads a file kept in R2 to a temp file and restores from it."""
    fd, tmp = tempfile.mkstemp(suffix=".dump")
    os.close(fd)
    try:
        try:
            await asyncio.to_thread(storage.download_file, prefix + name, tmp)
        except storage.ObjectMissing:
            raise HTTPException(status_code=404, detail="Unknown backup")
        except Exception as exc:
            raise _storage_error(exc)
        return await _restore_from_path(tmp)
    finally:
        _remove(tmp)


@router.post("/restore")
async def restore(request: Request, confirm: str = Query(default=""), admin=Depends(require_platform_admin)):
    """Replace the WHOLE database with an uploaded backup (raw request body = the .dump / .dump.enc file).
    Everything happens in one transaction; a safety snapshot of the current data is saved to R2 first. Works on a
    fresh database: sign in as the platform admin created by bootstrap-admin, upload the backup, sign in again."""
    _require_confirm(confirm)
    limit = settings.max_restore_mb * 1024 * 1024
    fd, path = tempfile.mkstemp(suffix=".dump")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"Backup file is larger than {settings.max_restore_mb} MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="No backup file was sent")
        result = await _restore_from_path(path)
        # The audit log comes from the backup: this entry is added after the restore, so it is the newest line.
        await log_event(
            admin, "RESTORE", "DATABASE", request=request,
            details={"upload_bytes": size, "safety_snapshot": result.get("snapshot")},
        )
        return result
    finally:
        _remove(path)


@router.get("/snapshots")
async def snapshots():
    return await _list(SNAPSHOT_PREFIX, bk.SNAPSHOT_RE)


@router.post("/snapshots/{name}/restore")
async def restore_snapshot(name: str, request: Request, confirm: str = Query(default=""), admin=Depends(require_platform_admin)):
    """Undo a restore: put back one of the automatic safety snapshots kept in R2."""
    _require_confirm(confirm)
    if not bk.SNAPSHOT_RE.match(name):
        raise HTTPException(status_code=404, detail="Unknown snapshot")
    result = await _restore_stored(SNAPSHOT_PREFIX, name)
    await log_event(
        admin, "RESTORE_SNAPSHOT", "DATABASE", request=request,
        details={"snapshot": name, "safety_snapshot": result.get("snapshot")},
    )
    return result


@router.post("/backups/{name}/restore")
async def restore_cloud_backup(name: str, request: Request, confirm: str = Query(default=""), admin=Depends(require_platform_admin)):
    """Restore one of the backups stored in R2 (no need to upload the file again)."""
    _require_confirm(confirm)
    if not BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="Unknown backup")
    result = await _restore_stored(BACKUP_PREFIX, name)
    await log_event(
        admin, "RESTORE", "DATABASE", request=request,
        details={"cloud_backup": name, "safety_snapshot": result.get("snapshot")},
    )
    return result


@router.get("/audit-logs")
async def admin_audit_logs(filters: AuditFilters = Depends(), page: Page = Depends()):
    """The platform admin's own history (sign-ins, backups, restores), newest first. Business activity is not
    shown here -- that is the owner's GET /audit-logs. Same filters and paging."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await fetch_logs(conn, scope="admin", filters=filters, limit=page.limit, offset=page.offset)
    return paged_response(rows)

"""File storage for ticket photos and backups: Cloudflare R2 only (nothing is written to the server's disk).

R2 speaks the S3 API, so this uses boto3 against https://<account>.r2.cloudflarestorage.com.
Every function here is BLOCKING; async code calls them through asyncio.to_thread.

Object layout in the bucket:
    service_tickets/<ticket id>/<random>.<ext>   ticket photos (the key is what the DB stores)
    backups/bcacctvwala-backup-<stamp>.bca.enc   backups the platform admin downloaded        } in R2_BACKUP_BUCKET
    snapshots/pre-restore-<stamp>.bca.enc        automatic safety snapshots before a restore  } (default: R2_BUCKET)
"""
from __future__ import annotations

from datetime import timezone

from app.config import settings

_client_cache = None


def _client():
    global _client_cache
    if _client_cache is None:
        import boto3
        from botocore.config import Config

        endpoint = settings.r2_endpoint_url or f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"
        _client_cache = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"},
                          connect_timeout=10, read_timeout=60),
        )
    return _client_cache


class ObjectMissing(Exception):
    pass


# ----------------------------------------------------------------------------- bytes (photos)
def put_bytes(key: str, data: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.r2_bucket, Key=key, Body=data, ContentType=content_type,
        CacheControl="private, max-age=31536000, immutable",  # a stored photo never changes
    )


def get_bytes(key: str) -> bytes:
    client = _client()
    try:
        return client.get_object(Bucket=settings.r2_bucket, Key=key)["Body"].read()
    except client.exceptions.NoSuchKey:
        raise ObjectMissing(key)


def delete_keys(keys: list[str]) -> None:
    """Best effort (used to clean up photos of a rolled-back upload)."""
    for key in keys:
        try:
            _client().delete_object(Bucket=settings.r2_bucket, Key=key)
        except Exception:
            pass


# ----------------------------------------------------------------------------- files (backups)
def _backup_bucket() -> str:
    return settings.r2_backup_bucket or settings.r2_bucket


def put_file(key: str, path: str, content_type: str = "application/gzip") -> None:
    _client().upload_file(path, _backup_bucket(), key, ExtraArgs={"ContentType": content_type})


def download_file(key: str, path: str) -> None:
    try:
        _client().download_file(_backup_bucket(), key, path)
    except Exception as exc:  # botocore ClientError 404 -> "Not Found"
        if "404" in str(exc) or "Not Found" in str(exc) or "NoSuchKey" in str(exc):
            raise ObjectMissing(key)
        raise


def list_objects(prefix: str) -> list[dict]:
    """[{name, size_bytes, created_at}] newest first; name is the key without the prefix."""
    out: list[dict] = []
    paginator = _client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_backup_bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix):]
            if not name or "/" in name:
                continue
            modified = obj["LastModified"]
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            out.append({"name": name, "size_bytes": obj["Size"], "created_at": modified.isoformat(),
                        "_ts": modified.timestamp()})
    out.sort(key=lambda o: (o["_ts"], o["name"]), reverse=True)
    for o in out:
        del o["_ts"]
    return out


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=_backup_bucket(), Key=key)


def prune(prefix: str, keep: int) -> None:
    if keep <= 0:
        return
    for old in list_objects(prefix)[keep:]:
        try:
            delete_object(prefix + old["name"])
        except Exception:
            pass

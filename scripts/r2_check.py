"""Checks the Cloudflare R2 settings with the same client the API uses.
Run:  docker exec -e PYTHONPATH=/app bcacctvwala-api python scripts/r2_check.py
Writes, reads and deletes one tiny object in each bucket, and lists the backup prefix.
"""
import sys
import uuid

from app.config import settings
from app.utils import storage

ok = True


def step(label, fn):
    global ok
    try:
        out = fn()
        print(f"  ok    {label}" + (f"  ({out})" if out else ""))
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")


client = storage._client()
photo_bucket = settings.r2_bucket
backup_bucket = settings.r2_backup_bucket or settings.r2_bucket
key = f"healthcheck/{uuid.uuid4().hex}.txt"

print(f"endpoint : {settings.r2_endpoint_url or f'https://{settings.r2_account_id}.r2.cloudflarestorage.com'}")
print(f"buckets  : files={photo_bucket}  backups={backup_bucket}")

for name in sorted({photo_bucket, backup_bucket}):
    print(f"bucket {name}")
    step("write", lambda: client.put_object(Bucket=name, Key=key, Body=b"ok", ContentType="text/plain"))
    step("read", lambda: client.get_object(Bucket=name, Key=key)["Body"].read().decode())
    step("list", lambda: f"{client.list_objects_v2(Bucket=name, MaxKeys=1).get('KeyCount', 0)} key(s) seen")
    step("delete", lambda: client.delete_object(Bucket=name, Key=key))

print("RESULT:", "R2 is ready" if ok else "R2 NOT ready")
sys.exit(0 if ok else 1)

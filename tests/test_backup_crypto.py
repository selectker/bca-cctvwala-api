"""Backup encryption (optional since v1.14.0): round trip, tampering, truncation, wrong key, key rotation, how the
restore path opens plain and encrypted pg_dump files. No database."""
import asyncio
import os

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers import admin as admin_router
from app.utils import backup as bk
from app.utils import crypto_backup as cb

KEY_A = "a" * 40
KEY_B = "b" * 40
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "backup_encryption_key", KEY_A)


def make(tmp_path, data: bytes):
    src = str(tmp_path / "plain.bin")
    enc = str(tmp_path / "enc.bin")
    open(src, "wb").write(data)
    cb.encrypt_file(src, enc)
    return src, enc


def read(path):
    return open(path, "rb").read()


@pytest.mark.parametrize("size", [0, 1, MB - 1, MB, MB + 1, 2 * MB + 12345])
def test_round_trip_across_chunk_boundaries(tmp_path, size):
    data = os.urandom(size)
    _, enc = make(tmp_path, data)
    assert read(enc).startswith(cb.MAGIC) and cb.is_encrypted(enc)
    out = str(tmp_path / "out.bin")
    cb.decrypt_file(enc, out)
    assert read(out) == data


def test_ciphertext_is_unreadable_and_different_every_time(tmp_path):
    data = b"customer: Secret Name, bank: 12345678\n" * 100
    src, enc1 = make(tmp_path, data)
    enc2 = str(tmp_path / "enc2.bin")
    cb.encrypt_file(src, enc2)
    assert b"Secret Name" not in read(enc1) and read(enc1) != read(enc2)  # random salt per file


def test_any_change_is_detected(tmp_path):
    _, enc = make(tmp_path, os.urandom(3 * MB))
    raw = read(enc)
    out = str(tmp_path / "out.bin")
    for position in (len(cb.MAGIC) + 2, 40, len(raw) // 2, len(raw) - 1):  # salt, first record, middle, tag
        changed = bytearray(raw)
        changed[position] ^= 0x01
        open(enc, "wb").write(bytes(changed))
        with pytest.raises(bk.BackupError):
            cb.decrypt_file(enc, out)


def test_truncation_is_detected_even_on_a_record_boundary(tmp_path):
    _, enc = make(tmp_path, os.urandom(3 * MB))
    raw = read(enc)
    record = 4 + MB + 16
    out = str(tmp_path / "out.bin")
    for cut in (len(raw) - 1, len(cb.MAGIC) + cb.SALT_LEN + record, len(cb.MAGIC) + cb.SALT_LEN + 2 * record, 10, 0):
        open(enc, "wb").write(raw[:cut])
        with pytest.raises(bk.BackupError):
            cb.decrypt_file(enc, out)


def test_records_cannot_be_reordered(tmp_path):
    _, enc = make(tmp_path, os.urandom(3 * MB))
    raw = read(enc)
    head = len(cb.MAGIC) + cb.SALT_LEN
    record = 4 + MB + 16
    swapped = raw[:head] + raw[head + record: head + 2 * record] + raw[head: head + record] + raw[head + 2 * record:]
    open(enc, "wb").write(swapped)
    with pytest.raises(bk.BackupError):
        cb.decrypt_file(enc, str(tmp_path / "out.bin"))


def test_wrong_key_and_key_rotation(tmp_path, monkeypatch):
    data = os.urandom(MB + 5)
    _, enc = make(tmp_path, data)
    out = str(tmp_path / "out.bin")
    monkeypatch.setattr(settings, "backup_encryption_key", KEY_B)
    with pytest.raises(bk.BackupError, match="different BACKUP_ENCRYPTION_KEY"):
        cb.decrypt_file(enc, out)
    # rotation: the new key goes first and encrypts; the old one is kept so old backups still open
    monkeypatch.setattr(settings, "backup_encryption_key", f"{KEY_B}, {KEY_A}")
    assert settings.backup_keys == [KEY_B, KEY_A]
    cb.decrypt_file(enc, out)
    assert read(out) == data
    _, fresh = make(tmp_path, data)
    monkeypatch.setattr(settings, "backup_encryption_key", KEY_B)
    cb.decrypt_file(fresh, out)  # new backups are made with the first key
    assert read(out) == data


def test_not_one_of_ours(tmp_path):
    junk = str(tmp_path / "junk")
    open(junk, "wb").write(b"garbage")
    with pytest.raises(bk.BackupError):
        cb.decrypt_file(junk, str(tmp_path / "out"))
    assert not cb.is_encrypted(junk) and not cb.is_gzip(junk)


def test_short_keys_are_refused(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)  # conftest exports one; "no key" must mean really none

    base = dict(db_host="h", db_name="n", db_user="u", db_password="p", jwt_secret="x" * 32, r2_account_id="a",
                r2_access_key_id="k", r2_secret_access_key="s", r2_bucket="b")
    Settings(_env_file=None, backup_encryption_key="k" * 32, **base)
    assert Settings(_env_file=None, **base).backup_keys == []  # no key = no encryption (plain pg_dump files)
    for bad in ("short", "k" * 32 + ",short"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, backup_encryption_key=bad, **base)


def test_plain_dumps_are_used_as_they_are(tmp_path):
    plain = str(tmp_path / "x.dump")
    open(plain, "wb").write(b"PGDMP\x01\x0f\x00")
    assert asyncio.run(admin_router._open_backup(plain)) == (plain, False)


def test_encrypted_files_need_a_key_on_the_server(tmp_path, monkeypatch):
    plain = str(tmp_path / "p.dump")
    open(plain, "wb").write(b"PGDMP\x01\x0f\x00")
    enc = str(tmp_path / "p.dump.enc")
    cb.encrypt_file(plain, enc)
    monkeypatch.setattr(settings, "backup_encryption_key", "")
    with pytest.raises(HTTPException) as refused:
        asyncio.run(admin_router._open_backup(enc))
    assert refused.value.status_code == 400 and "BACKUP_ENCRYPTION_KEY" in refused.value.detail


def test_encrypted_files_open_through_the_restore_path(tmp_path):
    plain = str(tmp_path / "p.dump")
    open(plain, "wb").write(b"PGDMP\x01\x0f\x00 pretend archive")
    enc = str(tmp_path / "p.dump.enc")
    cb.encrypt_file(plain, enc)
    opened, temporary = asyncio.run(admin_router._open_backup(enc))
    try:
        assert temporary and opened != enc and open(opened, "rb").read().startswith(b"PGDMP")
    finally:
        os.remove(opened)

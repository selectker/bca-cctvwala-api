"""Encryption of backup files (v1.13.4, optional since v1.14.0).

OPTIONAL since v1.14.0: when BACKUP_ENCRYPTION_KEY is set, every backup (downloads, copies in R2, safety snapshots) is
written encrypted with a key that lives only in the server's environment; when it is empty, backups are plain pg_dump
files. Restore accepts both: an encrypted file is decrypted into a temp file, a plain archive is used as it is.

File format (streamed, so a multi-GB backup never has to fit in memory):

    b"BCAENC1\\n"  +  16-byte random salt  +  records...
    record = 4-byte big-endian length + AES-256-GCM ciphertext (1 MiB of plaintext per record, 16-byte tag included)

* A fresh key per file: HKDF-SHA256(master key, salt). So no (key, nonce) pair is ever used twice.
* The nonce of record i is i (12 bytes big-endian), so records can't be reordered or repeated.
* The associated data is 0x01 for the last record and 0x00 for the others, so cutting a file short is detected.
* The plaintext inside is the pg_dump archive (utils/backup.py).

BACKUP_ENCRYPTION_KEY may hold several comma-separated keys: the first encrypts, every one is tried when decrypting
(that is how a key is rotated: put the new key first, keep the old one until the old backups are gone).
"""
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings
from app.utils.backup import BackupError

MAGIC = b"BCAENC1\n"
SALT_LEN = 16
CHUNK = 1024 * 1024
_TAG = 16
_INFO = b"bcacctvwala-backup-v1"
_LAST = b"\x01"
_MORE = b"\x00"


def _cipher(master: str, salt: bytes) -> AESGCM:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_INFO).derive(master.encode("utf-8"))
    return AESGCM(key)


def _nonce(index: int) -> bytes:
    return index.to_bytes(12, "big")


def is_encrypted(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def is_gzip(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def encrypt_file(src: str, dst: str) -> None:
    """Writes the encrypted copy of `src` to `dst` (with the first configured key)."""
    salt = os.urandom(SALT_LEN)
    aes = _cipher(settings.backup_keys[0], salt)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC + salt)
        index = 0
        current = fin.read(CHUNK)
        while True:
            following = fin.read(CHUNK)
            last = not following
            ciphertext = aes.encrypt(_nonce(index), current, _LAST if last else _MORE)
            fout.write(len(ciphertext).to_bytes(4, "big"))
            fout.write(ciphertext)
            if last:
                return
            current, index = following, index + 1


def decrypt_file(src: str, dst: str) -> None:
    """Writes the plaintext of the encrypted backup `src` to `dst`. Raises BackupError if the file is not one of
    ours, was made with another key, was changed, or is incomplete. `dst` is only meaningful on success."""
    with open(src, "rb") as fin:
        head = fin.read(len(MAGIC) + SALT_LEN)
        if len(head) != len(MAGIC) + SALT_LEN or head[: len(MAGIC)] != MAGIC:
            raise BackupError("This is not an encrypted BCAcctvWala backup")
        salt = head[len(MAGIC):]
        candidates = [_cipher(k, salt) for k in settings.backup_keys]

        def read_record() -> bytes | None:
            raw = fin.read(4)
            if not raw:
                return None
            if len(raw) < 4:
                raise BackupError("The backup file is incomplete")
            size = int.from_bytes(raw, "big")
            if size < _TAG or size > CHUNK + _TAG:
                raise BackupError("The backup file is damaged")
            data = fin.read(size)
            if len(data) != size:
                raise BackupError("The backup file is incomplete")
            return data

        current = read_record()
        if current is None:
            raise BackupError("The backup file is incomplete")
        aes = None
        index = 0
        with open(dst, "wb") as fout:
            while True:
                following = read_record()
                aad = _LAST if following is None else _MORE
                if aes is None:  # the first record decides which of the configured keys made this file
                    for candidate in candidates:
                        try:
                            plain = candidate.decrypt(_nonce(index), current, aad)
                        except InvalidTag:
                            continue
                        aes = candidate
                        break
                    else:
                        raise BackupError(
                            "This backup could not be decrypted: it was made with a different BACKUP_ENCRYPTION_KEY, "
                            "or the file has been changed or cut short"
                        )
                else:
                    try:
                        plain = aes.decrypt(_nonce(index), current, aad)
                    except InvalidTag:
                        raise BackupError("The backup file has been changed or is incomplete (it failed its integrity check)")
                fout.write(plain)
                if following is None:
                    return
                current, index = following, index + 1

"""Ticket photos: EXIF/GPS is stripped by re-encoding, and R2 uploads happen outside the DB transaction. No database needed."""
import uuid
from io import BytesIO

import pytest
from PIL import Image

from app.utils import photo
from app.utils.photo import PhotoError, normalise_photo

GPS_IFD = 0x8825


def _exif_with_gps(orientation: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"       # Make
    exif[0xA435] = "SERIAL-123"      # LensSerialNumber
    gps = exif.get_ifd(GPS_IFD)
    gps[1], gps[2] = "N", (28.0, 24.0, 30.0)
    gps[3], gps[4] = "E", (77.0, 18.0, 45.0)
    if orientation:
        exif[0x0112] = orientation
    return exif


def _jpeg(size=(400, 200), exif=None, color=(200, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", exif=exif or b"")
    return buf.getvalue()


def test_exif_and_gps_are_removed():
    src = _jpeg(exif=_exif_with_gps())
    assert GPS_IFD in Image.open(BytesIO(src)).getexif() and b"SecretCam" in src  # the fixture really has them
    out = normalise_photo(src)
    img = Image.open(BytesIO(out))
    assert img.format == "JPEG" and len(img.getexif()) == 0 and not img.getexif().get_ifd(GPS_IFD)
    assert b"SecretCam" not in out and b"SERIAL-123" not in out and b"Exif" not in out
    assert "icc_profile" not in img.info and "xmp" not in img.info


def test_rotation_flag_is_applied_before_it_is_dropped():
    out = Image.open(BytesIO(normalise_photo(_jpeg((400, 200), exif=_exif_with_gps(orientation=6)))))
    assert out.size == (200, 400)  # orientation 6 = rotate 90 degrees


def test_big_photos_are_scaled_down_and_small_ones_left_alone():
    assert max(Image.open(BytesIO(normalise_photo(_jpeg((5000, 3000))))).size) == photo.MAX_SIDE_PX
    assert Image.open(BytesIO(normalise_photo(_jpeg((300, 200))))).size == (300, 200)


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("PNG", "P"), ("WEBP", "RGB"), ("JPEG", "L")])
def test_other_formats_become_plain_rgb_jpeg(fmt, mode):
    buf = BytesIO()
    Image.new(mode, (64, 64), 0 if mode in ("P", "L") else (10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30)).save(buf, fmt)
    img = Image.open(BytesIO(normalise_photo(buf.getvalue())))
    assert img.format == "JPEG" and img.mode == "RGB"


def test_transparent_png_is_flattened_onto_white():
    buf = BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buf, "PNG")
    assert Image.open(BytesIO(normalise_photo(buf.getvalue()))).getpixel((8, 8)) == (255, 255, 255)


def test_png_metadata_chunks_are_dropped():
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Location", "28.4,77.3")
    buf = BytesIO()
    Image.new("RGB", (32, 32), (1, 2, 3)).save(buf, "PNG", pnginfo=info)
    assert b"28.4,77.3" in buf.getvalue()
    assert b"28.4,77.3" not in normalise_photo(buf.getvalue())


def _gif() -> bytes:
    buf = BytesIO()
    Image.new("P", (8, 8), 1).save(buf, "GIF")
    return buf.getvalue()


@pytest.mark.parametrize("data", [b"", b"not an image", b"%PDF-1.4 hello", b"\xff\xd8\xff\xe0" + b"0" * 64, _gif()])
def test_rejects_anything_that_is_not_a_decodable_jpeg_png_webp(data):
    with pytest.raises(PhotoError):
        normalise_photo(data)


def test_pixel_ceiling_is_checked_before_decoding(monkeypatch):
    monkeypatch.setitem(photo.MAX_SOURCE_PIXELS, "PNG", 100)
    buf = BytesIO()
    Image.new("RGB", (20, 20)).save(buf, "PNG")
    with pytest.raises(PhotoError, match="too many pixels"):
        normalise_photo(buf.getvalue())


# ----------------------------------------------------------------------------- the attend endpoint, without a database
class _FakeConn:
    def __init__(self, log, row):
        self.log, self.row = log, row

    def transaction(self):
        log = self.log

        class _Tx:
            async def __aenter__(self_):
                log.append("tx_begin")

            async def __aexit__(self_, *exc):
                log.append("tx_rollback" if exc[0] else "tx_commit")
                return False

        return _Tx()

    async def fetchrow(self, sql, *args):
        self.log.append("lock" if "FOR UPDATE" in sql else "read")
        return self.row

    async def fetchval(self, sql, *args):
        return uuid.uuid4()

    async def execute(self, sql, *args):
        self.log.append("write")


class _FakePool:
    def __init__(self, log, row):
        self.log, self.row, self.open = log, row, 0

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_):
                pool.open += 1
                pool.log.append("acquire")
                return _FakeConn(pool.log, pool.row)

            async def __aexit__(self_, *exc):
                pool.open -= 1
                pool.log.append("release")

        return _Acq()


@pytest.fixture
def attend(monkeypatch):
    """Call the endpoint function directly with a recording fake pool and R2."""
    import asyncio

    from fastapi import UploadFile

    from app.routers import tickets

    ticket_id = str(uuid.uuid4())
    user = {"id": str(uuid.uuid4()), "role": "TECHNICIAN"}
    row = {"id": ticket_id, "status": "OPEN", "assigned_to": None}
    log: list[str] = []
    pool = _FakePool(log, row)
    monkeypatch.setattr(tickets, "get_pool", lambda: pool)
    stored: dict[str, bytes] = {}

    def put(key, data, content_type):
        log.append(f"r2_put(open_connections={pool.open}, in_tx={'tx_begin' in log and 'tx_commit' not in log})")
        stored[key] = data

    monkeypatch.setattr(tickets.storage, "put_bytes", put)
    monkeypatch.setattr(tickets.storage, "delete_keys", lambda keys: [stored.pop(k, None) for k in keys])

    async def load_visible(conn, tid, u):
        return {"id": tid, "status": "ATTENDED"}

    monkeypatch.setattr(tickets, "_load_visible", load_visible)

    def run(files, row_override=None):
        if row_override:
            row.update(row_override)
        uploads = [UploadFile(BytesIO(data), filename=f"{i}.jpg") for i, data in enumerate(files)]
        return asyncio.run(tickets.attend_ticket(
            ticket_id, remarks="done", images=uploads, customer_name=None, customer_signature=None,
            customer_acknowledged=False, no_signoff_reason=None, user=user))

    return run, log, stored


def test_r2_upload_happens_before_the_transaction_and_without_a_connection(attend):
    run, log, stored = attend
    run([_jpeg(exif=_exif_with_gps()), _jpeg()])
    puts = [e for e in log if e.startswith("r2_put")]
    assert len(puts) == 2 and all("open_connections=0" in e and "in_tx=False" in e for e in puts)
    assert max(i for i, e in enumerate(log) if e.startswith("r2_put")) < log.index("tx_begin")
    assert log.count("tx_begin") == 1 and log[-1] == "release" and "tx_commit" in log
    # what reached R2 is clean and re-encoded
    assert len(stored) == 2 and all(k.endswith(".jpg") for k in stored)
    for data in stored.values():
        assert len(Image.open(BytesIO(data)).getexif()) == 0 and b"SecretCam" not in data


def test_a_ticket_that_cannot_be_attended_uploads_nothing(attend):
    from fastapi import HTTPException

    run, log, stored = attend
    with pytest.raises(HTTPException) as err:
        run([_jpeg()], row_override={"status": "ATTENDED"})
    assert err.value.status_code == 409 and not stored and not any(e.startswith("r2_put") for e in log)


def test_a_bad_photo_uploads_nothing(attend):
    from fastapi import HTTPException

    run, log, stored = attend
    with pytest.raises(HTTPException) as err:
        run([_jpeg(), b"not an image"])
    assert err.value.status_code == 400 and not stored and "tx_begin" not in log


def test_photos_are_removed_from_r2_when_the_transaction_fails(attend, monkeypatch):
    from app.routers import tickets

    run, log, stored = attend

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(tickets, "_add_event", boom)
    with pytest.raises(RuntimeError):
        run([_jpeg(), _jpeg()])
    assert "tx_rollback" in log and stored == {}


def test_a_failed_r2_upload_is_a_502_and_leaves_nothing_behind(attend, monkeypatch):
    from fastapi import HTTPException

    from app.routers import tickets

    run, log, stored = attend
    real_put = tickets.storage.put_bytes
    calls = {"n": 0}

    def flaky(key, data, content_type):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("R2 timeout")
        real_put(key, data, content_type)

    monkeypatch.setattr(tickets.storage, "put_bytes", flaky)
    with pytest.raises(HTTPException) as err:
        run([_jpeg(), _jpeg(), _jpeg()])
    assert err.value.status_code == 502 and stored == {} and "tx_begin" not in log

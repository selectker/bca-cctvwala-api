"""Profile image (v1.25.0): image normalising and request limits (no database), then the endpoints end to end."""
import os
import time
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.body_limit import limit_for
from app.config import settings
from app.main import app
from app.utils import avatar
from app.utils.avatar import AvatarError, normalise_avatar
from tests.helpers import first_login

MB = 1024 * 1024
GPS_IFD = 0x8825
OWNER = {"phone": "9000000001", "password": "ownerpass1"}
ADMIN = {"phone": "9000000000", "password": "adminpass1"}


def _image(fmt="PNG", size=(900, 600), mode="RGB", color=(20, 80, 200), exif=None) -> bytes:
    buf = BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    Image.new(mode, size, color).save(buf, fmt, **kwargs)
    return buf.getvalue()


# ----------------------------------------------------------------------------- image normalising
@pytest.mark.parametrize("fmt,mode,color", [("PNG", "RGBA", (20, 80, 200, 255)), ("JPEG", "RGB", (20, 80, 200)), ("WEBP", "RGB", (20, 80, 200))])
def test_png_jpeg_webp_become_a_square_jpeg(fmt, mode, color):
    img = Image.open(BytesIO(normalise_avatar(_image(fmt, (2400, 800), mode, color))))
    assert img.format == "JPEG" and img.mode == "RGB"
    assert img.size == (avatar.SIZE_PX, avatar.SIZE_PX)


def test_large_images_are_cropped_to_the_size_cap():
    assert Image.open(BytesIO(normalise_avatar(_image("JPEG", (3000, 2000))))).size == (avatar.SIZE_PX, avatar.SIZE_PX)


def test_small_images_are_cropped_square_but_never_enlarged():
    assert Image.open(BytesIO(normalise_avatar(_image(size=(200, 120))))).size == (120, 120)


def test_the_centre_of_the_picture_is_kept():
    src = Image.new("RGB", (900, 300), (255, 0, 0))
    src.paste((0, 0, 255), (300, 0, 600, 300))  # blue middle third, red left and right thirds
    buf = BytesIO()
    src.save(buf, "PNG")
    out = Image.open(BytesIO(normalise_avatar(buf.getvalue())))
    assert out.size == (300, 300)
    r, g, b = out.getpixel((150, 150))
    assert b > 200 and r < 60  # the crop is the blue middle, not the red sides


def test_exif_and_gps_are_removed_and_rotation_is_applied():
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    gps = exif.get_ifd(GPS_IFD)
    gps[1], gps[2] = "N", (28.0, 24.0, 30.0)
    exif[0x0112] = 6  # rotate 90 degrees
    src = _image("JPEG", (400, 200), exif=exif)
    assert b"SecretCam" in src
    out = normalise_avatar(src)
    img = Image.open(BytesIO(out))
    assert b"SecretCam" not in out and len(img.getexif()) == 0 and "icc_profile" not in img.info
    assert img.size == (200, 200)


def test_transparent_png_is_flattened_onto_white():
    out = Image.open(BytesIO(normalise_avatar(_image("PNG", (64, 64), "RGBA", (0, 0, 0, 0)))))
    assert all(c > 240 for c in out.getpixel((32, 32)))


@pytest.mark.parametrize("data", [b"", b"not an image", b"%PDF-1.4 hello", b"GIF89a" + b"0" * 64, _image("GIF", mode="P", color=1)])
def test_rejects_anything_that_is_not_png_jpeg_webp(data):
    with pytest.raises(AvatarError):
        normalise_avatar(data)


def test_rejects_a_truncated_image():
    with pytest.raises(AvatarError):
        normalise_avatar(_image("PNG", (800, 800))[:-40])


def test_pixel_ceiling_is_checked_before_decoding(monkeypatch):
    monkeypatch.setitem(avatar.MAX_SOURCE_PIXELS, "PNG", 100)
    with pytest.raises(AvatarError, match="too many pixels"):
        normalise_avatar(_image("PNG", (200, 200)))


def test_keys_are_random_and_under_the_users_prefix():
    a, b = avatar.new_key(), avatar.new_key()
    assert a != b and a.startswith("users/profile/") and a.endswith(".jpg")


# ----------------------------------------------------------------------------- request limits
def test_profile_image_upload_has_its_own_body_limit():
    assert limit_for("/api/v1/profile/image") == (settings.max_profile_image_mb + 1) * MB
    assert limit_for("/api/v1/profile") == settings.max_body_kb * 1024


def test_oversized_upload_is_refused_without_being_read():
    r = TestClient(app).post("/api/v1/profile/image", content=b"x" * (limit_for("/api/v1/profile/image") + 1),
                             headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413


# ----------------------------------------------------------------------------- end to end (needs PostgreSQL, see README 2m)
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.headers["X-Setup-Token"] = os.environ["SETUP_TOKEN"]
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


def ok(resp, code=200):
    assert resp.status_code == code, f"{resp.status_code}: {resp.text}"
    return resp.json()


def got_latest(client, token):
    return client.get("/api/v1/profile/image", headers=H(token)).content


def _profile_keys(fake_r2):
    return {k for k in fake_r2.objects if k.startswith(avatar.PREFIX)}


def test_profile_image_end_to_end(client, fake_r2):
    state = ok(client.get("/api/v1/auth/bootstrap-status"))
    if state["needs_admin_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-admin", json={"name": "Admin", **ADMIN}), 201)
    if ok(client.get("/api/v1/auth/bootstrap-status"))["needs_owner_bootstrap"]:
        ok(client.post("/api/v1/auth/bootstrap-owner", json={"name": "Owner", **OWNER}), 201)
    ot = ok(client.post("/api/v1/auth/login", json=OWNER))["token"]
    phone = f"96{str(int(time.time() * 1000))[-8:]}"
    staff = ok(client.post("/api/v1/auth/users", headers=H(ot), json={"name": "Pic Staff", "phone": phone, "role": "STAFF"}), 201)
    st = first_login(client, staff)

    # nothing yet
    assert client.get("/api/v1/profile/image").status_code == 401
    me = ok(client.get("/api/v1/profile", headers=H(st)))
    assert me["has_image"] is False and me["image_updated_at"] is None and "avatar_key" not in me
    assert client.get("/api/v1/profile/image", headers=H(st)).status_code == 404

    # unusable files are a 400 and store nothing
    before = _profile_keys(fake_r2)
    for name, data in (("a.txt", b"hello"), ("a.png", b"")):
        assert client.post("/api/v1/profile/image", headers=H(st), files={"image": (name, data, "image/png")}).status_code == 400
    assert _profile_keys(fake_r2) == before

    # upload
    done = ok(client.post("/api/v1/profile/image", headers=H(st), files={"image": ("me.png", _image("PNG", (800, 500)), "image/png")}))
    assert done["has_image"] is True and done["image_updated_at"] and "avatar_key" not in done and done["id"] == staff["id"]
    first_keys = _profile_keys(fake_r2) - before
    assert len(first_keys) == 1
    got = client.get("/api/v1/profile/image", headers=H(st))
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg" and "no-cache" in got.headers["cache-control"]
    assert Image.open(BytesIO(got.content)).size == (500, 500)

    # replacing swaps the stored file (the old one is deleted)
    ok(client.post("/api/v1/profile/image", headers=H(st), files={"image": ("me2.jpg", _image("JPEG", (600, 600)), "image/jpeg")}))
    second_keys = _profile_keys(fake_r2) - before
    assert len(second_keys) == 1 and second_keys != first_keys

    # it belongs to this account only
    other = client.get("/api/v1/profile/image", headers=H(ot))  # the owner never gets the staff member's picture
    assert other.status_code == 404 or other.content != got.content

    # teammates see it in the team list and can fetch it
    mine = next(u for u in ok(client.get("/api/v1/auth/team", headers=H(ot))) if u["id"] == staff["id"])
    assert mine["has_image"] is True and mine["image_updated_at"] and "avatar_key" not in mine and "phone" not in mine
    pic = client.get(f"/api/v1/auth/team/{staff['id']}/image", headers=H(ot))
    assert pic.status_code == 200 and pic.headers["content-type"] == "image/jpeg" and pic.content == got_latest(client, st)
    assert client.get(f"/api/v1/auth/team/{staff['id']}/image").status_code == 401
    assert client.get("/api/v1/auth/team/not-a-uuid/image", headers=H(ot)).status_code == 404
    assert client.get("/api/v1/auth/team/00000000-0000-4000-8000-000000000000/image", headers=H(ot)).status_code == 404
    assert next(u for u in ok(client.get("/api/v1/auth/users", headers=H(ot))) if u["id"] == staff["id"])["has_image"] is True

    # audit trail
    rows = ok(client.get("/api/v1/audit-logs", headers=H(ot), params={"user_id": staff["id"], "action": "UPDATE_PROFILE_IMAGE",
                                                                     "from": "2000-01-01", "to": "2100-01-01"}))
    assert len(rows) >= 2 and all(r["summary"] == "Changed own profile image" for r in rows[:2])

    # remove
    gone = ok(client.delete("/api/v1/profile/image", headers=H(st)))
    assert gone["has_image"] is False
    assert _profile_keys(fake_r2) == before
    assert client.get("/api/v1/profile/image", headers=H(st)).status_code == 404
    assert client.get(f"/api/v1/auth/team/{staff['id']}/image", headers=H(ot)).status_code == 404
    assert next(u for u in ok(client.get("/api/v1/auth/team", headers=H(ot))) if u["id"] == staff["id"])["has_image"] is False
    assert ok(client.delete("/api/v1/profile/image", headers=H(st)))["has_image"] is False  # removing nothing is fine

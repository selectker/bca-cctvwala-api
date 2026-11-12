"""v1.17.2: one cap on image decoding for photos, logo and signature; lower pixel ceiling for logo / signature."""
import asyncio
import threading
import time
from io import BytesIO

import pytest
from PIL import Image

from app.utils import image_gate
from app.utils.logo import LogoError, normalise_logo
from app.utils.signature import SignatureError, normalise_signature


def test_at_most_two_image_jobs_run_at_once_across_everything():
    running = peak = 0
    lock = threading.Lock()

    def job(_):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return "done"

    async def burst():
        # 12 uploads at once: photos, logos and signatures all go through the same gate
        return await asyncio.gather(*(image_gate.run_limited(job, i) for i in range(12)))

    assert asyncio.run(burst()) == ["done"] * 12
    assert 1 <= peak <= image_gate.MAX_CONCURRENT_JOBS == 2


def test_photo_logo_and_signature_all_use_the_shared_gate():
    import pathlib

    root = pathlib.Path("app")
    assert "image_gate.run_limited(normalise_photo" in (root / "utils/photo.py").read_text()
    store = (root / "routers/store.py").read_text()
    assert "image_gate.run_limited(logo_util.normalise_logo" in store
    assert "image_gate.run_limited(signature_util.normalise_signature" in store
    assert "to_thread(logo_util.normalise_logo" not in store and "to_thread(signature_util.normalise_signature" not in store


def _png(size, mode="L"):
    buf = BytesIO()
    Image.new(mode, size, 255).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("normalise,error", [(normalise_logo, LogoError), (normalise_signature, SignatureError)])
def test_images_over_twelve_megapixels_are_refused_before_decoding(normalise, error):
    # 3600 x 3400 = 12.24 MP: compresses to a few KB, so only the pixel ceiling can stop it
    with pytest.raises(error, match="too large"):
        normalise(_png((3600, 3400)))


@pytest.mark.parametrize("normalise", [normalise_logo, normalise_signature])
def test_images_just_under_the_ceiling_still_work(normalise):
    out = normalise(_png((3400, 3400), "RGB"))
    assert Image.open(BytesIO(out)).format == "PNG"


def test_big_jpegs_are_decoded_shrunk_and_still_come_out_right():
    buf = BytesIO()
    Image.new("RGB", (3000, 1000), (20, 80, 200)).save(buf, "JPEG")
    assert Image.open(BytesIO(normalise_logo(buf.getvalue()))).size == (600, 200)
    assert max(Image.open(BytesIO(normalise_signature(buf.getvalue()))).size) <= 600

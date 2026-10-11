import io

import pytest
from PIL import Image

from app.services import image_processing


class FakeSession:
    created = 0

    def __init__(self, model_name, sess_opts, providers=None):
        FakeSession.created += 1
        self.providers = providers

    @classmethod
    def name(cls, *args, **kwargs):
        return "fake-model"


@pytest.fixture
def fake_rembg(monkeypatch, tmp_path):
    import rembg
    import rembg.sessions

    FakeSession.created = 0
    calls = []

    def fake_remove(img, session=None, **kwargs):
        calls.append((img.size, session))
        out = Image.new("RGBA", img.size, (0, 0, 0, 0))
        out.paste(Image.new("RGBA", (img.width // 2, img.height // 2), (255, 0, 0, 255)), (img.width // 4, img.height // 4))
        return out

    monkeypatch.setattr(rembg, "remove", fake_remove)
    monkeypatch.setattr(rembg.sessions, "sessions_class", [FakeSession])
    monkeypatch.setattr(image_processing.settings, "rembg_model", "fake-model")
    monkeypatch.setattr(image_processing, "_session", None)
    monkeypatch.setattr(image_processing, "CUTOUT_CACHE_DIR", tmp_path / "cutouts")
    return calls


def test_session_created_once_and_cpu_only(fake_rembg, image_bytes):
    image_processing.cutout_png(image_bytes((200, 100), color=(1, 2, 3)))
    image_processing.cutout_png(image_bytes((200, 100), color=(9, 9, 9)))  # different bytes -> inference again
    assert FakeSession.created == 1
    assert len(fake_rembg) == 2
    assert fake_rembg[0][1].providers == ["CPUExecutionProvider"]


def test_cache_hit_skips_inference(fake_rembg, image_bytes):
    data = image_bytes((200, 100))
    first = image_processing.cutout_png(data)
    second = image_processing.cutout_png(data)
    assert first == second
    assert len(fake_rembg) == 1
    assert list(image_processing.CUTOUT_CACHE_DIR.glob("*-fake-model.png"))


def test_input_is_downscaled_before_inference(fake_rembg, image_bytes):
    png = image_processing.cutout_png(image_bytes((3000, 2000)))
    assert max(fake_rembg[0][0]) == image_processing.CUTOUT_MAX_SIDE
    out = Image.open(io.BytesIO(png))
    assert out.mode == "RGBA" and max(out.size) == image_processing.CUTOUT_MAX_SIDE


def test_cache_evicts_oldest_beyond_cap(fake_rembg, image_bytes, monkeypatch):
    monkeypatch.setattr(image_processing, "CUTOUT_CACHE_MAX_BYTES", 1)
    image_processing.cutout_png(image_bytes((100, 100), color=(1, 1, 1)))
    image_processing.cutout_png(image_bytes((100, 100), color=(2, 2, 2)))
    assert len(list(image_processing.CUTOUT_CACHE_DIR.glob("*.png"))) <= 1


def test_background_removed_uses_cutout_and_places_subject(fake_rembg, image_bytes):
    out = image_processing.render_background("background_removed", image_bytes((400, 400), color=(10, 200, 10)), "1:1")
    img = Image.open(io.BytesIO(out))
    assert img.size == (1080, 1080)
    assert img.getpixel((5, 5)) == (0, 0, 0)  # black backdrop, not the green source
    r, g, b = img.getpixel((540, 500))
    assert r > 200 and g < 60  # the (fake) red subject sits centred


def test_cutout_endpoint_returns_png_and_caches(client, fake_rembg, image_bytes):
    data = image_bytes((300, 200))
    for _ in range(2):
        resp = client.post("/api/posts/cutout", files={"image": ("a.png", data, "image/png")})
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(resp.content)).mode == "RGBA"
    assert len(fake_rembg) == 1


def test_cutout_endpoint_400_on_garbage(client, fake_rembg):
    resp = client.post("/api/posts/cutout", files={"image": ("a.png", b"not an image", "image/png")})
    assert resp.status_code == 400

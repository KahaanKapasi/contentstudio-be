"""Muapi image-to-video: upload the start frame, submit the i2v endpoint with image_url/images_list, poll, download.
Endpoint ids and fields come from https://api.muapi.ai/openapi.json (checked 2026-10-09)."""

import json

import httpx
import pytest
from PIL import Image

from app.config import settings
from app.services.studio.engines import _scenefilm as sf
from app.services.studio.kit import clips
from app.services.video_providers import JobParams, ProviderError, catalog
from app.services.video_providers.muapi import MuapiProvider

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64


@pytest.fixture
def still(tmp_path):
    path = tmp_path / "still.png"
    Image.new("RGB", (64, 112), (200, 30, 30)).save(path)
    return path


@pytest.fixture
def http(monkeypatch):
    real = httpx.Client

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=transport, **kw))
        monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: real(transport=transport).stream(method, url, **kw))

    monkeypatch.setattr(settings, "muapi_api_key", "mu-key")
    return install


def fake_muapi(seen, upload_reply=None):
    polls = {"n": 0}

    def handler(request):
        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=MP4)
        assert request.headers["x-api-key"] == "mu-key"
        path = request.url.path
        seen.setdefault("calls", []).append((request.method, path))
        if path == "/api/v1/upload_file":
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert b'name="file"' in request.content and b"\xff\xd8" in request.content  # a JPEG part
            return httpx.Response(200, json=upload_reply or {"url": "https://cdn.example.com/up/still.jpg"})
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"request_id": "r-1"})
        polls["n"] += 1
        if polls["n"] < 2:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(200, json={"status": "completed", "outputs": ["https://cdn.example.com/out.mp4"]})

    return handler


# --- catalog ---


def test_i2v_catalog_ids_fields_and_flags():
    kling = catalog.get_model("muapi", "kling-v3.0-standard-image-to-video")
    assert kling.image_to_video and kling.path == "kling-v3.0-standard-image-to-video"
    assert catalog.build_body(kling, "p", "9:16", 5, "720p", "https://u/i.jpg") == {"prompt": "p", "image_url": "https://u/i.jpg", "generate_audio": True, "duration": 5}
    seed = catalog.get_model("muapi", "seedance-v2.0-i2v")
    body = catalog.build_body(seed, "walks", "9:16", 5, "720p", "https://u/i.jpg")
    assert body == {"prompt": "@image1 walks", "images_list": ["https://u/i.jpg"], "aspect_ratio": "9:16", "duration": 5, "resolution": "720p"}
    wan = catalog.get_model("muapi", "wan2.6-image-to-video")
    assert catalog.build_body(wan, "p", "9:16", 10, "1080p", "u") == {"prompt": "p", "image_url": "u", "duration": 10, "resolution": "1080p"}
    k26 = catalog.get_model("muapi", "kling-v2.6-pro-i2v")
    assert catalog.build_body(k26, "p", "9:16", 5, "1080p", "u")["sound"] is True
    for spec in catalog.MUAPI_I2V_MODELS.values():  # every entry is usable and validates against itself
        assert spec.path and spec.image_to_video
        catalog.validate("muapi", spec.id, spec.aspect_ratios[0], spec.durations[0], spec.resolutions[0])


def test_text_models_stay_text_only_and_out_of_the_i2v_picker():
    assert not any(m.image_to_video for m in catalog.MODELS["muapi"].values())
    assert "kling-v3.0-standard-image-to-video" not in catalog.MODELS["muapi"]  # the plain Video tab has no image input
    t2v = catalog.get_model("muapi", "wan2.5-text-to-video")
    assert catalog.build_body(t2v, "p", "9:16", 5, "720p", "https://ignored") == catalog.build_body(t2v, "p", "9:16", 5, "720p")


def test_image_variant_mapping():
    assert catalog.image_variant("muapi", "kling-v3.0-standard-text-to-video") == "kling-v3.0-standard-image-to-video"
    assert catalog.image_variant("muapi", "seedance-v2.0-t2v") == "seedance-v2.0-i2v"
    assert catalog.image_variant("muapi", "wan2.6-text-to-video") == "wan2.6-image-to-video"
    assert catalog.image_variant("muapi", "veo3.1-text-to-video") == "veo3.1-image-to-video"
    assert catalog.image_variant("veo", "veo-3.1-lite-generate-preview") is None
    assert catalog.supports_image("veo", "x") and not catalog.supports_image("higgsfield", "seedance-2.0")


# --- provider ---


def test_muapi_i2v_upload_submit_poll_download(http, still, tmp_path, monkeypatch):
    seen = {}
    http(fake_muapi(seen))
    monkeypatch.setattr(clips, "POLL_INTERVAL_S", 0)
    out = clips.generate_clip(
        "she turns and smiles", tmp_path / "c.mp4", provider="muapi", model="kling-v3.0-standard-image-to-video",
        aspect_ratio="9:16", duration_seconds=5, resolution="720p", image=still,
    )
    assert out.read_bytes() == MP4
    assert seen["calls"][:2] == [("POST", "/api/v1/upload_file"), ("POST", "/api/v1/kling-v3.0-standard-image-to-video")]
    assert seen["body"] == {"prompt": "she turns and smiles", "image_url": "https://cdn.example.com/up/still.jpg", "generate_audio": True, "duration": 5}


def test_text_model_with_image_is_routed_to_its_i2v_sibling(http, still, tmp_path, monkeypatch):
    seen = {}
    http(fake_muapi(seen))
    monkeypatch.setattr(clips, "POLL_INTERVAL_S", 0)
    clips.generate_clip(
        "p", tmp_path / "c.mp4", provider="muapi", model="seedance-v2.0-t2v", aspect_ratio="9:16", duration_seconds=5,
        resolution="high", image=still,
    )
    assert ("POST", "/api/v1/seedance-v2.0-i2v") in seen["calls"]
    assert seen["body"]["images_list"] == ["https://cdn.example.com/up/still.jpg"] and seen["body"]["resolution"] == "720p"


def test_text_only_path_is_unchanged(http, tmp_path, monkeypatch):
    seen = {}
    http(fake_muapi(seen))
    monkeypatch.setattr(clips, "POLL_INTERVAL_S", 0)
    clips.generate_clip("p", tmp_path / "c.mp4", provider="muapi", model="wan2.5-text-to-video", aspect_ratio="9:16", duration_seconds=5, resolution="720p")
    assert ("POST", "/api/v1/upload_file") not in seen["calls"]
    assert seen["body"] == {"prompt": "p", "aspect_ratio": "9:16", "duration": 5, "resolution": "720p"}


def test_i2v_model_without_image_is_a_clear_error(http, tmp_path):
    http(fake_muapi({}))
    with pytest.raises(ProviderError, match="needs a start image"):
        MuapiProvider().submit(JobParams("p", "wan2.6-image-to-video", "9:16", 5, "720p"))


def test_upload_reply_shapes_and_cloudinary_fallback(http, still, monkeypatch):
    http(fake_muapi({}, upload_reply={"data": {"url": "https://cdn.example.com/nested.jpg"}}))
    assert MuapiProvider().upload_image(str(still)) == "https://cdn.example.com/nested.jpg"

    def failing(request):
        return httpx.Response(500, json={"detail": "down"})

    http(failing)
    with pytest.raises(ProviderError, match="500"):  # no Cloudinary configured: surface Muapi's error
        MuapiProvider().upload_image(str(still))
    monkeypatch.setattr(settings, "cloudinary_url", "cloudinary://x:y@z")
    from app.services import media_hosting

    monkeypatch.setattr(media_hosting, "upload_image", lambda data, public_id=None: "https://res.cloudinary.com/a.jpg")
    assert MuapiProvider().upload_image(str(still)) == "https://res.cloudinary.com/a.jpg"


# --- scene films ---


def test_scenefilm_muapi_targets_an_image_to_video_model():
    provider, model, res = sf.ai_target({"clip_provider": "muapi", "motion_quality": "ai"})
    assert provider == "muapi" and catalog.get_model(provider, model).image_to_video and catalog.supports_image(provider, model)
    assert sf.clip_len({"clip_provider": "muapi"}, 6.2) == 7

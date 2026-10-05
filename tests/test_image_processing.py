import io
from pathlib import Path

import pytest
from PIL import Image

from app.services import image_processing
from app.services.image_processing import ASPECT_RATIOS

FAST_TEMPLATES = ["darkened_background", "transparency_overlay"]
MODEL_PATH = Path.home() / ".u2net" / "u2net.onnx"


def jpeg_size(data: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    return img.size


@pytest.mark.parametrize("template", FAST_TEMPLATES)
@pytest.mark.parametrize("ratio", list(ASPECT_RATIOS))
def test_render_background_matches_aspect_ratio_dimensions(template, ratio, image_bytes):
    out = image_processing.render_background(template, image_bytes((400, 300)), ratio)
    assert jpeg_size(out) == ASPECT_RATIOS[ratio]


@pytest.mark.parametrize("template", FAST_TEMPLATES)
@pytest.mark.parametrize("ratio", list(ASPECT_RATIOS))
def test_render_template_matches_aspect_ratio_dimensions(template, ratio, image_bytes):
    out = image_processing.render_template(template, image_bytes((400, 300)), "Hala Madrid", ratio)
    assert jpeg_size(out) == ASPECT_RATIOS[ratio]


def test_default_aspect_ratio_is_square(image_bytes):
    out = image_processing.render_background("darkened_background", image_bytes())
    assert jpeg_size(out) == (1080, 1080)


def test_extreme_source_aspect_is_cover_cropped_to_canvas(image_bytes):
    tall = image_bytes((100, 900))
    out = image_processing.render_background("transparency_overlay", tall, "16:9")
    assert jpeg_size(out) == (1920, 1080)


def test_darkened_background_is_darker_than_source(image_bytes):
    src = image_bytes((200, 200), color=(200, 200, 200))
    out = Image.open(io.BytesIO(image_processing.render_background("darkened_background", src))).convert("L")
    assert max(out.getdata()) < 200


def test_transparency_overlay_blends_towards_black(image_bytes):
    src = image_bytes((200, 200), color=(255, 255, 255))
    out = Image.open(io.BytesIO(image_processing.render_background("transparency_overlay", src))).convert("L")
    assert 90 < out.getpixel((540, 20)) < 140


def test_render_template_with_text_differs_from_background_only(image_bytes):
    src = image_bytes((300, 300))
    plain = image_processing.render_background("darkened_background", src)
    with_text = image_processing.render_template("darkened_background", src, "Vinicius scores a hat-trick", "1:1")
    assert plain != with_text


def test_render_template_accepts_empty_text(image_bytes):
    out = image_processing.render_template("darkened_background", image_bytes(), "", "1:1")
    assert jpeg_size(out) == (1080, 1080)


def test_resolve_canvas_size_unknown_ratio_raises():
    with pytest.raises(ValueError, match="Unknown aspect_ratio"):
        image_processing.resolve_canvas_size("2:1")


def test_unknown_aspect_ratio_raises_for_background_and_template(image_bytes):
    with pytest.raises(ValueError):
        image_processing.render_background("darkened_background", image_bytes(), "2:1")
    with pytest.raises(ValueError):
        image_processing.render_template("darkened_background", image_bytes(), "x", "2:1")


def test_unknown_template_raises_for_background_and_template(image_bytes):
    with pytest.raises(ValueError, match="Unknown template"):
        image_processing.render_background("sepia", image_bytes())
    with pytest.raises(ValueError, match="Unknown template"):
        image_processing.render_template("sepia", image_bytes(), "x")


def test_wrap_text_caps_at_five_lines():
    from PIL import ImageDraw

    img = Image.new("RGB", (400, 400))
    draw = ImageDraw.Draw(img)
    font = image_processing._load_font(40)
    lines = image_processing._wrap_text("word " * 200, font, 300, draw)
    assert len(lines) == 5
    assert all(draw.textlength(line, font=font) <= 300 for line in lines)


def test_aspect_ratios_endpoint_matches_service(client):
    resp = client.get("/api/posts/aspect-ratios")
    assert resp.status_code == 200
    assert resp.json() == [{"id": k, "width": w, "height": h} for k, (w, h) in ASPECT_RATIOS.items()]


def post_image(client, path, params, data):
    return client.post(path, params=params, files={"image": ("in.png", data, "image/png")})


def test_render_background_endpoint_returns_jpeg_of_requested_size(client, image_bytes):
    resp = post_image(
        client,
        "/api/posts/render-background",
        {"template_name": "darkened_background", "aspect_ratio": "9:16"},
        image_bytes(),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert jpeg_size(resp.content) == (1080, 1920)


def test_render_preview_endpoint_returns_jpeg_of_requested_size(client, image_bytes):
    resp = post_image(
        client,
        "/api/posts/render-preview",
        {"template_name": "transparency_overlay", "text": "Hello", "aspect_ratio": "4:5"},
        image_bytes(),
    )
    assert resp.status_code == 200
    assert jpeg_size(resp.content) == (1080, 1350)


@pytest.mark.parametrize("path, extra", [("/api/posts/render-background", {}), ("/api/posts/render-preview", {"text": "x"})])
def test_render_endpoints_400_on_unknown_aspect_ratio(client, image_bytes, path, extra):
    resp = post_image(client, path, {"template_name": "darkened_background", "aspect_ratio": "7:3", **extra}, image_bytes())
    assert resp.status_code == 400
    assert "aspect_ratio" in resp.json()["detail"]


@pytest.mark.parametrize("path, extra", [("/api/posts/render-background", {}), ("/api/posts/render-preview", {"text": "x"})])
def test_render_endpoints_400_on_unknown_template(client, image_bytes, path, extra):
    resp = post_image(client, path, {"template_name": "nope", **extra}, image_bytes())
    assert resp.status_code == 400
    assert "template" in resp.json()["detail"].lower()


def rembg_unavailable_reason() -> str | None:
    if not MODEL_PATH.exists():
        return "rembg onnx model not cached locally; would require a download"
    try:
        import rembg  # noqa: F401
    except Exception as exc:
        return f"rembg cannot be imported in this environment: {type(exc).__name__}"
    return None


@pytest.mark.slow
def test_background_removed_renders_correct_dimensions(image_bytes):
    reason = rembg_unavailable_reason()
    if reason:
        pytest.skip(reason)
    out = image_processing.render_template("background_removed", image_bytes((256, 256), color=(30, 160, 90)), "Subject", "4:5")
    assert jpeg_size(out) == ASPECT_RATIOS["4:5"]

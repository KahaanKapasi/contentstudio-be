"""Auto-fit image+text composition for the 3 fixed Posts/Carousel templates,
per 04_Posts_Carousel_Studio.md. This is the backend auto-fit step — the
frontend fabric.js editor then lets the user manually refine the result; it is
NOT a general-purpose layout engine (template-constrained, per doc's explicit
boundary).

Two entry points:
- `render_background` — applies only the image treatment (no text), used to
  feed the live fabric.js editor's canvas background so text stays a
  separately draggable/editable layer.
- `render_template` — treatment + text + watermark baked in, for a one-shot
  export where no further editing is needed.

Both accept an `aspect_ratio` key (see ASPECT_RATIOS) so the same 3 templates
work across the common social crop shapes (square, portrait feed, stories,
landscape) rather than being locked to the original 1:1 IG square.

Interpretation note: "Darkened background — foreground subject preserved" is
implemented as a full-image darken/contrast/desaturate treatment (not true
subject segmentation) since that matches the simple Canva-style quick-edit
this is replacing. "Background-removed" is the one template that does real
subject segmentation, via rembg.
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# Long edge fixed at 1080/1920 depending on orientation — matches common
# export resolutions for each shape rather than being an arbitrary choice.
ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "3:4": (1080, 1440),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}
DEFAULT_ASPECT_RATIO = "1:1"

WATERMARK_TEXT = "@Madridonomy"  # placeholder — swap for the real handle/logo asset

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def resolve_canvas_size(aspect_ratio: str) -> tuple[int, int]:
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect_ratio '{aspect_ratio}'. Options: {list(ASPECT_RATIOS)}")
    return ASPECT_RATIOS[aspect_ratio]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _fit_cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:5]  # 3-5 lines max per doc


def _draw_centered_text(draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.FreeTypeFont, canvas_size: tuple[int, int], fill=(255, 255, 255, 255)):
    line_height = font.size + 10
    total_height = line_height * len(lines)
    y = (canvas_size[1] - total_height) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        x = (canvas_size[0] - w) // 2
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height


def _add_watermark(draw: ImageDraw.ImageDraw, canvas_size: tuple[int, int]):
    font = _load_font(28)
    text = WATERMARK_TEXT
    w = draw.textlength(text, font=font)
    x = canvas_size[0] - w - 30
    y = canvas_size[1] - 28 - 30
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 200))


def _treat_darkened_background(image_bytes: bytes, canvas_size: tuple[int, int]) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = _fit_cover(img, canvas_size)
    img = ImageEnhance.Color(img).enhance(0.5)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Brightness(img).enhance(0.55)
    return img.convert("RGBA")


def _treat_transparency_overlay(image_bytes: bytes, canvas_size: tuple[int, int]) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = _fit_cover(img, canvas_size)
    img = ImageEnhance.Color(img).enhance(0.35)

    black_bg = Image.new("RGBA", canvas_size, (0, 0, 0, 255))
    faded = img.convert("RGBA")
    faded.putalpha(int(255 * 0.45))
    return Image.alpha_composite(black_bg, faded)


def _treat_background_removed(image_bytes: bytes, canvas_size: tuple[int, int]) -> tuple[Image.Image, int]:
    """Returns (canvas, subject_bottom_y) — callers that draw text below the
    subject (the full render_template path) need subject_bottom_y; the plain
    background (no text) path only needs the canvas."""
    from rembg import remove  # imported lazily — heavy (onnxruntime) import

    cutout = remove(image_bytes)  # PNG bytes with alpha
    subject = Image.open(io.BytesIO(cutout)).convert("RGBA")

    subject.thumbnail((canvas_size[0] - 120, int(canvas_size[1] * 0.75)), Image.LANCZOS)
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 255))
    paste_x = (canvas_size[0] - subject.width) // 2
    paste_y = (canvas_size[1] - subject.height) // 2 - 40
    canvas.paste(subject, (paste_x, paste_y), subject)
    return canvas, paste_y + subject.height


_TREATMENTS = {
    "darkened_background": _treat_darkened_background,
    "transparency_overlay": _treat_transparency_overlay,
    "background_removed": lambda b, size: _treat_background_removed(b, size)[0],
}

_TEXT_FONT_SIZE = {
    "darkened_background": 64,
    "transparency_overlay": 60,
    "background_removed": 56,
}


def render_background(template_name: str, image_bytes: bytes, aspect_ratio: str = DEFAULT_ASPECT_RATIO) -> bytes:
    """Treatment only, no text/watermark — for the live editor's canvas
    background, so text stays a separate, draggable client-side layer."""
    treat = _TREATMENTS.get(template_name)
    if not treat:
        raise ValueError(f"Unknown template '{template_name}'. Options: {list(_TREATMENTS)}")
    canvas_size = resolve_canvas_size(aspect_ratio)
    canvas = treat(image_bytes, canvas_size)
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()


def render_template(
    template_name: str, image_bytes: bytes, text: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO
) -> bytes:
    """Treatment + text + watermark baked in — one-shot export."""
    if template_name not in _TREATMENTS:
        raise ValueError(f"Unknown template '{template_name}'. Options: {list(_TREATMENTS)}")
    canvas_size = resolve_canvas_size(aspect_ratio)

    font = _load_font(_TEXT_FONT_SIZE[template_name])

    if template_name == "background_removed":
        canvas, subject_bottom_y = _treat_background_removed(image_bytes, canvas_size)
        draw = ImageDraw.Draw(canvas)
        lines = _wrap_text(text, font, canvas_size[0] - 160, draw)
        y = subject_bottom_y + 30
        line_height = font.size + 10
        for line in lines:
            w = draw.textlength(line, font=font)
            x = (canvas_size[0] - w) // 2
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_height
    else:
        canvas = _TREATMENTS[template_name](image_bytes, canvas_size)
        draw = ImageDraw.Draw(canvas)
        lines = _wrap_text(text, font, canvas_size[0] - 160, draw)
        _draw_centered_text(draw, lines, font, canvas_size)

    _add_watermark(draw, canvas_size)
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()

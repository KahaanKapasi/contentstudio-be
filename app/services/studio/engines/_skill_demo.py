"""Recipe `product-demo`: Gemini (which can look at the screenshots) writes one narrated beat per screenshot and says
where to point the callout; each screenshot becomes a zoom/pan clip with a spotlight highlight box drawn with Pillow;
narration + captions follow, and an end card closes on the product name. The product URL is only used as text."""

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.services.studio.engines import _shared
from app.services.studio.engines import _skill_common as common
from app.services.studio.kit import captions, ffmpeg, images, tts
from app.services.studio.registry import FieldSpec, RecipeSpec, Stage, StageError, ValidationError

MAX_SHOTS = 8
END_CARD_S = 2.5
EFFECTS = ("in", "pan_right", "out", "pan_left")
ACCENT = (56, 189, 248)
BG_TOP, BG_BOTTOM = (23, 30, 56), (8, 10, 22)
DEFAULT_BOXES = [(0.08, 0.1, 0.5, 0.25), (0.45, 0.35, 0.45, 0.25), (0.1, 0.55, 0.5, 0.25), (0.5, 0.1, 0.4, 0.25)]

SPEC = RecipeSpec(
    "product-demo", "Product demo", "Screenshots with zoom/pan callouts, narration and captions.",
    [
        FieldSpec("product", "Product name", "text", required=True),
        FieldSpec("url", "Product URL (text only, not crawled)", "text"),
        FieldSpec("screenshots", "Screenshots (1-8, in the order to show them)", "images", required=True),
        FieldSpec("features", "Key features (one per line)", "textarea", required=True),
        *_shared.tts_fields(),
        *common.caption_fields("bold-pop"),
        _shared.aspect_field(),
    ],
    paid=False, keys=("GEMINI_API_KEY",),
)


def title(params: dict) -> str:
    return f"{params['product'].strip()} demo"[:60]


def validate(params: dict, files: dict[str, list]) -> None:
    n = len(files.get("screenshots") or [])
    if n > MAX_SHOTS:
        raise ValidationError(f"'screenshots' (Screenshots): at most {MAX_SHOTS} images, you sent {n}.")


def stages(params: dict) -> list[Stage]:
    return [
        Stage("Writing the demo script", _write_script, "plan", weight=2),
        Stage("Recording the voice", _record, "plan", weight=2),
        Stage("Building callouts", _callouts, "render", weight=5),
        Stage("Finishing the video", _finish, "render", weight=2),
    ]


# --- plan phase ---


def _clamp_box(box: object, index: int) -> tuple[float, float, float, float]:
    """A highlight box as fractions (x, y, w, h) of the screenshot, kept inside it."""
    try:
        x, y, w, h = (float(v) for v in box)  # type: ignore[union-attr]
        if max(x, y, w, h) > 1.5:  # model answered in percent
            x, y, w, h = x / 100, y / 100, w / 100, h / 100
    except (TypeError, ValueError):
        x, y, w, h = DEFAULT_BOXES[index % len(DEFAULT_BOXES)]
    w, h = min(max(w, 0.1), 0.9), min(max(h, 0.08), 0.8)
    return (min(max(x, 0.0), 1 - w), min(max(y, 0.0), 1 - h), w, h)


def _write_script(ctx) -> None:
    p = ctx.params
    shots = ctx.inputs["screenshots"][:MAX_SHOTS]
    n = len(shots)
    features = [f.strip(" -*•\t") for f in p["features"].splitlines() if f.strip()]
    prompt = f"""You write the voice-over for a short product demo video and decide where each callout points.
Product: {p['product']}{f" ({p['url']})" if p.get('url') else ''}  (the web address is only a name: you cannot browse it)
Key features, in the order the maker cares about them:
{chr(10).join('- ' + f for f in features)}

You are given {n} screenshots, in order. Write exactly {n} beats, one per screenshot, in the same order. Each beat's narration is 1-2 short
sentences (about 14-24 words) explaining what the viewer sees and what it does for them, using only the features above and what is visible.
Do not invent numbers, prices, customers or claims. Beat 1 opens with a hook. No emoji, no markdown.
For each beat also give "label": a 2-4 word callout tag (e.g. "One-click export") and "box": [x, y, w, h] as fractions 0-1 of the screenshot
(top-left origin) around the single most relevant area to highlight.
Return JSON: {{"tagline": "<max 8 words>", "beats": [{{"narration": "<text>", "label": "<tag>", "box": [0.1, 0.2, 0.4, 0.2]}}]}}"""
    data = common.gemini_vision_json(prompt, shots)
    raw = data.get("beats") if isinstance(data.get("beats"), list) else data.get("items", [])
    beats = []
    for i in range(n):
        item = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        text = common.clean_line(item.get("narration")) or (f"{p['product']}: {features[i % len(features)]}." if features else f"Here is {p['product']}.")
        beats.append({
            "narration": text,
            "label": common.clip_text(item.get("label"), 32) or (features[i % len(features)][:28] if features else p["product"]),
            "box": list(_clamp_box(item.get("box"), i)),
        })
    ctx.data["beats"] = beats
    ctx.data["tagline"] = common.clip_text(data.get("tagline"), 70)
    ctx.plan.update(
        summary=f"{n}-screenshot demo of {p['product']} with zoom/pan callouts and an end card.",
        script="\n".join(b["narration"] for b in beats),
        notes="Callout boxes are chosen by Gemini from the screenshots; the product URL is used as text only.",
    )


def _record(ctx) -> None:
    p = ctx.params
    beats = ctx.data["beats"]
    result = tts.synthesize(
        [b["narration"] for b in beats], ctx.path("narration.wav"), provider=p.get("tts_provider", "edge"),
        voice=p.get("voice"), language="en", progress=ctx.progress,
    )
    ctx.register("narration", result.path, label="Narration", kind="audio", preview=True)
    ctx.data["tts"] = {"duration": result.duration, "sentences": result.sentences}
    sents = result.sentences
    # each beat's clip lasts from its first word to the start of the next beat (the last one to the end of the audio)
    bounds = [s["start"] for s in sents] + [result.duration]
    ctx.data["beat_seconds"] = [max(bounds[i + 1] - bounds[i], 1.0) for i in range(len(sents))]
    ctx.data["total"] = sum(ctx.data["beat_seconds"]) + END_CARD_S
    ctx.plan["scenes"] = [
        {"index": i + 1, "text": s["text"], "visual": f"Callout: {ctx.data['beats'][i]['label']}", "duration_s": round(ctx.data["beat_seconds"][i], 1)}
        for i, s in enumerate(sents)
    ]
    ctx.plan["summary"] += f" About {ctx.data['total']:.0f} s."


# --- Pillow frames ---


def _wrap(text: str, f, max_w: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if f.getlength(trial) > max_w and line:
            lines.append(line)
            line = word
        else:
            line = trial
    return [*lines, line] if line else lines


def _gradient(w: int, h: int) -> Image.Image:
    ramp = Image.linear_gradient("L").resize((1, h), Image.BILINEAR)
    top = Image.new("RGB", (w, h), BG_TOP)
    return Image.composite(Image.new("RGB", (w, h), BG_BOTTOM), top, ramp.resize((w, h)))


def callout_frame(shot: Path, dst: Path, w: int, h: int, *, box: tuple[float, float, float, float], label: str, product: str, step: str) -> Path:
    """The screenshot on a styled canvas with everything but the highlighted box dimmed, a frame around the box and a label tag."""
    tall = h > w
    canvas = _gradient(w, h).convert("RGBA")
    d = ImageDraw.Draw(canvas)
    u = min(w, h) / 1080
    head_font = captions.font(max(int(46 * u), 14))
    d.text((w * 0.07, h * (0.055 if tall else 0.06)), product.upper(), font=head_font, fill=(245, 247, 251, 255), anchor="lm")
    d.text((w * 0.93, h * (0.055 if tall else 0.06)), step, font=head_font, fill=(*ACCENT, 255), anchor="rm")

    area = (w * 0.06, h * (0.11 if tall else 0.13), w * 0.94, h * (0.70 if tall else 0.80))
    aw, ah = int(area[2] - area[0]), int(area[3] - area[1])
    src = Image.open(shot).convert("RGB")
    if tall and src.width / src.height > 1.15:  # a wide screenshot would be tiny on a tall frame: crop around the highlight
        sw, sh = src.size
        fx, fy, fw, fh = box
        target = aw / ah
        cw = min(sw, max(sh * target, fw * sw * 1.2))
        ch = min(sh, cw / target)
        left = min(max(fx * sw + fw * sw / 2 - cw / 2, 0), sw - cw)
        top = min(max(fy * sh + fh * sh / 2 - ch / 2, 0), sh - ch)
        src = src.crop((round(left), round(top), round(left + cw), round(top + ch)))
        x0, y0 = max((fx * sw - left) / cw, 0.0), max((fy * sh - top) / ch, 0.0)
        box = (x0, y0, min(fw * sw / cw, 1 - x0), min(fh * sh / ch, 1 - y0))
    ratio = min(aw / src.width, ah / src.height)
    iw, ih = max(int(src.width * ratio), 8), max(int(src.height * ratio), 8)
    src = src.resize((iw, ih), Image.LANCZOS).convert("RGBA")
    ox, oy = int(area[0] + (aw - iw) / 2), int(area[1] + (ah - ih) / 2)
    bx, by, bw, bh = (int(box[0] * iw), int(box[1] * ih), int(box[2] * iw), int(box[3] * ih))

    dim = Image.new("RGBA", (iw, ih), (4, 6, 16, 150))
    ImageDraw.Draw(dim).rounded_rectangle((bx, by, bx + bw, by + bh), radius=int(14 * u), fill=(0, 0, 0, 0))
    src.alpha_composite(dim)
    line = max(int(7 * u), 3)
    ImageDraw.Draw(src).rounded_rectangle((bx, by, bx + bw, by + bh), radius=int(14 * u), outline=(*ACCENT, 255), width=line)

    radius = int(28 * u)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((ox, oy + int(14 * u), ox + iw, oy + ih + int(14 * u)), radius=radius, fill=(0, 0, 0, 170))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(int(22 * u))))
    mask = Image.new("L", (iw, ih), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, iw - 1, ih - 1), radius=radius, fill=255)
    canvas.paste(src, (ox, oy), mask)

    tag_font = captions.font(max(int(58 * u), 16))
    tag = label.upper()
    tw = int(tag_font.getlength(tag)) + int(60 * u)
    th = int(100 * u)
    tx = min(max(ox + bx + bw // 2 - tw // 2, int(w * 0.05)), w - tw - int(w * 0.05))
    below = oy + by + bh + int(24 * u)
    ty = below if below + th < area[3] + h * 0.08 else max(oy + by - th - int(24 * u), int(h * 0.08))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((tx, ty, tx + tw, ty + th), radius=th // 2, fill=(*ACCENT, 255))
    d.text((tx + tw / 2, ty + th / 2 + 2 * u), tag, font=tag_font, fill=(8, 10, 22, 255), anchor="mm")
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dst)
    return dst


def end_card(dst: Path, w: int, h: int, *, product: str, tagline: str, url: str) -> Path:
    canvas = _gradient(w, h).convert("RGBA")
    d = ImageDraw.Draw(canvas)
    u = min(w, h) / 1080
    size = int(150 * u)
    f = captions.font(size)
    lines = _wrap(product.upper(), f, int(w * 0.84))
    while len(lines) > 3 and size > 40:
        size = int(size * 0.85)
        f = captions.font(size)
        lines = _wrap(product.upper(), f, int(w * 0.84))
    line_h = int(size * 1.12)
    top = h * 0.42 - line_h * len(lines) / 2
    for i, ln in enumerate(lines):
        d.text((w / 2, top + i * line_h), ln, font=f, fill=(245, 247, 251, 255), anchor="mm")
    d.rounded_rectangle((w / 2 - 90 * u, top + len(lines) * line_h + 10 * u, w / 2 + 90 * u, top + len(lines) * line_h + 22 * u), radius=int(6 * u), fill=(*ACCENT, 255))
    y = top + len(lines) * line_h + 80 * u
    sub = captions.font(max(int(56 * u), 14))
    for ln in _wrap(tagline.upper(), sub, int(w * 0.8))[:2]:
        d.text((w / 2, y), ln, font=sub, fill=(*ACCENT, 255), anchor="mm")
        y += 70 * u
    if url:
        d.text((w / 2, h * 0.66), re.sub(r"^https?://", "", url).rstrip("/")[:50], font=captions.font(max(int(46 * u), 12)), fill=(160, 170, 195, 255), anchor="mm")
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dst)
    return dst


# --- render phase ---


def _callouts(ctx) -> None:
    p = ctx.params
    w, h = ffmpeg.target_size(p["aspect"])
    shots = ctx.inputs["screenshots"][:MAX_SHOTS]
    beats, seconds = ctx.data["beats"], ctx.data["beat_seconds"]
    n = len(beats)
    clips = []
    for i, (shot, beat, secs) in enumerate(zip(shots, beats, seconds)):
        frame = callout_frame(shot, ctx.path("frames", f"{i}.png"), w, h, box=tuple(beat["box"]), label=beat["label"], product=p["product"], step=f"{i + 1}/{n}")
        clip = ctx.path("clips", f"{i}.mp4")
        images.ken_burns(frame, clip, secs, w, h, effect=EFFECTS[i % len(EFFECTS)])
        clips.append(clip)
        ctx.progress((i + 1) / (n + 1))
    card = end_card(ctx.path("frames", "end.png"), w, h, product=p["product"], tagline=ctx.data.get("tagline") or "", url=p.get("url", ""))
    end_clip = ctx.path("clips", "end.mp4")
    images.ken_burns(card, end_clip, END_CARD_S, w, h, effect="in")
    clips.append(end_clip)
    ffmpeg.concat(clips, ctx.path("demo.mp4"))
    for clip in clips:
        clip.unlink(missing_ok=True)


def _finish(ctx) -> None:
    p = ctx.params
    _shared.compose_final(
        ctx, ctx.path("demo.mp4"), seconds=ctx.data["total"], audio=ctx.asset("narration"), sentences=ctx.data["tts"]["sentences"],
        captions_on=bool(p.get("subtitles")), style=p.get("caption_style", "bold-pop"), position=p.get("subtitle_position", "bottom"),
    )

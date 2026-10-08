"""Recipes `ps1-lowpoly` and `spiderverse`: take footage (an AI-generated Veo clip, your own video, or your images
animated with Ken Burns), restyle it with an FFmpeg look, and add the style's extras (jitter + 15 fps for PS1;
halftone dots, comic caption boxes and 12 fps "on twos" for Spider-Verse). Optional narration and captions."""

from pathlib import Path

from PIL import Image, ImageDraw

from app.services.studio.engines import _shared
from app.services.studio.engines import _skill_common as common
from app.services.studio.kit import captions, clips, ffmpeg, images
from app.services.studio.registry import FieldSpec, RecipeSpec, Stage, StageError, ValidationError

MAX_TOTAL_S = 20.0
VEO_DURATIONS = (4, 6, 8)
VEO_RESOLUTION = "720p"
VEO_MODELS = [
    {"value": "veo-3.1-lite-generate-preview", "label": "Veo 3.1 Lite (cheapest)"},
    {"value": "veo-3.1-fast-generate-preview", "label": "Veo 3.1 Fast (better quality)"},
]
EFFECTS = ("in", "pan_right", "out", "pan_left")

STYLE_PACKS = {
    "ps1-lowpoly": (
        "PlayStation 1 era 3D game footage: low-poly models with flat-shaded faces, chunky low-resolution textures, simple geometry, "
        "limited draw distance with fog, early-3D-cutscene feel, slightly wobbly camera"
    ),
    "spiderverse": (
        "stylised comic-book animation like a graphic novel in motion: bold black ink outlines, Ben-Day halftone dot shading, "
        "off-register colour fringing, saturated pop-art palette, painterly 2D/3D hybrid look, dynamic camera"
    ),
}
STYLE_FILTER = {"ps1-lowpoly": "ps1", "spiderverse": "spiderverse"}
VIDEO_SOURCE = [
    {"value": "generate", "label": "Generate with an AI video model (paid)"},
    {"value": "upload", "label": "Use my own video / images"},
]


def spec(recipe_id: str, label: str, description: str) -> RecipeSpec:
    return RecipeSpec(
        recipe_id, label, description,
        [
            FieldSpec("idea", "Idea", "textarea", required=True),
            FieldSpec("source", "Footage source", "select", default="generate", options=VIDEO_SOURCE),
            FieldSpec("clip_model", "AI video model (if generating)", "select", default=VEO_MODELS[0]["value"], options=VEO_MODELS,
                      help="720p, 4-8 s. Lite costs about $0.05 per second, Fast about $0.10."),
            FieldSpec("video", "Your video (if using your own)", "video"),
            FieldSpec("images", "Your images (if using your own)", "images"),
            FieldSpec("duration", "Length (seconds)", "number", default=8, min=4, max=12),
            FieldSpec("narration", "AI voice-over", "toggle", default=False, help="Gemini writes a short script and a voice reads it."),
            *_shared.tts_fields(),
            *common.caption_fields("bold-pop"),
            _shared.aspect_field(),
        ],
        paid=True, keys=("GEMINI_API_KEY",),
    )


PS1 = spec("ps1-lowpoly", "PS1 low-poly", "Crunchy PlayStation-1 look: low resolution, dithering, jittery 15 fps.")
SPIDERVERSE = spec("spiderverse", "Spider-Verse comic", "Comic-book look: posterised colours, halftone feel, on-twos 12 fps and caption boxes.")


def title(params: dict) -> str:
    return params["idea"].strip().splitlines()[0][:60]


def validate(recipe_id: str, params: dict, files: dict[str, list]) -> None:
    if params.get("source", "generate") == "upload" and not (files.get("video") or files.get("images")):
        raise ValidationError("'source' (Footage source): upload a video or some images, or switch to generating one with AI.")


def stages(recipe_id: str, params: dict) -> list[Stage]:
    out = [Stage("Planning the look", lambda ctx: _plan(ctx, recipe_id), "plan")]
    if params.get("narration"):
        out.append(Stage("Recording the voice", _record, "plan", weight=2))
    return out + [
        Stage("Preparing the footage", lambda ctx: _source(ctx, recipe_id), "render", weight=6),
        Stage("Applying the style", lambda ctx: _stylise(ctx, recipe_id), "render", weight=3),
        Stage("Finishing the video", lambda ctx: _finish(ctx, recipe_id), "render", weight=2),
    ]


def veo_seconds(duration: float) -> int:
    return next((d for d in VEO_DURATIONS if d >= min(duration, VEO_DURATIONS[-1])), VEO_DURATIONS[-1])


def estimate_cost(params: dict, plan: dict) -> float | None:
    if params.get("source", "generate") != "generate":
        return 0.0
    cost = clips.estimate_cost("veo", params.get("clip_model", VEO_MODELS[0]["value"]), VEO_RESOLUTION, veo_seconds(float(params.get("duration", 8))))
    return cost


# --- plan phase ---


def _plan(ctx, recipe_id: str) -> None:
    p = ctx.params
    pack = STYLE_PACKS[recipe_id]
    generate = p.get("source", "generate") == "generate"
    ctx.data["pack"] = pack
    ctx.data["prompt"] = ""
    ctx.data["caption"] = common.clip_text(p["idea"], 60).upper()
    if generate or p.get("narration"):
        words = int(max(float(p["duration"]) - 1.0, 3) * common.WORDS_PER_SECOND)
        narration_spec = f"a voice-over of about {words} words, short punchy sentences, no stage directions or emoji" if p.get("narration") else "an empty string"
        data = common.ask_json(
            f"""You help make a short stylised video.
Idea: {p['idea']}
Visual style: {pack}

Return JSON with:
"prompt": one vivid paragraph (max 70 words) describing the single continuous shot for a video model: subject, action, setting, camera move, lighting, in the visual style above. No on-screen text or logos, no real people's names.
"caption": a punchy comic-style caption of at most 7 words for the opening, ALL CAPS (e.g. "MEANWHILE, IN THE PIXEL CITY...").
"narration": {narration_spec}"""
        )
        ctx.data["prompt"] = common.clip_text(data.get("prompt"), 600)
        ctx.data["caption"] = common.clip_text(data.get("caption"), 60).upper() or ctx.data["caption"]
        if p.get("narration"):
            text = common.clean_line(data.get("narration"))
            if len(text.split()) < 4:
                raise StageError("Gemini did not return a narration. Retry, or turn the voice-over off.")
            ctx.data["narration"] = text
            ctx.plan["script"] = text
    if generate and not ctx.data["prompt"]:
        raise StageError("Gemini did not return a shot description. Retry, or use your own footage.")
    total = float(p["duration"])
    source = {"generate": f"an AI-generated {veo_seconds(total)} s clip (Veo)", "upload": "your own footage"}[p.get("source", "generate")]
    ctx.data["total"] = total
    ctx.plan.update(
        summary=f"{total:.0f} s {p['aspect']} video in the {recipe_id.replace('-', ' ')} style from {source}.",
        scenes=[{"index": 1, "text": p["idea"].strip()[:200], "visual": ctx.data["prompt"] or "your footage, restyled", "duration_s": total}],
        notes=("Footage is generated once with Veo (paid, no sound); " if generate else "No paid generation: ") + "the style is applied with FFmpeg filters.",
    )


def _record(ctx) -> None:
    result = _shared.record_voice(ctx, ctx.data["narration"])
    ctx.data["total"] = min(max(float(ctx.params["duration"]), result.duration + 0.5), MAX_TOTAL_S)
    ctx.plan["scenes"][0]["duration_s"] = round(ctx.data["total"], 1)


# --- render phase ---


def _source(ctx, recipe_id: str) -> None:
    p = ctx.params
    w, h = ffmpeg.target_size(p["aspect"])
    total = ctx.data["total"]
    out = ctx.path("source.mp4")
    if p.get("source", "generate") == "generate":
        raw = ctx.path("generated.mp4")
        if not raw.is_file():  # never pay twice if a later step in this stage fails and is retried
            clips.generate_clip(
                f"{ctx.data['prompt']} Style: {ctx.data['pack']}. No text, no logos.", raw, provider="veo",
                model=p.get("clip_model", VEO_MODELS[0]["value"]), aspect_ratio="9:16" if p["aspect"] == "9:16" else "16:9",
                duration_seconds=veo_seconds(float(p["duration"])), resolution=VEO_RESOLUTION, timeout=min(clips.CLIP_TIMEOUT_S, ctx.remaining()),
            )
        ctx.progress(0.8)
        ffmpeg.normalize_clip(raw, out, w, h, total, loop=True)
    elif ctx.inputs.get("video"):
        ffmpeg.normalize_clip(ctx.inputs["video"][0], out, w, h, total, loop=True)
    else:
        pics = ctx.inputs["images"][:12]
        per = max(total / len(pics), 1.0)
        parts = []
        for i, pic in enumerate(pics):
            part = ctx.path("parts", f"{i}.mp4")
            images.ken_burns(pic, part, per, w, h, effect=EFFECTS[i % len(EFFECTS)])
            parts.append(part)
            ctx.progress((i + 1) / len(pics))
        joined = ffmpeg.concat(parts, ctx.path("joined.mp4"))
        ffmpeg.normalize_clip(joined, out, w, h, total, loop=True)
        for part in parts:
            part.unlink(missing_ok=True)
        joined.unlink(missing_ok=True)


def style_filter(recipe_id: str, width: int) -> str:
    """The FFmpeg look: kit's stylise filter, plus a vertex-snap style jitter in front of it for PS1."""
    base = ffmpeg.stylise_filter(STYLE_FILTER[recipe_id])
    if recipe_id != "ps1-lowpoly":
        return base
    pad = max(round(width * 0.012), 4)
    amp = pad * 0.6
    return (
        f"scale=iw+{2 * pad}:ih+{2 * pad},crop=iw-{2 * pad}:ih-{2 * pad}:x='{pad}+{amp:.2f}*sin(n*2.3)':y='{pad}+{amp:.2f}*cos(n*1.9)',{base}"
    )


def _stylise(ctx, recipe_id: str) -> None:
    w, h = ffmpeg.target_size(ctx.params["aspect"])
    vf = f"{style_filter(recipe_id, w)},scale={w}:{h}:flags=neighbor,setsar=1"  # the /4 pixelation can round the size down
    ffmpeg.run(["-i", ctx.path("source.mp4"), "-vf", vf, "-t", f"{ctx.data['total']:.3f}", "-an", *ffmpeg.VIDEO_ARGS, ctx.path("styled.mp4")])
    ctx.path("source.mp4").unlink(missing_ok=True)


def _finish(ctx, recipe_id: str) -> None:
    p = ctx.params
    total = ctx.data["total"]
    styled = ctx.path("styled.mp4")
    tts_data = ctx.data.get("tts")
    audio = ctx.asset("narration") if tts_data else None
    sentences = tts_data["sentences"] if tts_data else None
    if recipe_id == "ps1-lowpoly":
        _shared.compose_final(
            ctx, styled, seconds=total, audio=audio, sentences=sentences, captions_on=bool(p.get("subtitles")) and bool(tts_data),
            style=p.get("caption_style", "bold-pop"), position=p.get("subtitle_position", "bottom"),
        )
        return
    probe = ffmpeg.probe(styled)
    size = (probe.width or 1080, probe.height or 1920)
    overlays = [(halftone_overlay(ctx.path("overlays", "halftone.png"), *size), 0.0, total, 0, 0)]
    comic_on = bool(p.get("subtitles")) and bool(tts_data)
    if comic_on:  # narration shows up as comic caption boxes instead of subtitles
        for i, s in enumerate(sentences):
            path, x, y = comic_box(ctx.path("overlays", f"box_{i}.png"), s["text"], size)
            overlays.append((path, s["start"], min(s["end"] + 0.3, total), x, y))
    else:
        path, x, y = comic_box(ctx.path("overlays", "title.png"), ctx.data["caption"], size)
        overlays.append((path, 0.3, min(3.2, total), x, y))
    common.compose_with_overlays(ctx, styled, seconds=total, audio=audio, extra_overlays=overlays)


# --- Pillow overlays (spider-verse) ---


def halftone_overlay(dst: Path, w: int, h: int) -> Path:
    """Transparent PNG of Ben-Day dots that grow towards the bottom-left and top-right corners."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    step = max(round(w / 54), 6)
    for row, y in enumerate(range(0, h + step, step)):
        for x in range(-step * (row % 2) // 2, w + step, step):
            # 0 at the middle diagonal, 1 in the bottom-left / top-right corners
            k = abs((x / w) - (y / h)) ** 1.2
            r = step * 0.46 * max(0.0, min((k - 0.35) / 0.65, 1.0))
            if r > 0.8:
                d.ellipse((x - r, y - r, x + r, y + r), fill=(15, 10, 40, 95))
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst


def comic_box(dst: Path, text: str, frame: tuple[int, int], *, y_frac: float = 0.07) -> tuple[Path, int, int]:
    """Yellow comic caption box with a black border and hard shadow. Returns (png, x, y) for the overlay position."""
    fw, fh = frame
    size = round(min(fw, fh) * 0.055)
    f = captions.font(size)
    max_w = int(fw * 0.8)
    lines, line = [], ""
    for word in text.upper().split():
        trial = f"{line} {word}".strip()
        if f.getlength(trial) > max_w and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    lines = lines[:4]
    pad = round(size * 0.5)
    line_h = round(size * 1.15)
    bw = int(max(f.getlength(ln) for ln in lines)) + 2 * pad
    bh = line_h * len(lines) + 2 * pad - round(size * 0.1)
    shadow = max(round(size * 0.14), 4)
    border = max(round(size * 0.1), 3)
    img = Image.new("RGBA", (bw + shadow, bh + shadow), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((shadow, shadow, bw + shadow, bh + shadow), fill=(0, 0, 0, 255))
    d.rectangle((0, 0, bw, bh), fill=(255, 221, 0, 255), outline=(0, 0, 0, 255), width=border)
    for i, ln in enumerate(lines):
        d.text((pad, pad + i * line_h - round(size * 0.05)), ln, font=f, fill=(0, 0, 0, 255))
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst, round(fw * 0.06), round(fh * y_frac)

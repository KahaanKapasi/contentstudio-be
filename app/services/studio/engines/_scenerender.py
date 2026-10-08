"""Render-side helpers of the scene-film core (see _scenefilm.py): camera moves on stills, atomic file writes,
PCM track building (every shot's audio is a 24 kHz mono wav fitted to the shot length), the end card and the
burned-in "AI parody" tag, and the final caption/tag/audio encode."""

import wave
from pathlib import Path

from PIL import Image, ImageDraw

from app.services.studio import registry
from app.services.studio.kit import captions, ffmpeg, images, tts

SR = tts.SAMPLE_RATE
CAMERA_CYCLE = ("in", "pan_right", "out", "pan_left")
EFFECTS = (*images.KEN_BURNS_EFFECTS, "shake")


# --- files ---


def part_path(dest: Path) -> Path:
    """Sibling path ffmpeg writes to before it is renamed to `dest`, so a crash never leaves a half-written clip."""
    return dest.with_name(dest.stem + ".part" + dest.suffix)


def commit(part: Path, dest: Path) -> Path:
    part.replace(dest)
    return dest


# --- audio ---


def read_pcm(path: Path | None) -> bytes:
    if not path or not Path(path).is_file():
        return b""
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SR):
            raise ValueError(f"{Path(path).name} is not 24 kHz mono 16-bit audio.")
        return w.readframes(w.getnframes())


def fit_pcm(pcm: bytes, seconds: float) -> bytes:
    """Trim or zero-pad to exactly `seconds`."""
    n = round(seconds * SR) * 2
    return pcm[:n] + b"\x00" * max(0, n - len(pcm))


def write_pcm(dest: Path, pcm: bytes) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return dest


def build_track(dest: Path, wavs: list[Path | None], durations: list[float]) -> Path:
    """One wav: each shot's audio (or silence) fitted to its shot length, back to back."""
    return write_pcm(dest, b"".join(fit_pcm(read_pcm(w), d) for w, d in zip(wavs, durations)))


def extract_audio(video: Path, dest: Path) -> Path | None:
    """The clip's own soundtrack as 24 kHz mono wav, or None when it has none."""
    if not ffmpeg.probe(video).has_audio:
        return None
    ffmpeg.run(["-i", video, "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", dest])
    return dest


# --- camera moves on stills ---


def pick_effect(camera: str, index: int) -> str:
    c = (camera or "").lower()
    if any(k in c for k in ("shake", "handheld", "impact", "whip", "rumble", "crash")):
        return "shake"
    if "pan left" in c or "pan_left" in c:
        return "pan_left"
    if "pan right" in c or "pan_right" in c or "tracking" in c:
        return "pan_right"
    if any(k in c for k in ("pull back", "pull out", "zoom out", "dolly out", "reveal")):
        return "out"
    if any(k in c for k in ("push", "zoom in", "dolly in", "close")):
        return "in"
    return CAMERA_CYCLE[index % len(CAMERA_CYCLE)]


def shake(image: Path, dst: Path, seconds: float, w: int, h: int) -> Path:
    """Handheld jitter: a slightly oversized still, cropped with a small sinusoidal offset per frame."""
    big_w, big_h = round(w * 1.12) // 2 * 2, round(h * 1.12) // 2 * 2
    amp = max(w * 0.018, 2)
    vf = (
        f"{ffmpeg.cover_filter(big_w, big_h)},crop={w}:{h}:x='(iw-{w})/2+{amp:.2f}*sin(t*37)':y='(ih-{h})/2+{amp:.2f}*cos(t*43)',format=yuv420p"
    )
    ffmpeg.run(["-loop", "1", "-framerate", str(ffmpeg.FPS), "-i", image, "-t", f"{seconds:.3f}", "-vf", vf, "-an", *ffmpeg.VIDEO_ARGS, dst])
    return dst


def still_to_clip(image: Path, dst: Path, seconds: float, w: int, h: int, effect: str) -> Path:
    if effect == "shake":
        return shake(image, dst, seconds, w, h)
    return images.ken_burns(image, dst, seconds, w, h, effect=effect)


# --- cards and tags ---


def render_card(text: str, dest: Path, w: int, h: int, *, bg=(255, 212, 0), fg=(18, 18, 18)) -> Path:
    """Punchline end card: big Anton text centred on a flat colour."""
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    size = round(min(w, h) * 0.105)
    f = captions.font(size)
    lines, line = [], ""
    for word in text.upper().split():
        trial = f"{line} {word}".strip()
        if line and f.getlength(trial) > w * 0.84:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    lh = round(size * 1.2)
    y = (h - lh * len(lines)) / 2
    for row in lines:
        draw.text((w / 2, y), row, font=f, fill=fg, anchor="mt")
        y += lh
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return dest


def render_tag(dest: Path, w: int, h: int, text: str = "AI PARODY") -> tuple[Path, int, int]:
    """Small translucent pill, top-left. Returns (png, x, y) for the overlay filter."""
    fs = max(round(min(w, h) * 0.042), 10)
    f = captions.font(fs)
    pad = round(fs * 0.45)
    pw, ph = round(f.getlength(text)) + 2 * pad, round(fs * 1.25) + pad
    img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=ph // 3, fill=(0, 0, 0, 170))
    d.text((pw / 2, ph / 2), text, font=f, fill=(255, 255, 255, 255), anchor="mm")
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return dest, round(w * 0.04), round(h * 0.06)


# --- captions ---


def even_words(text: str, start: float, end: float) -> list[dict]:
    """Word timings split proportionally to word length (used when the voice is not ours, e.g. native audio)."""
    words = text.split()
    if not words:
        return []
    total = sum(len(w) + 1 for w in words)
    out, cursor = [], start
    for w in words:
        span = (end - start) * (len(w) + 1) / total
        out.append({"text": w, "start": cursor, "end": cursor + span})
        cursor += span
    return out


def offset_sentences(sentences: list[dict], t0: float) -> list[dict]:
    return [
        {"text": s["text"], "start": s["start"] + t0, "end": s["end"] + t0,
         "words": [{**w, "start": w["start"] + t0, "end": w["end"] + t0} for w in s.get("words") or []]}
        for s in sentences
    ]


def finalize_film(
    ctx, video: Path, *, audio: Path | None, seconds: float, sentences: list[dict], subtitles: bool, style: str, tag: str | None,
) -> Path:
    """Burn captions (ASS, or PNG overlays without libass) and the optional tag, mux audio, encode final.mp4."""
    probe = ffmpeg.probe(video)
    size = (probe.width or 1080, probe.height or 1920)
    ass = None
    overlays: list = []
    if subtitles and sentences:
        if ffmpeg.subtitle_filter_name():
            ass = captions.write_ass(ctx.path("captions.ass"), sentences, style=style, size=size, position="bottom")
        else:
            overlays += captions.render_overlays(ctx.path("caps"), sentences, style=style, size=size, position="bottom")
    if tag:
        png, x, y = render_tag(ctx.path("tag.png"), size[0], size[1], tag)
        overlays.append((png, 0.0, seconds, x, y))
    return ffmpeg.finalize(
        video, ctx.path(registry.FINAL_NAME), audio=audio, ass=ass, fonts_dir=captions.FONTS_DIR if ass else None,
        overlays=overlays or None, seconds=seconds,
    )

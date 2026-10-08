"""Helpers shared by the V3 recipe modules (singing story, podcast, PS1 / Spider-Verse, product demo)."""

import io
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from app.config import settings
from app.services.costs import prices
from app.services import gemini_client
from app.services.studio import registry
from app.services.studio.kit import captions, ffmpeg
from app.services.studio.registry import StageError

IMAGE_COST_USD = prices.usd("gemini.image.out")  # Gemini image generation, per 1K image (costs/prices.py)
WORDS_PER_SECOND = 2.5  # relaxed spoken pace, used to size scripts

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


# --- text / JSON ---


def clip_text(text: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def clean_line(text: object) -> str:
    """One speakable line: no markdown, no stage directions."""
    text = re.sub(r"[\[\(][^\]\)]*[\]\)]", "", str(text or ""))
    text = re.sub(r"[*_`#]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_json(text: str) -> dict | list:
    """Parse a model reply that should be JSON (tolerates code fences and surrounding prose)."""
    text = (text or "").strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = text.find(open_c), text.rfind(close_c)
        if 0 <= a < b:
            try:
                return json.loads(text[a : b + 1])
            except ValueError:
                continue
    raise StageError("Gemini returned something that could not be read. Retry this step.")


def ask_json(prompt: str) -> dict:
    """Gemini JSON object (a bare list is wrapped as {"items": [...]})."""
    data = gemini_client.generate_json(prompt)
    if isinstance(data, list):
        return {"items": data}
    return data if isinstance(data, dict) else {}


def gemini_vision_json(prompt: str, images: list[Path]) -> dict:
    """Gemini JSON reply to a prompt plus (downscaled) images. Raises StageError if the reply is unreadable."""
    from google.genai import types

    parts: list = [prompt]
    for path in images:
        img = Image.open(path).convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))
    response = gemini_client.get_client().models.generate_content(
        model=settings.gemini_text_model, contents=parts, config={"response_mime_type": "application/json"}
    )
    data = parse_json(response.text or "")
    return data if isinstance(data, dict) else {"items": data}


# --- timings ---


def words_between(text: str, start: float, end: float) -> list[dict]:
    """Spread the words of `text` over [start, end], proportional to word length."""
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 2 for w in words]
    span = max(end - start, 0.1)
    total = sum(weights)
    out, cursor = [], start
    for w, wt in zip(words, weights):
        nxt = cursor + span * wt / total
        out.append({"text": w, "start": cursor, "end": nxt})
        cursor = nxt
    return out


def sentence(text: str, start: float, end: float, words: list[dict] | None = None) -> dict:
    return {"text": text, "start": start, "end": end, "words": words or words_between(text, start, end)}


def shift_words(words: list[dict], offset: float, limit: float) -> list[dict]:
    return [{"text": w["text"], "start": min(w["start"] + offset, limit), "end": min(w["end"] + offset, limit)} for w in words]


# --- audio ---


def audio_envelope(path: Path, fps: int) -> list[float]:
    """Loudness (RMS) per video frame, 0..1, from the real audio (ffmpeg -> mono PCM -> numpy), lightly smoothed."""
    rate = 16000
    proc = subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(rate), "pipe:1"],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise StageError("Could not read the voice audio to animate the waveform.")
    x = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size == 0:
        return [0.0]
    idx = (np.arange(x.size) * fps) // rate
    counts = np.bincount(idx)
    rms = np.sqrt(np.bincount(idx, weights=x * x) / np.maximum(counts, 1))
    loud = rms[rms > 0.01]
    ref = float(np.percentile(loud, 95)) if loud.size else 1.0
    level = np.clip(rms / max(ref, 1e-4), 0, 1) ** 0.8
    smooth = np.convolve(np.pad(level, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    return [round(float(v), 3) for v in smooth]


def wav_frames(path: Path) -> tuple[tuple, bytes]:
    with wave.open(str(path), "rb") as w:
        return w.getparams()[:3], w.readframes(w.getnframes())


def assemble_wav(parts: list[tuple[Path, float, float]], dst: Path) -> Path:
    """Place wav files (all the same 16-bit mono format) at given start times inside one wav.
    `parts` = [(wav, start_s, slot_s)]; each file is padded or clipped to its slot."""
    fmt, rate = None, None
    chunks: list[bytes] = []
    for path, _start, slot in parts:
        (channels, width, framerate), data = wav_frames(path)
        fmt, rate = (channels, width), framerate
        want = int(round(slot * framerate)) * channels * width
        chunks.append(data[:want].ljust(want, b"\x00"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as out:
        out.setnchannels(fmt[0])
        out.setsampwidth(fmt[1])
        out.setframerate(rate)
        out.writeframes(b"".join(chunks))
    return dst


# --- final compose with extra PNG overlays ---


def compose_with_overlays(
    ctx, video: Path, *, seconds: float, audio: Path | None = None, sentences: list[dict] | None = None,
    captions_on: bool = False, style: str = "bold-pop", position: str = "bottom",
    extra_overlays: list[tuple[Path, float, float, int, int]] | None = None,
) -> Path:
    """Like `_shared.compose_final` but lets a recipe add its own PNG overlays (comic boxes, halftone) as well."""
    probe = ffmpeg.probe(video)
    size = (probe.width or 1080, probe.height or 1920)
    ass = None
    overlays = list(extra_overlays or [])
    if captions_on and sentences:
        if ffmpeg.subtitle_filter_name():
            ass = captions.write_ass(ctx.path("captions.ass"), sentences, style=style, size=size, position=position)
        else:
            overlays += captions.render_overlays(ctx.path("caps"), sentences, style=style, size=size, position=position)
    return ffmpeg.finalize(
        video, ctx.path(registry.FINAL_NAME), audio=audio, ass=ass, fonts_dir=captions.FONTS_DIR if ass else None,
        overlays=overlays or None, seconds=seconds,
    )


def caption_fields(default_style: str = "bold-pop"):
    """The shared caption fields with a recipe-specific default style."""
    from app.services.studio.engines import _shared

    fields = _shared.caption_fields(default_on=True)
    for f in fields:
        if f.name == "caption_style":
            f.default = default_style
    return fields

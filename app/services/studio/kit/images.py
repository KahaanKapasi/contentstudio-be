"""Gemini image generation (GEMINI_IMAGE_MODEL) and Ken-Burns stills for the scene pipelines."""

import io
from pathlib import Path

from PIL import Image

from app.config import settings
from app.services import gemini_client
from app.services.studio.kit import ffmpeg

KEN_BURNS_EFFECTS = ("in", "out", "pan_left", "pan_right")
_ASPECTS = {"9:16", "16:9", "1:1", "3:4", "4:3"}


class ImageGenError(RuntimeError):
    """Image generation failed; the message is safe to show the user."""


def generate_image(prompt: str, dest: Path, *, aspect: str = "9:16", references: list[Path] | None = None) -> Path:
    """One Gemini image (optionally conditioned on reference images) saved as PNG at `dest`."""
    from google.genai import errors as genai_errors
    from google.genai import types

    if aspect not in _ASPECTS:
        raise ValueError(f"Unsupported image aspect '{aspect}'.")
    contents: list = [prompt]
    for ref in references or []:
        data = Path(ref).read_bytes()
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        contents.append(types.Part.from_bytes(data=data, mime_type=mime))
    config = types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio=aspect))
    try:
        response = gemini_client.get_client().models.generate_content(model=settings.gemini_image_model, contents=contents, config=config)
    except genai_errors.APIError as exc:
        raise ImageGenError(f"Gemini image generation failed ({getattr(exc, 'code', '')}). Try again or reword the prompt.") from exc
    for candidate in response.candidates or []:
        for part in (candidate.content.parts if candidate.content else None) or []:
            if part.inline_data and part.inline_data.data:
                dest.parent.mkdir(parents=True, exist_ok=True)
                Image.open(io.BytesIO(part.inline_data.data)).convert("RGB").save(dest, format="PNG")
                return dest
    raise ImageGenError("Gemini returned no image (the prompt may have been blocked). Try rewording it.")


def ken_burns(image: Path, dst: Path, seconds: float, w: int, h: int, *, effect: str = "in") -> Path:
    """Still -> video with a slow zoom/pan (zoompan on a 2x upscaled copy so motion stays smooth)."""
    if effect not in KEN_BURNS_EFFECTS:
        raise ValueError(f"Unknown Ken Burns effect '{effect}'.")
    frames = max(round(seconds * ffmpeg.FPS), 1)
    step = 0.22 / frames
    centre = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    expr = {
        "in": f"z='min(zoom+{step:.6f},1.22)':{centre}",
        "out": f"z='if(eq(on,0),1.22,max(zoom-{step:.6f},1.0))':{centre}",
        "pan_left": f"z=1.2:x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'",
        "pan_right": f"z=1.2:x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'",
    }[effect]
    vf = f"{ffmpeg.cover_filter(w * 2, h * 2)},zoompan={expr}:d={frames}:s={w}x{h}:fps={ffmpeg.FPS},format=yuv420p"
    ffmpeg.run(["-i", image, "-vf", vf, "-frames:v", str(frames), "-an", *ffmpeg.VIDEO_ARGS, dst])
    return dst

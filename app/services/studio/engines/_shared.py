"""Pieces the engines share: language/voice/aspect fields, the TTS stage body, and the final compose step."""

from pathlib import Path

from app.services.studio import registry
from app.services.studio.kit import captions, ffmpeg, tts
from app.services.studio.registry import FieldSpec

LANGUAGES = [
    ("en", "English (US)"), ("en-GB", "English (UK)"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("pt", "Portuguese"), ("it", "Italian"), ("hi", "Hindi"), ("ar", "Arabic"),
]
LANGUAGE_NAMES = {code: name.split(" (")[0] for code, name in LANGUAGES}
ASPECTS = [{"value": "9:16", "label": "9:16 (Reels / Shorts)"}, {"value": "16:9", "label": "16:9 (YouTube)"}, {"value": "1:1", "label": "1:1 (Square)"}]


def aspect_field(default: str = "9:16") -> FieldSpec:
    return FieldSpec("aspect", "Aspect ratio", "select", default=default, options=ASPECTS)


def tts_fields() -> list[FieldSpec]:
    return [
        FieldSpec("tts_provider", "Voice provider", "select", default="edge", options=tts.PROVIDER_OPTIONS,
                  help="Edge is free. Gemini voices sound more natural but use your Gemini quota."),
        FieldSpec("voice", "Voice", "select", default="", options=tts.VOICE_OPTIONS,
                  help="Pick a voice that matches the provider; otherwise the provider's default for the language is used."),
    ]


def caption_fields(default_on: bool = True) -> list[FieldSpec]:
    return [
        FieldSpec("subtitles", "Burn-in captions", "toggle", default=default_on),
        FieldSpec("subtitle_position", "Caption position", "select", default="bottom",
                  options=[{"value": p, "label": p.capitalize()} for p in captions.POSITIONS]),
        FieldSpec("caption_style", "Caption style", "select", default="bold-pop",
                  options=[{"value": "bold-pop", "label": "Bold pop (word highlight)"}, {"value": "clean", "label": "Clean box"}, {"value": "karaoke", "label": "Karaoke sweep"}]),
    ]


def record_voice(ctx, text: str, *, language: str = "en", label: str = "Narration") -> tts.TTSResult:
    """Synthesize `text` into narration.wav, keep timings in ctx.data["tts"], expose the audio as a preview."""
    p = ctx.params
    result = tts.synthesize(
        tts.split_sentences(text), ctx.path("narration.wav"), provider=p.get("tts_provider", "edge"),
        voice=p.get("voice"), language=language, progress=ctx.progress,
    )
    ctx.data["tts"] = {"duration": result.duration, "sentences": result.sentences}
    ctx.register("narration", result.path, label=label, kind="audio", preview=True)
    return result


def compose_final(ctx, video: Path, *, seconds: float, audio: Path | None = None, sentences: list[dict] | None = None,
                  captions_on: bool = False, style: str = "bold-pop", position: str = "bottom", bgm: Path | None = None) -> Path:
    """Mix optional bgm under the voice, burn captions (ASS, or PNG overlays when libass is missing), encode final.mp4."""
    probe = ffmpeg.probe(video)
    size = (probe.width or 1080, probe.height or 1920)
    if bgm:
        audio = ffmpeg.mix_audio(audio, bgm, ctx.path("mix.wav"), seconds)
    ass = overlays = None
    if captions_on and sentences:
        if ffmpeg.subtitle_filter_name():
            ass = captions.write_ass(ctx.path("captions.ass"), sentences, style=style, size=size, position=position)
        else:
            overlays = captions.render_overlays(ctx.path("caps"), sentences, style=style, size=size, position=position)
    return ffmpeg.finalize(
        video, ctx.path(registry.FINAL_NAME), audio=audio, ass=ass, fonts_dir=captions.FONTS_DIR if ass else None,
        overlays=overlays, seconds=seconds,
    )

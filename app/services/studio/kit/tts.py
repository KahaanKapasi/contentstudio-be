"""Text-to-speech, one sentence at a time so every sentence has an exact start/end in the final track.

Providers: `edge` (edge-tts, free, real word boundaries) and `gemini` (GEMINI_TTS_MODEL through
google-genai generate_content with response_modalities=["AUDIO"]; word times are a proportional split).
All audio is normalised to 24 kHz mono 16-bit PCM and joined in Python with a short gap, so the
returned timings are sample-exact.
"""

import re
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.studio.kit import ffmpeg

SAMPLE_RATE = 24000
GAP_S = 0.2
GEMINI_RETRIES = 3
TTS_TOKENS_PER_SECOND = 25  # audio output tokens per second of speech (used only when usage_metadata is absent)

EDGE_DEFAULT_VOICES = {
    "en": "en-US-AndrewNeural", "en-US": "en-US-AndrewNeural", "en-GB": "en-GB-RyanNeural",
    "es": "es-ES-AlvaroNeural", "fr": "fr-FR-HenriNeural", "de": "de-DE-ConradNeural",
    "pt": "pt-BR-AntonioNeural", "hi": "hi-IN-MadhurNeural", "it": "it-IT-DiegoNeural", "ar": "ar-SA-HamedNeural",
}
GEMINI_DEFAULT_VOICE = "Kore"
GEMINI_VOICES = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
    "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia",
    "Sadaltager", "Sulafat",
)
# Shown in the UI as one list; a voice that does not belong to the chosen provider falls back to its default.
VOICE_OPTIONS = [{"value": "", "label": "Auto (by language)"}] + [
    {"value": v, "label": label}
    for v, label in (
        ("en-US-AndrewNeural", "Andrew (Edge, US male)"), ("en-US-AriaNeural", "Aria (Edge, US female)"),
        ("en-GB-RyanNeural", "Ryan (Edge, UK male)"), ("en-GB-SoniaNeural", "Sonia (Edge, UK female)"),
        ("en-US-GuyNeural", "Guy (Edge, US male)"), ("en-AU-WilliamNeural", "William (Edge, AU male)"),
    )
] + [{"value": v, "label": f"{v} (Gemini)"} for v in ("Kore", "Puck", "Charon", "Fenrir", "Aoede", "Zephyr", "Orus", "Leda")]
PROVIDER_OPTIONS = [{"value": "edge", "label": "Edge TTS (free)"}, {"value": "gemini", "label": "Gemini TTS"}]


class TTSError(RuntimeError):
    """Synthesis failed; the message is safe to show the user."""


@dataclass
class TTSResult:
    path: Path
    duration: float
    sentences: list[dict]  # [{text, start, end, words: [{text, start, end}]}]


def split_sentences(text: str, min_words: int = 3) -> list[str]:
    """Split on sentence punctuation; fragments shorter than `min_words` merge into the next sentence."""
    text = re.sub(r"\s+", " ", text.strip())
    parts = [p.strip() for p in re.split(r"(?<=[.!?…。！？])\s+", text) if p.strip()]
    merged: list[str] = []
    carry = ""
    for i, part in enumerate(parts):
        part = f"{carry} {part}".strip()
        carry = ""
        if len(part.split()) < min_words and i < len(parts) - 1:
            carry = part
        else:
            merged.append(part)
    return merged


def resolve_voice(provider: str, voice: str | None, language: str = "en") -> str:
    voice = (voice or "").strip()
    if provider == "gemini":
        return voice if voice in GEMINI_VOICES else GEMINI_DEFAULT_VOICE
    if re.fullmatch(r"[a-z]{2,3}-[A-Za-z0-9-]+Neural", voice):
        return voice
    return EDGE_DEFAULT_VOICES.get(language) or EDGE_DEFAULT_VOICES.get(language.split("-")[0], EDGE_DEFAULT_VOICES["en"])


def synthesize(
    sentences: list[str], out_path: Path, *, provider: str = "edge", voice: str | None = None, language: str = "en",
    voices: list[str | None] | None = None, progress=None,
) -> TTSResult:
    """Synthesize each sentence and join them into one wav at `out_path`. `voices[i]` overrides `voice` for
    sentence i (character voices; same provider). `progress(fraction)` is optional."""
    pairs = [(s.strip(), (voices[i] if voices and i < len(voices) else None) or voice) for i, s in enumerate(sentences)]
    pairs = [(s, v) for s, v in pairs if s]
    if not pairs:
        raise TTSError("There is no text to turn into speech.")
    sentences = [s for s, _ in pairs]
    pcm_parts: list[bytes] = []
    timings: list[dict] = []
    cursor = 0.0
    gap = b"\x00\x00" * int(SAMPLE_RATE * GAP_S)
    for i, (text, voice_i) in enumerate(pairs):
        voice = resolve_voice(provider, voice_i, language)
        if provider == "gemini":
            pcm, words = _gemini_sentence(text, voice), None
        elif provider == "edge":
            pcm, words = _edge_sentence(text, voice)
        else:
            raise TTSError(f"Unknown TTS provider '{provider}'.")
        length = len(pcm) / 2 / SAMPLE_RATE
        timings.append({"text": text, "start": cursor, "end": cursor + length, "words": _word_timings(text, words, cursor, length)})
        pcm_parts += [pcm, gap]
        cursor += length + GAP_S
        if progress:
            progress((i + 1) / len(sentences))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(pcm_parts))
    return TTSResult(out_path, cursor, timings)


def _word_timings(text: str, boundaries: list[tuple[str, float, float]] | None, offset: float, length: float) -> list[dict]:
    """Edge boundaries when present (clamped into the sentence), else a split proportional to word length."""
    if boundaries:
        return [
            {"text": w, "start": offset + min(s, length), "end": offset + min(max(e, s), length)}
            for w, s, e in boundaries
        ]
    words = text.split()
    weights = [len(w) + 1 for w in words]
    total, t = sum(weights), offset
    out = []
    for w, weight in zip(words, weights):
        d = length * weight / total
        out.append({"text": w, "start": t, "end": t + d})
        t += d
    return out


# --- edge ---


def _edge_sentence(text: str, voice: str) -> tuple[bytes, list[tuple[str, float, float]]]:
    mp3, boundaries = _edge_fetch(text, voice)
    return _to_pcm(mp3), boundaries


def _edge_fetch(text: str, voice: str) -> tuple[bytes, list[tuple[str, float, float]]]:
    """(mp3 bytes, [(word, start_s, end_s)]). Separate function so tests can replace the network call."""
    import edge_tts

    audio, words = b"", []
    try:
        for chunk in edge_tts.Communicate(text, voice, boundary="WordBoundary").stream_sync():
            if chunk["type"] == "audio":
                audio += chunk["data"]
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append((chunk["text"], start, start + chunk["duration"] / 1e7))
    except Exception as exc:
        raise TTSError(f"Edge TTS failed ({type(exc).__name__}). Check the connection or pick another voice.") from exc
    if not audio:
        raise TTSError("Edge TTS returned no audio. Try a different voice.")
    return audio, words


def _to_pcm(data: bytes) -> bytes:
    proc = subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=data, capture_output=True, timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise TTSError("Could not decode the synthesized audio.")
    return proc.stdout


# --- gemini ---


def _gemini_sentence(text: str, voice: str) -> bytes:
    from google.genai import errors as genai_errors
    from google.genai import types

    from app.services import gemini_client

    client = gemini_client.get_client()
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
        ),
    )
    for attempt in range(GEMINI_RETRIES):
        try:
            response = client.models.generate_content(model=settings.gemini_tts_model, contents=text, config=config)
            part = response.candidates[0].content.parts[0].inline_data
            pcm = _gemini_audio_to_pcm(part.data)
            gemini_client.record_usage(response, kind="tts", fallback=int(len(pcm) / 2 / SAMPLE_RATE * TTS_TOKENS_PER_SECOND))
            return pcm
        except (genai_errors.APIError, IndexError, AttributeError, TypeError) as exc:
            retryable = getattr(exc, "code", None) in (429, 500, 503) or not isinstance(exc, genai_errors.APIError)
            if attempt == GEMINI_RETRIES - 1 or not retryable:
                raise TTSError("Gemini TTS could not synthesize this sentence. Try the Edge voice or retry.") from exc
            time.sleep(2 * (attempt + 1))
    raise TTSError("Gemini TTS failed.")  # pragma: no cover


def _gemini_audio_to_pcm(data: bytes) -> bytes:
    """gemini-3.8-flash-tts answers with a RIFF wav, the 2.5 preview models with raw L16 PCM at 24 kHz."""
    return _to_pcm(data) if data[:4] == b"RIFF" else data

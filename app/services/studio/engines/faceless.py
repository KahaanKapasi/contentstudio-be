"""V2: stock-footage shorts, following MoneyPrinterTurbo's workflow (MIT, creator permission) composed with FFmpeg:
script -> search terms -> Pexels clips -> voice -> trim/crop each clip -> concat to narration length -> captions + bgm."""

import math
import re

from app.services import gemini_client
from app.services.studio.engines import _shared
from app.services.studio.kit import ffmpeg, stock
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec, Stage, StageError

MAX_CLIPS = 30
TAIL_S = 0.4  # picture continues briefly after the last word

FIELDS = [
    FieldSpec("subject", "Video subject", "text", required=True, help="e.g. 'Why Real Madrid keeps winning the Champions League'."),
    FieldSpec("script", "Script (optional)", "textarea", help="Leave empty and the AI writes it from the subject."),
    FieldSpec("language", "Language", "select", default="en", options=[{"value": c, "label": n} for c, n in _shared.LANGUAGES]),
    _shared.aspect_field(),
    FieldSpec("paragraphs", "Script paragraphs", "number", default=1, min=1, max=5, help="Only used when the AI writes the script."),
    FieldSpec("clip_duration", "Max seconds per clip", "number", default=5, min=2, max=10),
    *_shared.tts_fields(),
    FieldSpec("bgm", "Background music (optional)", "audio", help="Mixed under the voice at 20% and faded out over the last 3 s."),
    *_shared.caption_fields(default_on=True),
]


class FacelessEngine(Engine):
    id = "faceless"
    label = "Stock-footage shorts"
    description = "Script, voice and captions over free stock footage (Pexels), composed automatically. Based on MoneyPrinterTurbo."
    recipes = [
        RecipeSpec("faceless", "Faceless short", "A narrated vertical short built from stock footage.", FIELDS, paid=False, keys=("GEMINI_API_KEY", "PEXELS_API_KEY"))
    ]

    def title(self, recipe, params):
        return params["subject"].strip()[:60]

    def stages(self, recipe, params):
        stages = []
        if not params.get("script"):
            stages.append(Stage("Writing script", _write_script, "plan"))
        return stages + [
            Stage("Choosing search terms", _search_terms, "plan"),
            Stage("Recording voice", _record_voice, "plan", weight=2),
            Stage("Finding footage", _find_footage, "render"),
            Stage("Downloading footage", _download, "render", weight=3),
            Stage("Preparing clips", _prepare, "render", weight=3),
            Stage("Assembling video", _assemble, "render", weight=2),
            Stage("Adding voice and captions", _finish, "render", weight=3),
        ]


# --- plan phase ---


def clean_script(text: str) -> str:
    """Strip markdown, heading lines and bracketed stage directions the model sometimes adds."""
    text = re.sub(r"^\s*#+.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[\[\(][^\]\)]*[\]\)]", "", text)
    text = re.sub(r"^\s*([-*]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"^\s*(voice ?over|narrator)\s*:\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _write_script(ctx) -> None:
    p = ctx.params
    language = _shared.LANGUAGE_NAMES.get(p["language"], "English")
    text = gemini_client.generate_text(
        f"""# Role: Video Script Generator
## Goal
Write the voice-over script for a short stock-footage video about the subject below.
## Constraints
1. Return exactly {p['paragraphs']} paragraph(s) of plain text.
2. Get straight to the point. No greeting, no "welcome to this video".
3. No markdown, no title, no lists, no emoji, no stage directions, no "narrator:" labels.
4. Do not mention this prompt, the paragraph count or the script itself.
5. Keep it factual: do not invent statistics or quotes.
6. Write in {language}.
## Subject
{p['subject']}
"""
    )
    script = clean_script(text)
    if len(script.split()) < 8:
        raise StageError("Gemini returned too little script text. Retry, or paste your own script.")
    ctx.data["script"] = script


def _search_terms(ctx) -> None:
    p = ctx.params
    script = ctx.data.get("script") or p.get("script", "")
    data = gemini_client.generate_json(
        f"""Generate 5 search terms for stock videos (Pexels), depending on the video subject and script.
Return ONLY a JSON array of 5 strings. Each term is 1-3 English words, concrete and filmable, and always includes the main subject.
Order them from most to least relevant. Do not include people's names unless essential.
Subject: {p['subject']}
Script: {script[:1500]}"""
    )
    items = data.get("terms") if isinstance(data, dict) else data
    terms: list[str] = []
    for t in items if isinstance(items, list) else []:
        t = re.sub(r"\s+", " ", str(t)).strip().lower()
        if t and t not in terms:
            terms.append(t)
    if not terms:
        terms = [p["subject"].strip().lower()[:40]]
    ctx.data["terms"] = terms[:8]


def _record_voice(ctx) -> None:
    p = ctx.params
    script = ctx.data.get("script") or p["script"]
    ctx.data["script"] = script
    result = _shared.record_voice(ctx, script, language=p["language"])
    total = result.duration + TAIL_S
    ctx.data["total"] = total
    per = p["clip_duration"]
    ctx.data["clips_needed"] = min(math.ceil(total / per), MAX_CLIPS * 2)
    ctx.plan.update(
        summary=f"{total:.0f} s {p['aspect']} short about '{p['subject'].strip()[:80]}' from stock clips, search terms: {', '.join(ctx.data['terms'])}.",
        script=script,
        scenes=[{"index": i + 1, "text": s["text"], "visual": ", ".join(ctx.data["terms"][:2]), "duration_s": round(s["end"] - s["start"], 1)} for i, s in enumerate(result.sentences)],
        notes="Footage is picked automatically from Pexels search results for the terms above.",
    )


# --- render phase ---


def _find_footage(ctx) -> None:
    p = ctx.params
    clips = stock.find_clips(ctx.data["terms"], p["aspect"], p["clip_duration"])
    if not clips:
        raise StageError("No stock footage matched these search terms. Try a broader subject or edit the script.")
    ctx.data["found"] = [c.__dict__ for c in clips]


def _download(ctx) -> None:
    clips = [stock.StockClip(**c) for c in ctx.data["found"]]
    want = min(ctx.data["clips_needed"], len(clips), MAX_CLIPS)
    paths: list[str] = []
    for clip in clips:
        if len(paths) >= want:
            break
        dest = ctx.path("clips", f"raw_{len(paths)}.mp4")
        try:
            stock.download(clip, dest)
            ffmpeg.probe(dest)  # reject files ffmpeg cannot read
        except (stock.StockError, ffmpeg.FFmpegError):
            dest.unlink(missing_ok=True)
            continue
        paths.append(str(dest.relative_to(ctx.dir)))
        ctx.progress(len(paths) / want)
    if not paths:
        raise StageError("Could not download any stock footage. Please retry.")
    ctx.data["raw"] = paths


def _prepare(ctx) -> None:
    p = ctx.params
    w, h = ffmpeg.target_size(p["aspect"])
    per = p["clip_duration"]
    out = []
    for i, rel in enumerate(ctx.data["raw"]):
        src = ctx.dir / rel
        length = min(per, ffmpeg.duration(src))
        dest = ctx.path("clips", f"norm_{i}.mp4")
        ffmpeg.normalize_clip(src, dest, w, h, length, loop=False, start=0.0)
        src.unlink(missing_ok=True)  # raw footage is big; keep only the normalised cut
        out.append((str(dest.relative_to(ctx.dir)), length))
        ctx.progress((i + 1) / len(ctx.data["raw"]))
    ctx.data["norm"] = out


def _assemble(ctx) -> None:
    """Repeat the prepared clips in order until they cover the narration, then concat (stream copy)."""
    total = ctx.data["total"]
    clips, covered, i = [], 0.0, 0
    norm = ctx.data["norm"]
    while covered < total:
        rel, length = norm[i % len(norm)]
        clips.append(ctx.dir / rel)
        covered += length
        i += 1
        if i > 400:
            raise StageError("Not enough footage to cover the narration.")
    ffmpeg.concat(clips, ctx.path("assembled.mp4"))


def _finish(ctx) -> None:
    p = ctx.params
    bgm_files = ctx.inputs.get("bgm") or []
    _shared.compose_final(
        ctx, ctx.path("assembled.mp4"), seconds=ctx.data["total"], audio=ctx.asset("narration"), sentences=ctx.data["tts"]["sentences"],
        captions_on=bool(p.get("subtitles")), style=p.get("caption_style", "bold-pop"), position=p.get("subtitle_position", "bottom"),
        bgm=bgm_files[0] if bgm_files else None,
    )
    for f in ctx.dir.glob("clips/*.mp4"):  # keep the project folder small once the final video exists
        f.unlink(missing_ok=True)


ENGINE = FacelessEngine()

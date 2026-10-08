"""Recipe `singing-story`: Gemini writes a short rhymed story (one line per picture) and one style prompt; each line
gets a Gemini illustration in that style, animated with Ken Burns moves, timed to your song or to a rhythmic
spoken-word narration, with karaoke captions."""

import math
from pathlib import Path

from app.services.studio.engines import _shared
from app.services.studio.engines import _skill_common as common
from app.services.studio.kit import ffmpeg, images, tts
from app.services.studio.registry import FieldSpec, RecipeSpec, Stage, StageError

MAX_IMAGES = 12  # cap on paid image generations; longer stories reuse pictures across neighbouring lines
MAX_SONG_S = 180.0
MIN_SLOT_S = 2.0
BEAT_S = 0.6  # spoken-word lines snap to a 2-beat pulse (100 bpm) so the narration feels rhythmic
EFFECTS = ("in", "pan_right", "out", "pan_left")

STYLE_PACKS = {
    "storybook": "warm hand-painted children's storybook illustration, soft gouache texture, gentle rounded shapes, cosy colours",
    "anime": "cinematic anime key art, soft cel shading, painterly backgrounds, expressive characters, vivid palette",
    "watercolor": "loose watercolor illustration, wet-on-wet washes, visible paper grain, delicate ink linework",
    "pixar-3d": "stylised 3D animated film still, soft global illumination, appealing rounded characters, shallow depth of field",
    "comic": "bold comic-book panel art, thick ink outlines, flat saturated colours, halftone shading",
}
STYLE_OPTIONS = [("storybook", "Storybook"), ("anime", "Anime"), ("watercolor", "Watercolor"), ("pixar-3d", "3D animated"), ("comic", "Comic book")]

SPEC = RecipeSpec(
    "singing-story", "Singing story",
    "A story told line by line over your song (or a rhythmic spoken narration), with consistent AI illustrations.",
    [
        FieldSpec("idea", "Story idea", "textarea", required=True),
        FieldSpec("style", "Illustration style", "select", default="storybook", options=[{"value": v, "label": l} for v, l in STYLE_OPTIONS]),
        FieldSpec("song", "Song (optional)", "audio", help="Without a song, a rhythmic spoken-word narration is used."),
        FieldSpec("lyrics_start", "Vocals start at (seconds)", "number", default=0, min=0, max=30,
                  help="Only with a song: skip an instrumental intro so the lines line up with the singing."),
        FieldSpec("lines", "Story lines (one picture each, capped at 12 pictures)", "number", default=8, min=6, max=16),
        *_shared.tts_fields(),
        *common.caption_fields("karaoke"),
        _shared.aspect_field(),
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)


def title(params: dict) -> str:
    return params["idea"].strip().splitlines()[0][:60]


def stages(params: dict) -> list[Stage]:
    return [
        Stage("Writing the story", _write_story, "plan", weight=2),
        Stage("Timing the lines", _timing, "plan", weight=2),
        Stage("Illustrating the story", _illustrate, "render", weight=8),
        Stage("Animating the pictures", _animate, "render", weight=4),
        Stage("Finishing the video", _finish, "render", weight=2),
    ]


def image_count(n_lines: int) -> int:
    return min(n_lines, MAX_IMAGES)


def image_index(line: int, n_lines: int) -> int:
    """Which picture a line uses (identity unless the story is longer than MAX_IMAGES)."""
    return min(line * image_count(n_lines) // n_lines, image_count(n_lines) - 1)


def estimate_cost(params: dict, plan: dict) -> float | None:
    return round(plan.get("image_count", image_count(int(params.get("lines", 8)))) * common.IMAGE_COST_USD, 3)


# --- plan phase ---


def _write_story(ctx) -> None:
    p = ctx.params
    n = int(p["lines"])
    style = STYLE_PACKS.get(p.get("style", "storybook"), STYLE_PACKS["storybook"])
    data = common.ask_json(
        f"""You write short sung stories for illustrated music videos.
Story idea: {p['idea']}

Write a story told in exactly {n} lyric lines: a clear beginning, a turn in the middle and a satisfying ending. Each line is 5-11 words,
singable, simple words, ideally rhyming in couplets. No stage directions, no emoji, no markdown. Keep it family friendly and do not
present invented things as real facts about real people.
For each line also give a "visual": one sentence describing the single picture that illustrates it (who is there, where, what happens).
Keep the same recurring characters and setting across the pictures.
Also give "style_prompt": two sentences describing the characters' fixed appearance (hair, clothes, colours, species) and the setting's look,
so every picture can be drawn the same way. Base art style: {style}.
Return JSON: {{"title": "<max 6 words>", "style_prompt": "<text>", "lines": [{{"text": "<lyric line>", "visual": "<picture>"}}]}}"""
    )
    raw = data.get("lines") if isinstance(data.get("lines"), list) else data.get("items", [])
    lines = []
    for item in raw:
        text = common.clean_line(item.get("text") if isinstance(item, dict) else item)
        visual = common.clip_text(item.get("visual") if isinstance(item, dict) else "", 300) or text
        if text:
            lines.append({"text": text, "visual": visual})
    if len(lines) < 4:
        raise StageError("Gemini returned too few story lines. Retry, or reword the story idea.")
    lines = lines[:n]
    character = common.clip_text(data.get("style_prompt"), 600)
    ctx.data["story"] = {
        "title": common.clip_text(data.get("title"), 80) or title(p),
        "style_prompt": f"{style}. {character}".strip(),
        "lines": lines,
    }
    ctx.plan.update(
        summary=f"A {len(lines)}-line illustrated story: {ctx.data['story']['title']} ({p['aspect']}, {p['style']} style).",
        script="\n".join(line["text"] for line in lines),
        scenes=[{"index": i + 1, "text": line["text"], "visual": line["visual"], "duration_s": 0} for i, line in enumerate(lines)],
        image_count=image_count(len(lines)),
        notes=f"{image_count(len(lines))} Gemini illustrations (about ${common.IMAGE_COST_USD:.2f} each). "
        + ("Lines are spread across your song by length." if ctx.inputs.get("song") else "No song: a spoken-word narration paced on a steady beat is used."),
    )


def _timing(ctx) -> None:
    lines = ctx.data["story"]["lines"]
    song = (ctx.inputs.get("song") or [None])[0]
    sentences = _song_timing(ctx, lines, song) if song else _spoken_timing(ctx, lines)
    ctx.data["sentences"] = sentences
    ctx.data["slots"] = [[s["slot_start"], s["slot_end"]] for s in sentences]
    ctx.data["total"] = sentences[-1]["slot_end"]
    for scene, sent in zip(ctx.plan["scenes"], sentences):
        scene["duration_s"] = round(sent["slot_end"] - sent["slot_start"], 1)
    ctx.plan["summary"] += f" About {ctx.data['total']:.0f} s."


def _song_timing(ctx, lines: list[dict], song: Path) -> list[dict]:
    total = min(ffmpeg.duration(song), MAX_SONG_S)
    begin = min(float(ctx.params.get("lyrics_start") or 0), total * 0.5)
    weights = [max(len(line["text"].split()), 3) for line in lines]
    span = (total - begin) / sum(weights)
    out, cursor = [], begin
    for i, (line, wt) in enumerate(zip(lines, weights)):
        end = total if i == len(lines) - 1 else cursor + span * wt
        sung_end = max(end - 0.25, cursor + 0.3)
        sent = common.sentence(line["text"], cursor, sung_end)
        sent.update(slot_start=0.0 if i == 0 else cursor, slot_end=end)
        out.append(sent)
        cursor = end
    ctx.data["audio"] = "song"
    return out


def _spoken_timing(ctx, lines: list[dict]) -> list[dict]:
    """One TTS clip per line, each placed on a 2-beat grid so the narration has a steady pulse."""
    p = ctx.params
    parts, out, cursor = [], [], 0.0
    pulse = 2 * BEAT_S
    for i, line in enumerate(lines):
        wav = ctx.path("voice", f"line_{i}.wav")
        result = tts.synthesize([line["text"]], wav, provider=p.get("tts_provider", "edge"), voice=p.get("voice"), language="en")
        slot = max(MIN_SLOT_S, math.ceil((result.duration + 0.25) / pulse) * pulse)
        spoken = result.sentences[0]
        sent = common.sentence(line["text"], cursor + spoken["start"], cursor + min(spoken["end"], slot), common.shift_words(spoken["words"], cursor, cursor + slot))
        sent.update(slot_start=cursor, slot_end=cursor + slot)
        out.append(sent)
        parts.append((wav, cursor, slot))
        cursor += slot
        ctx.progress((i + 1) / len(lines))
    narration = common.assemble_wav(parts, ctx.path("narration.wav"))
    for wav, _, _ in parts:
        wav.unlink(missing_ok=True)
    ctx.data["audio"] = "narration"
    ctx.register("narration", narration, label="Narration", kind="audio", preview=True)
    return out


# --- render phase ---


def _illustrate(ctx) -> None:
    p = ctx.params
    story = ctx.data["story"]
    n_lines = len(story["lines"])
    count = image_count(n_lines)
    # the visual for picture k is the visual of the first line that uses it
    visuals = {}
    for i, line in enumerate(story["lines"]):
        visuals.setdefault(image_index(i, n_lines), line["visual"])
    paths: list[Path] = []
    for k in range(count):
        dest = ctx.path("images", f"{k}.png")
        if not dest.is_file():
            prompt = (
                f"{story['style_prompt']}\n\nIllustrate this moment of the story: {visuals[k]}\n"
                "Full-bleed artwork, no text, no letters, no captions, no watermark."
            )
            refs = [paths[0]] if paths else None
            if refs:
                prompt += "\nMatch the art style, colours and characters of the reference image exactly."
            try:
                images.generate_image(prompt, dest, aspect=p["aspect"], references=refs)
            except images.ImageGenError:
                if k == 0:
                    raise
                try:  # one more try, then reuse the previous picture rather than failing the whole video
                    ctx.remaining()
                    images.generate_image(prompt, dest, aspect=p["aspect"], references=refs)
                except images.ImageGenError:
                    dest = paths[-1]
        paths.append(dest)
        ctx.progress((k + 1) / count)
    ctx.data["images"] = [str(path.relative_to(ctx.dir)) for path in paths]


def _animate(ctx) -> None:
    w, h = ffmpeg.target_size(ctx.params["aspect"])
    n_lines = len(ctx.data["slots"])
    clips = []
    for i, (start, end) in enumerate(ctx.data["slots"]):
        pic = ctx.dir / ctx.data["images"][image_index(i, n_lines)]
        clip = ctx.path("clips", f"{i}.mp4")
        images.ken_burns(pic, clip, end - start, w, h, effect=EFFECTS[i % len(EFFECTS)])
        clips.append(clip)
        ctx.progress((i + 1) / n_lines)
    ffmpeg.concat(clips, ctx.path("story.mp4"))
    for clip in clips:
        clip.unlink(missing_ok=True)


def _finish(ctx) -> None:
    p = ctx.params
    audio = (ctx.inputs.get("song") or [None])[0] if ctx.data["audio"] == "song" else ctx.asset("narration")
    _shared.compose_final(
        ctx, ctx.path("story.mp4"), seconds=ctx.data["total"], audio=audio, sentences=ctx.data["sentences"],
        captions_on=bool(p.get("subtitles")), style=p.get("caption_style", "karaoke"), position=p.get("subtitle_position", "bottom"),
    )

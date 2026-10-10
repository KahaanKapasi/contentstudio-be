"""The scene-film core shared by the toons, shorts/film and explainer3d engines.

plan phase   research (optional, grounded) -> script as JSON (title, characters, shots, end card) -> character /
             style reference sheets (Gemini image from the uploaded photos, shown as previews) -> per-shot voices
             (one distinct voice per character) -> shot list with final durations + cost estimate
render phase one still per shot (references = the sheets) -> `cheap`: Ken-Burns / push-in / shake on the still,
             `ai`: image-to-video per shot -> stitch (hard cuts or short cross-fades) -> voices or native audio,
             bold-pop captions, optional burned-in "AI parody" tag.

All three engines are paid: the runner stops at awaiting_approval after the plan phase with the plan, the
sheets and the estimate on screen. Each recipe is a `Flavour` (genre, style pack, script rules, stitching).
"""

import math
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

from app.services import gemini_client
from app.services.studio.engines import _scenerender as sr
from app.services.studio.engines import _shared
from app.services.studio.kit import clips as kitclips
from app.services.studio.kit import ffmpeg, images, tts
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec, Stage, StageError, ValidationError
from app.services.costs import prices
from app.services.video_providers import catalog

# --- shared field specs ---

IMAGE_COST_USD = prices.usd("gemini.image.out")  # per Gemini image (character sheets + one still per shot), 1K
MAX_CHARACTERS = 6
CARD_S = 2.2
XFADE_S = 0.25


def motion_quality_field(default: str = "cheap") -> FieldSpec:
    return FieldSpec(
        "motion_quality", "Motion quality", "select", default=default,
        options=[{"value": "cheap", "label": "Cheap: stills with camera moves (Gemini image cost only)"}, {"value": "ai", "label": "AI: image-to-video per shot (paid per second)"}],
        help="AI motion pays per second of video on top of the images; the estimate is shown before you approve.",
    )


MOTION_QUALITY = motion_quality_field()
RESEARCH = FieldSpec("research", "Research the topic on the web first", "toggle", default=False)
PARODY_LABEL = FieldSpec("parody_label", "Burn in an 'AI parody' label", "toggle", default=True)
CLIP_PROVIDER = FieldSpec(
    "clip_provider", "Clip provider (AI motion)", "select", default="veo",
    options=[{"value": "veo", "label": "Google Veo (uses the still as the first frame)"}, {"value": "muapi", "label": "Muapi (Kling 3.0 image-to-video from the shot still)"}],
)
SUBTITLES = FieldSpec("subtitles", "Burn-in captions", "toggle", default=True)
LANGUAGE = FieldSpec("language", "Language", "select", default="en", options=[{"value": c, "label": n} for c, n in _shared.LANGUAGES])
ASPECT = _shared.aspect_field()


def characters_fields() -> list[FieldSpec]:
    return [
        FieldSpec("characters", "Characters (one per line: Name: description)", "textarea"),
        FieldSpec("character_images", "Character photos (optional)", "images", help="Used as reference so characters look consistent in every shot. Order or file names matching the characters help."),
    ]


def style_field(options: list[tuple[str, str]], default: str) -> FieldSpec:
    return FieldSpec("visual_style", "Visual style", "select", default=default, options=[{"value": v, "label": l} for v, l in options])


# --- recipe flavours ---

SATIRE_RULES = (
    "Comedy and satire only. Exaggerated and absurd; nothing may be presented as a factual claim about a real person or club "
    "(no invented quotes, scandals or accusations). No sexual content, no slurs or hateful stereotypes, no glorification of real-world "
    "violence, no harassment of private individuals. Make the audience laugh at the situation, not at a person's identity."
)


@dataclass(frozen=True)
class Flavour:
    key: str  # engine/recipe, for messages
    kind: str  # dialogue | narration | lyrics
    brief: str  # what the model is writing
    rules: str  # script rules
    style: Callable[[dict], str]  # visual style pack from the params
    count_param: str  # field holding the number of shots
    shot_len: tuple[float, float]  # planned seconds per shot
    stitch: str  # cut | xfade
    caption_style: str
    end_card: bool
    topic_param: str
    min_shot: float = 2.0
    card_bg: tuple[int, int, int] = (255, 212, 0)


def _fmt_voice_hint(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:60]


# --- plan: prompt + parsing/repair ---


class PlanError(ValueError):
    """The model's JSON could not be turned into a usable shot list; the message goes back to the model."""


def parse_characters(text: str | None) -> list[dict]:
    """'Name: description' (or 'Name - description') per line."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip(" \t-*•")
        if not line:
            continue
        bits = re.split(r"\s*:\s*|\s+[-\u2013\u2014]\s+", line, maxsplit=1)
        name, desc = bits[0], (bits[1] if len(bits) > 1 else "")
        name = name.strip()[:40]
        if name and not any(c["name"].lower() == name.lower() for c in out):
            out.append({"name": name, "description": desc.strip()[:300], "voice": ""})
    return out[:MAX_CHARACTERS]


def build_prompt(fl: Flavour, params: dict, research: str | None) -> str:
    n = int(params[fl.count_param])
    lo, hi = fl.shot_len
    language = _shared.LANGUAGE_NAMES.get(params.get("language", "en"), "English")
    topic = params[fl.topic_param]
    user_chars = parse_characters(params.get("characters"))
    narration = fl.kind == "narration"
    shot_shape = (
        '{"index": 1, "duration_s": 3, "visual": "...", "camera": "...", "narration": "...", "sfx_note": "..."}' if narration
        else '{"index": 1, "duration_s": 4, "visual": "...", "camera": "...", "dialogue": [{"speaker": "NAME", "text": "..."}], "sfx_note": "..."}'
    )
    parts = [
        f"You are a short-form video writer and director. Write {fl.brief}",
        f"## Topic\n{topic}",
    ]
    if params.get("target"):
        parts.append(f"## Target of the parody\n{params['target']}")
    if research:
        parts.append(f"## Background research (use for accuracy of details; do not copy wording)\n{research[:5000]}")
    if user_chars:
        parts.append("## Characters the user supplied (use exactly these names and looks)\n" + "\n".join(f"- {c['name']}: {c['description']}" for c in user_chars))
    parts.append(f"## Style of the visuals\n{fl.style(params)}")
    parts.append(f"## Rules\n{fl.rules}\n- Exactly {n} shots, each {lo:g}-{hi:g} seconds long.\n- Write all spoken text in {language}.\n- 'visual' describes ONE still frame (who is where, doing what, expression, props, setting); 'camera' is a short move such as push in, pull back, pan left, handheld shake.\n- Speech must fit the shot: about 2.5 words per second.\n- No markdown, no emoji, no stage directions inside spoken text.")
    schema = (
        "## Output\nReturn ONLY JSON of this shape:\n"
        '{"title": "...", "logline": "one sentence", "characters": ['
        + ('' if narration else '{"name": "NAME", "description": "age, build, hair, outfit, signature props, in 1-2 sentences", "voice": "e.g. deep gruff male | bright young female"}')
        + f'], "shots": [{shot_shape}]'
        + (', "end_card": "short punchline shown on the final card"' if fl.end_card else "")
        + "}"
    )
    parts.append(schema)
    return "\n\n".join(parts)


_STAGE_DIRECTION = re.compile(r"[\[\(][^\]\)]*[\]\)]")


def clean_line(text) -> str:
    text = _STAGE_DIRECTION.sub("", str(text or ""))
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"\s+", " ", text).strip().strip('"“”')
    return text[:240]


_SPEAKER_KEYS = ("speaker", "character", "name", "who", "singer")
_TEXT_KEYS = ("text", "line", "lyric", "dialogue", "says", "narration")


def _pick(d: dict, keys) -> str:
    for k in keys:
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k]
    return ""


def _canon(name: str, chars: list[dict], allow_new: bool) -> str | None:
    """Map a speaker label to a known character (case-insensitive, prefix/contains), else add it when allowed."""
    low = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    if not low:
        return None
    for c in chars:
        cl = c["name"].lower()
        if low == cl or low in cl.split() or cl in low.split():
            return c["name"]
    for c in chars:
        if low in c["name"].lower() or c["name"].lower() in low:
            return c["name"]
    if allow_new and len(chars) < MAX_CHARACTERS:
        chars.append({"name": name.strip()[:40], "description": "", "voice": ""})
        return chars[-1]["name"]
    return None


def _dialogue(shot: dict, fl: Flavour, chars: list[dict]) -> list[dict]:
    raw = shot.get("dialogue")
    if raw in (None, "", []):
        raw = shot.get("narration") or shot.get("lyrics") or shot.get("text") or shot.get("line")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    lines: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            speaker, text = _pick(item, _SPEAKER_KEYS), _pick(item, _TEXT_KEYS)
        else:
            speaker, text = "", str(item)
            m = re.match(r"^\s*([A-Za-z][\w .'’-]{0,30}?)\s*:\s*(.+)$", text)
            if m:
                speaker, text = m.group(1), m.group(2)
        text = clean_line(text)
        if not text:
            continue
        if fl.kind == "narration":
            lines.append({"speaker": "Narrator", "text": text})
            continue
        name = _canon(speaker, chars, allow_new=fl.kind == "dialogue") or (chars[0]["name"] if chars else "Narrator")
        lines.append({"speaker": name, "text": text})
    if fl.kind == "narration" and len(lines) > 1:
        lines = [{"speaker": "Narrator", "text": " ".join(l["text"] for l in lines)}]
    return lines


def parse_plan(raw, fl: Flavour, params: dict) -> dict:
    """Validate and repair the model's JSON. Raises PlanError (retried with the message) if it is not salvageable."""
    if isinstance(raw, list):
        raw = {"shots": raw}
    if not isinstance(raw, dict):
        raise PlanError("the answer was not a JSON object")
    for wrapper in ("plan", "script", "film", "video"):  # unwrap {"plan": {...}}
        if "shots" not in raw and isinstance(raw.get(wrapper), dict):
            raw = raw[wrapper]
    shots_raw = raw.get("shots") or raw.get("scenes")
    if not isinstance(shots_raw, list) or not shots_raw:
        raise PlanError("it has no 'shots' list")

    chars: list[dict] = []
    if fl.kind != "narration":
        for c in raw.get("characters") if isinstance(raw.get("characters"), list) else []:
            if isinstance(c, str):
                c = {"name": c}
            if isinstance(c, dict) and str(c.get("name") or "").strip():
                name = str(c["name"]).strip()[:40]
                if not any(x["name"].lower() == name.lower() for x in chars):
                    chars.append({"name": name, "description": clean_line(c.get("description") or c.get("look") or "")[:300], "voice": _fmt_voice_hint(c.get("voice"))})
        for u in parse_characters(params.get("characters")):  # the user's characters always exist, with their wording
            hit = next((c for c in chars if c["name"].lower() == u["name"].lower()), None)
            if hit:
                hit["description"] = u["description"] or hit["description"]
            else:
                chars.append(u)
        chars = chars[:MAX_CHARACTERS]

    lo, hi = fl.shot_len
    shots: list[dict] = []
    for s in shots_raw:
        if not isinstance(s, dict):
            continue
        visual = clean_line(_pick(s, ("visual", "description", "scene", "image")))[:500]
        if len(visual) < 5:
            continue
        try:
            dur = float(s.get("duration_s") or s.get("duration") or (lo + hi) / 2)
        except (TypeError, ValueError):
            dur = (lo + hi) / 2
        shots.append({
            "index": len(shots) + 1, "duration_s": round(min(max(dur, lo), hi), 1), "visual": visual,
            "camera": clean_line(s.get("camera"))[:80], "dialogue": _dialogue(s, fl, chars),
            "sfx_note": clean_line(s.get("sfx_note") or s.get("sfx"))[:120],
        })
    want = int(params[fl.count_param])
    if len(shots) < max(2, math.ceil(want * 0.7)):
        raise PlanError(f"it has {len(shots)} usable shots but exactly {want} were required")
    shots = shots[:want]
    if fl.kind != "narration" and not chars:
        if fl.kind == "lyrics":
            chars = [{"name": "Singer", "description": "a comedic caricature of the song's target", "voice": ""}]
            for s in shots:
                for l in s["dialogue"]:
                    l["speaker"] = "Singer"
        else:
            raise PlanError("it has no characters")
    if fl.kind == "dialogue" and not any(s["dialogue"] for s in shots):
        raise PlanError("no shot has any dialogue")
    if fl.kind != "dialogue" and not any(s["dialogue"] for s in shots):
        raise PlanError("no shot has any spoken text")
    title = clean_line(raw.get("title"))[:80] or str(params[fl.topic_param]).strip()[:60]
    logline = clean_line(raw.get("logline"))[:240]
    card = None
    if fl.end_card:
        card = clean_line(raw.get("end_card") if isinstance(raw.get("end_card"), str) else (raw.get("end_card") or {}).get("text") if isinstance(raw.get("end_card"), dict) else "")[:90] or logline[:90] or title
    return {"title": title, "logline": logline, "characters": chars, "shots": shots, "end_card": card}


# --- voices ---

EDGE_MALE = ("en-US-GuyNeural", "en-GB-RyanNeural", "en-AU-WilliamNeural", "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-BrianNeural", "en-US-RogerNeural", "en-GB-ThomasNeural")
EDGE_FEMALE = ("en-US-AriaNeural", "en-US-JennyNeural", "en-GB-SoniaNeural", "en-US-MichelleNeural", "en-US-AvaNeural", "en-AU-NatashaNeural", "en-GB-LibbyNeural")
GEMINI_MALE = ("Puck", "Charon", "Fenrir", "Orus", "Iapetus", "Algenib", "Enceladus")
GEMINI_FEMALE = ("Kore", "Aoede", "Leda", "Zephyr", "Autonoe", "Callirrhoe", "Despina")
_FEMALE = re.compile(r"\b(female|woman|girl|she|her|lady|feminine|mother|queen)\b", re.I)


def assign_voices(speakers: list[dict], provider: str, language: str, user_voice: str | None) -> dict[str, str]:
    """A distinct voice per speaker, picked by the model's gender hint. Non-English edge voices have no
    per-gender pool here, so every speaker gets the language default (empty string)."""
    out: dict[str, str] = {}
    used: set[str] = set()
    english = language.split("-")[0] == "en"
    male, female = (GEMINI_MALE, GEMINI_FEMALE) if provider == "gemini" else (EDGE_MALE, EDGE_FEMALE)
    pools = {"m": list(male), "f": list(female)}
    for i, sp in enumerate(speakers):
        if i == 0 and user_voice:
            out[sp["name"]] = user_voice
            used.add(user_voice)
            continue
        if provider != "gemini" and not english:
            out[sp["name"]] = ""
            continue
        female_hint = bool(_FEMALE.search(f"{sp.get('voice', '')} {sp.get('description', '')}"))
        key = "f" if female_hint else "m"
        pool = [v for v in pools[key] if v not in used] or [v for v in pools["f" if key == "m" else "m"] if v not in used] or pools[key]
        out[sp["name"]] = pool[0]
        used.add(pool[0])
    return out


# --- reference sheets ---


def _slug(text: str, i: int) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "char")[:24] + f"-{i}"


def assign_photos(chars: list[dict], photos: list[Path]) -> dict[str, list[Path]]:
    """Photos whose file name contains a character's name go to that character; the rest are handed out in order."""
    out: dict[str, list[Path]] = {c["name"]: [] for c in chars}
    rest = []
    for p in photos:
        stem = re.sub(r"[^a-z0-9]", "", p.stem.lower())
        hit = next((c["name"] for c in chars if (n := re.sub(r"[^a-z0-9]", "", c["name"].lower())) and n in stem), None)
        (out[hit].append(p) if hit else rest.append(p))
    free = [c["name"] for c in chars if not out[c["name"]]]
    for name, p in zip(free, rest):
        out[name].append(p)
    return out


# --- ai clip choices ---

AI_DEFAULTS = {"veo": ("veo-3.1-lite-generate-preview", "720p"), "muapi": (catalog.DEFAULT_I2V_MODEL["muapi"], "720p")}  # image-to-video: the shot still is the start frame


def is_ai(params: dict) -> bool:
    return params.get("motion_quality", "cheap") == "ai"


def is_native(params: dict) -> bool:
    return is_ai(params) and params.get("dialogue") == "native"


def ai_target(params: dict) -> tuple[str, str, str]:
    provider = params.get("clip_provider", "veo")
    return (provider, *AI_DEFAULTS[provider])


def clip_len(params: dict, seconds: float) -> int:
    """Smallest clip length the model offers that covers `seconds` (else the longest)."""
    provider, model, _ = ai_target(params)
    durs = sorted(catalog.get_model(provider, model).durations)
    return next((d for d in durs if d >= seconds - 0.05), durs[-1])


def estimate(params: dict, durations: list[float], n_images: int) -> dict:
    usd = n_images * IMAGE_COST_USD
    clip_s, clip_usd, priced = 0, 0.0, True
    if is_ai(params):
        provider, model, res = ai_target(params)
        for d in durations:
            n = clip_len(params, d)
            clip_s += n
            c = catalog.estimate_cost(provider, model, res, n)
            if c is None:
                priced = False
            else:
                clip_usd += c
    return {"images": n_images, "image_usd": round(n_images * IMAGE_COST_USD, 3), "clip_seconds": clip_s, "clip_usd": round(clip_usd, 3) if priced else None,
            "usd": round(usd + clip_usd, 3)}


# --- stages ---


def _fmt_dialogue(lines: list[dict], kind: str) -> str:
    return " / ".join(l["text"] if kind == "narration" else f"{l['speaker']}: {l['text']}" for l in lines)


def _research(ctx, fl: Flavour) -> None:
    p = ctx.params
    topic = p[fl.topic_param] + (f" ({p['target']})" if p.get("target") else "")
    text, sources = gemini_client.generate_grounded(
        f"Research this topic for a short video script and give 8-12 concise, verifiable bullet points (names, numbers, dates, how it works, "
        f"the most surprising true details). Plain text bullets, no preamble.\nTopic: {topic}"
    )
    if len(text.strip()) < 40:
        raise StageError("The web research came back empty. Retry, or turn research off.")
    ctx.data["research"] = text.strip()[:6000]
    ctx.data["sources"] = sources[:8]


def _write_script(ctx, fl: Flavour) -> None:
    prompt = build_prompt(fl, ctx.params, ctx.data.get("research"))
    error = ""
    for attempt in range(2):
        text = prompt if not error else f"{prompt}\n\nYour previous answer was rejected because {error}. Fix that and return the complete JSON again."
        try:
            film = parse_plan(gemini_client.generate_json(text), fl, ctx.params)
            break
        except (PlanError, ValueError) as exc:  # PlanError, or malformed JSON from the model
            error = str(exc)[:200]
    else:
        raise StageError(f"The script came back unusable ({error}). Retry to have it rewritten.")
    ctx.data["film"] = film
    _write_plan(ctx, fl, [s["duration_s"] for s in film["shots"]])


def _design_sheets(ctx, fl: Flavour) -> None:
    film, p = ctx.data["film"], ctx.params
    style = fl.style(p)
    photos = list(ctx.inputs.get("character_images") or [])
    chars = film["characters"]
    jobs: list[tuple[str, str, str, list[Path]]] = []  # (key, label, prompt, references)
    if chars:
        mapped = assign_photos(chars, photos)
        for i, c in enumerate(chars):
            refs = mapped[c["name"]][:3]
            likeness = "Base the face and likeness on the person in the reference photo(s), drawn as a stylised caricature in this style. " if refs else ""
            jobs.append((_slug(c["name"], i), f"Character: {c['name']}", (
                f"Character reference sheet for {c['name']}: {c['description'] or 'a distinctive comedic character'}. {likeness}"
                f"Show the character full body and as a head close-up on one image, neutral pose, plain light background, no text. Style: {style}"), refs))
    else:
        jobs.append(("style", "Style reference", (
            f"Style reference frame for a video about: {p[fl.topic_param]}. One clean establishing image that shows the look: {style}. No text."), []))
    for n, (key, label, prompt, refs) in enumerate(jobs):
        dest = ctx.path("sheets", f"{key}.png")
        if not dest.is_file():
            images.generate_image(prompt, dest, aspect="1:1", references=refs)
        ctx.register(f"sheet_{key}", dest, label=label, kind="image", preview=True)
        ctx.data.setdefault("sheets", {})[key] = str(dest.relative_to(ctx.dir))
        ctx.progress((n + 1) / len(jobs))
    ctx.data["sheet_keys"] = {c["name"]: _slug(c["name"], i) for i, c in enumerate(chars)} or {"_style": "style"}


def _record_voices(ctx, fl: Flavour) -> None:
    film, p = ctx.data["film"], ctx.params
    provider, language = p.get("tts_provider", "edge"), p.get("language", "en")
    speakers = film["characters"] if fl.kind != "narration" else [{"name": "Narrator", "voice": "calm male", "description": ""}]
    voices = assign_voices(speakers, provider, language, p.get("voice") or None)
    voices.setdefault("Narrator", p.get("voice") or "")
    ctx.data["voices"] = voices
    ctx.data.setdefault("tts", {})
    shots = film["shots"]
    for n, shot in enumerate(shots):
        if shot["dialogue"] and not (str(n) in ctx.data["tts"] and ctx.path("audio", f"shot_{n:02d}.wav").is_file()):
            result = tts.synthesize(
                [l["text"] for l in shot["dialogue"]], ctx.path("audio", f"shot_{n:02d}.wav"), provider=provider, voice=None, language=language,
                voices=[voices.get(l["speaker"], "") for l in shot["dialogue"]],
            )
            ctx.data["tts"][str(n)] = {"duration": result.duration, "sentences": result.sentences}
        ctx.progress((n + 1) / len(shots))


def shot_durations(ctx, fl: Flavour) -> list[float]:
    """Final timeline length per shot: spoken length wins over the planned one; native-audio shots are whole clips."""
    p, shots = ctx.params, ctx.data["film"]["shots"]
    out = []
    for n, shot in enumerate(shots):
        planned = shot["duration_s"]
        if is_native(p):
            d = float(clip_len(p, planned))
        else:
            speech = (ctx.data.get("tts", {}).get(str(n)) or {}).get("duration")
            d = max(speech + 0.25, min(planned, speech + 1.5), fl.min_shot) if speech else max(planned, fl.min_shot)
        out.append(round(d, 1))
    return out


def _prepare(ctx, fl: Flavour) -> None:
    """Last plan stage: settle the durations, build the voice preview, write the plan the user approves and the estimate."""
    durations = shot_durations(ctx, fl)
    ctx.data["durations"] = durations
    if not is_native(ctx.params) and ctx.data.get("tts"):
        wavs = [ctx.path("audio", f"shot_{n:02d}.wav") if str(n) in ctx.data["tts"] else None for n in range(len(durations))]
        sr.build_track(ctx.path("audio", "voices.wav"), wavs, durations)
        ctx.register("voices", ctx.path("audio", "voices.wav"), label="Voices", kind="audio", preview=True)
    _write_plan(ctx, fl, durations, final=True)


def _write_plan(ctx, fl: Flavour, durations: list[float], final: bool = False) -> None:
    film, p = ctx.data["film"], ctx.params
    shots = film["shots"]
    script = [f"{film['title']}", film["logline"], ""]
    if film["characters"]:
        script += ["Characters:"] + [f"- {c['name']}: {c['description']}" for c in film["characters"]] + [""]
    for s in shots:
        script.append(f"[{s['index']}] {s['visual']}")
        script += [l["text"] if fl.kind == "narration" else f"  {l['speaker']}: {l['text']}" for l in s["dialogue"]]
    if film.get("end_card"):
        script += ["", f"END CARD: {film['end_card']}"]
    total = sum(durations) + (CARD_S if film.get("end_card") else 0)
    mode = "AI image-to-video" if is_ai(p) else "stills with camera moves"
    summary = f"{film['title']}: {total:.0f} s {p.get('aspect', '9:16')} video, {len(shots)} shots, {mode}."
    notes = []
    if final:
        n_images = len(shots) + max(len(film["characters"]), 1)
        est = estimate(p, durations, n_images)
        ctx.plan["estimate"] = est
        line = f"Estimated cost about ${est['usd']:.2f}: {est['images']} images at ~${IMAGE_COST_USD:.2f}"
        if is_ai(p):
            provider, model, res = ai_target(p)
            line += f" + {est['clip_seconds']} s of {catalog.get_model(provider, model).label} {res} video" + (f" (${est['clip_usd']:.2f})" if est["clip_usd"] is not None else " (billed in Muapi credits, not included)")
        notes.append(line + ".")
    if is_native(p):
        notes.append("Dialogue comes from the video model's native audio, so lines are as the model speaks them.")
    elif is_ai(p):
        notes.append("Clip audio is dropped; the character voices are added in post.")
    if is_ai(p) and p.get("clip_provider") == "muapi":
        notes.append("Muapi clips are image-to-video: each shot still is uploaded to Muapi as the start frame, so characters keep their look from the reference sheets.")
    if p.get("parody_label"):
        notes.append("A small 'AI PARODY' tag is burned into the corner of the video.")
    if fl.kind == "lyrics":
        notes.append("Lyrics are delivered in a rhythmic spoken style over karaoke captions" + (", mixed over your instrumental." if ctx.inputs.get("instrumental") else "; upload an instrumental to add a track."))
    if ctx.data.get("sources"):
        notes.append("Researched: " + ", ".join(s.get("title") or s["url"] for s in ctx.data["sources"][:4]) + ".")
    ctx.plan.update(
        summary=summary, script="\n".join(script).strip(),
        scenes=[{"index": s["index"], "text": _fmt_dialogue(s["dialogue"], fl.kind) or s.get("sfx_note", ""), "visual": s["visual"], "duration_s": d} for s, d in zip(shots, durations)],
        notes=" ".join(notes) or None,
    )


# --- render ---


def _characters_in_shot(film: dict, shot: dict) -> list[dict]:
    names = {l["speaker"] for l in shot["dialogue"]}
    low = shot["visual"].lower()
    hits = [c for c in film["characters"] if c["name"] in names or c["name"].lower() in low or any(part in low for part in c["name"].lower().split() if len(part) > 3)]
    return hits or film["characters"][:1]


def _shot_refs(ctx, film: dict, shot: dict) -> list[Path]:
    keys = ctx.data.get("sheet_keys", {})
    sheets = ctx.data.get("sheets", {})
    if not film["characters"]:
        names = ["_style"]
    else:
        names = [c["name"] for c in _characters_in_shot(film, shot)][:3]
    return [ctx.dir / sheets[keys[n]] for n in names if n in keys and keys[n] in sheets]


def _still_prompt(ctx, fl: Flavour, film: dict, shot: dict) -> str:
    p = ctx.params
    who = ""
    if film["characters"]:
        who = "\nCharacters in frame (keep each identical to its reference image): " + "; ".join(
            f"{c['name']}: {c['description']}" if c["description"] else c["name"] for c in _characters_in_shot(film, shot))
    cam = f"\nCamera: {shot['camera']}." if shot["camera"] else ""
    return (f"{fl.style(p)}\nScene: {shot['visual']}{cam}{who}\n"
            f"Compose for a {p.get('aspect', '9:16')} frame. No text, captions, subtitles, logos, watermarks or speech bubbles.")


def _visuals(ctx, fl: Flavour) -> None:
    film, p = ctx.data["film"], ctx.params
    shots = film["shots"]
    for n, shot in enumerate(shots):
        dest = ctx.path("stills", f"shot_{n:02d}.png")
        if not dest.is_file():
            images.generate_image(_still_prompt(ctx, fl, film, shot), dest, aspect=p.get("aspect", "9:16"), references=_shot_refs(ctx, film, shot))
        ctx.progress((n + 1) / len(shots))


def _clip_prompt(ctx, fl: Flavour, film: dict, shot: dict) -> str:
    p = ctx.params
    parts = [f"{fl.style(p)}. {shot['visual']}."]
    if shot["camera"]:
        parts.append(f"Camera: {shot['camera']}.")
    if is_native(p):
        voices = {c["name"]: c.get("voice") for c in film["characters"]}
        for l in shot["dialogue"]:
            hint = f" ({voices[l['speaker']]} voice)" if voices.get(l["speaker"]) else ""
            parts.append(f'{l["speaker"]}{hint} says: "{l["text"]}"')
        if shot["sfx_note"]:
            parts.append(f"Sound: {shot['sfx_note']}.")
    else:
        parts.append("No speech, no dialogue, no on-screen text or subtitles.")
    return " ".join(parts)


def _item_lengths(ctx, fl: Flavour) -> tuple[list[float], list[float]]:
    """(timeline length, rendered length) per item, card last. With cross-fades every item but the last is
    rendered `XFADE_S` longer so the overlaps eat the extra and audio stays in sync."""
    durations = list(ctx.data["durations"])
    if ctx.data["film"].get("end_card"):
        durations.append(CARD_S)
    td = XFADE_S if fl.stitch == "xfade" else 0.0
    return durations, [d + (td if i < len(durations) - 1 else 0.0) for i, d in enumerate(durations)]


def _animate(ctx, fl: Flavour) -> None:
    film, p = ctx.data["film"], ctx.params
    w, h = ffmpeg.target_size(p.get("aspect", "9:16"))
    _, rendered = _item_lengths(ctx, fl)
    shots = film["shots"]
    ai = is_ai(p)
    for n, shot in enumerate(shots):
        out = ctx.path("clips", f"shot_{n:02d}.mp4")
        still = ctx.path("stills", f"shot_{n:02d}.png")
        if out.is_file():
            ctx.progress((n + 1) / len(shots))
            continue
        part = sr.part_path(out)
        if not ai:
            sr.still_to_clip(still, part, rendered[n], w, h, sr.pick_effect(shot["camera"], n))
        else:
            provider, model, res = ai_target(p)
            raw = ctx.path("clips", f"raw_{n:02d}.mp4")
            if not raw.is_file():  # a paid clip is kept until it is normalised, so a retry never pays twice
                kitclips.generate_clip(
                    _clip_prompt(ctx, fl, film, shot), raw, provider=provider, model=model, aspect_ratio=p.get("aspect", "9:16"),
                    duration_seconds=clip_len(p, ctx.data["durations"][n]), resolution=res, image=still, timeout=min(kitclips.CLIP_TIMEOUT_S, ctx.remaining()),
                )
            if is_native(p):
                sr.extract_audio(raw, ctx.path("audio", f"shot_{n:02d}.wav"))
            ffmpeg.normalize_clip(raw, part, w, h, rendered[n], loop=True)
            raw.unlink(missing_ok=True)
        sr.commit(part, out)
        ctx.progress((n + 1) / len(shots))


def _assemble(ctx, fl: Flavour) -> None:
    film, p = ctx.data["film"], ctx.params
    w, h = ffmpeg.target_size(p.get("aspect", "9:16"))
    timeline, rendered = _item_lengths(ctx, fl)
    paths = [ctx.path("clips", f"shot_{n:02d}.mp4") for n in range(len(film["shots"]))]
    if film.get("end_card"):
        card_png = sr.render_card(film["end_card"], ctx.path("stills", "card.png"), w, h, bg=fl.card_bg)
        card = ctx.path("clips", "card.mp4")
        images.ken_burns(card_png, card, rendered[-1], w, h, effect="in")
        paths.append(card)
    dst = ctx.path("stitched.mp4")
    if fl.stitch == "xfade" and len(paths) > 1:
        ffmpeg.xfade_chain(paths, rendered, dst, transition="fade", td=XFADE_S)
    else:
        ffmpeg.concat(paths, dst)
    ctx.data["total"] = sum(timeline)


def _finish(ctx, fl: Flavour) -> None:
    film, p = ctx.data["film"], ctx.params
    durations = ctx.data["durations"]
    total = ctx.data["total"]
    t0, sentences, wavs = 0.0, [], []
    for n, shot in enumerate(film["shots"]):
        wav = ctx.path("audio", f"shot_{n:02d}.wav")
        wavs.append(wav if wav.is_file() else None)
        timings = ctx.data.get("tts", {}).get(str(n))
        if timings and not is_native(p):
            sentences += sr.offset_sentences(timings["sentences"], t0)
        elif shot["dialogue"]:  # native audio: no word times, spread the lines evenly over the shot
            k, span = len(shot["dialogue"]), (durations[n] - 0.4) / len(shot["dialogue"])
            for j, l in enumerate(shot["dialogue"]):
                s, e = t0 + 0.2 + j * span, t0 + 0.2 + (j + 1) * span
                sentences.append({"text": l["text"], "start": s, "end": e, "words": sr.even_words(l["text"], s, e)})
        t0 += durations[n]
    pad = total - sum(durations)  # end card is silent
    track = sr.build_track(ctx.path("audio", "track.wav"), [*wavs, None], [*durations, pad]) if pad > 0 else sr.build_track(ctx.path("audio", "track.wav"), wavs, durations)
    instrumental = (ctx.inputs.get("instrumental") or [None])[0]
    audio = ffmpeg.mix_audio(track, instrumental, ctx.path("audio", "mix.wav"), total, bgm_volume=0.35) if instrumental else track
    sr.finalize_film(
        ctx, ctx.path("stitched.mp4"), audio=audio, seconds=total, sentences=sentences, subtitles=bool(p.get("subtitles", True)),
        style=fl.caption_style, tag="AI PARODY" if p.get("parody_label") else None,
    )
    for f in [*ctx.dir.glob("clips/*.mp4"), ctx.path("stitched.mp4")]:  # the project folder keeps sheets and stills, not video intermediates
        f.unlink(missing_ok=True)


# --- engine ---


class SceneFilmEngine(Engine):
    """Subclasses set `flavours` (recipe id -> Flavour); stages and validation are shared."""

    flavours: dict[str, Flavour]

    def title(self, recipe: RecipeSpec, params: dict) -> str:
        fl = self.flavours[recipe.id]
        return str(params.get(fl.topic_param) or recipe.label).strip()[:60]

    def validate(self, recipe: RecipeSpec, params: dict, files: dict[str, list]) -> dict:
        out = super().validate(recipe, params, files)
        if is_ai(out) and out.get("clip_provider", "veo") == "veo" and out.get("aspect") == "1:1":
            raise ValidationError("'aspect' (Aspect ratio): Veo does not make 1:1 video. Choose 9:16 or 16:9, or switch the clip provider to Muapi.")
        return out

    def extra_keys(self, recipe: RecipeSpec, params: dict) -> list[str]:
        keys = []
        if is_ai(params) and params.get("clip_provider") == "muapi":
            keys.append("MUAPI_API_KEY")
        return keys

    def stages(self, recipe: RecipeSpec, params: dict) -> list[Stage]:
        fl = self.flavours[recipe.id]
        bind = lambda fn: partial(fn, fl=fl)  # noqa: E731
        stages = []
        if params.get("research"):
            stages.append(Stage("Researching the topic", bind(_research), "plan"))
        stages += [Stage("Writing script", bind(_write_script), "plan", 2), Stage("Designing characters", bind(_design_sheets), "plan", 3)]
        if not is_native(params):
            stages.append(Stage("Recording voices", bind(_record_voices), "plan", 2))
        stages.append(Stage("Preparing shot list", bind(_prepare), "plan"))
        stages += [
            Stage("Drawing shots", bind(_visuals), "render", 6),
            Stage("Animating shots", bind(_animate), "render", 8 if is_ai(params) else 3),
            Stage("Assembling video", bind(_assemble), "render", 2),
            Stage("Adding voices and captions", bind(_finish), "render", 3),
        ]
        return stages

    def estimate_cost(self, recipe: RecipeSpec, params: dict, plan: dict) -> float | None:
        est = (plan or {}).get("estimate")
        return est["usd"] if est else None

    def preplan_estimate(self, recipe: RecipeSpec, params: dict) -> dict:
        """Cost bounds before any plan exists (used by the cost layer): shot count x planned shot length."""
        fl = self.flavours[recipe.id]
        count = next((f.default for f in recipe.fields if f.name == fl.count_param), 5)
        n = int(params.get(fl.count_param) or count or 5)
        lo_s, hi_s = fl.shot_len
        n_chars = (1, 1 if fl.kind != "dialogue" else 4)
        out = {"images": (n + n_chars[0], n + n_chars[1]), "clip_seconds": (0, 0), "provider": None, "model": None, "resolution": None}
        if is_ai(params):
            provider, model, res = ai_target(params)
            out.update(provider=provider, model=model, resolution=res,
                       clip_seconds=(n * clip_len(params, lo_s), n * clip_len(params, hi_s)))
        return out

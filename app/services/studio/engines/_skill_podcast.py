"""Recipe `podcast`: Gemini writes a two-host dialogue (optionally grounded in a web search), each line is voiced with
its host's voice, and the picture is drawn with the studio_motion toolkit: a title bar, two host cards with initials
avatars, an active-speaker highlight and waveform bars driven by the real audio loudness."""

import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image as PILImage
from PIL import ImageDraw

from app.services import gemini_client
from app.services.studio.engines import _shared
from app.services.studio.engines import _skill_common as common
from app.services.studio.kit import ffmpeg, tts
from app.services.studio.motion import studio_motion as sm
from app.services.studio.registry import FieldSpec, RecipeSpec, Stage, StageError

TAIL_S = 0.6
EDGE_HOSTS = ("en-US-AndrewNeural", "en-US-AriaNeural")
GEMINI_HOSTS = ("Puck", "Kore")
MIN_LINES = 4

SPEC = RecipeSpec(
    "podcast", "AI podcast", "Two AI hosts discuss a topic over live waveform cards and captions.",
    [
        FieldSpec("topic", "Topic", "textarea", required=True),
        FieldSpec("research", "Research the topic on the web first", "toggle", default=False),
        FieldSpec("host_a", "Host 1 name", "text", default="Alex"),
        FieldSpec("host_b", "Host 2 name", "text", default="Sam"),
        FieldSpec("duration", "Target length (seconds)", "number", default=60, min=30, max=120),
        FieldSpec("tts_provider", "Voice provider", "select", default="edge",
                  options=[{"value": "edge", "label": "Edge TTS (free, 2 voices)"}, {"value": "gemini", "label": "Gemini TTS (multi-speaker)"}]),
        FieldSpec("voice_a", "Host 1 voice", "select", default="", options=tts.VOICE_OPTIONS, help="Auto picks a distinct voice for each host."),
        FieldSpec("voice_b", "Host 2 voice", "select", default="", options=tts.VOICE_OPTIONS),
        *common.caption_fields("bold-pop"),
        _shared.aspect_field(),
    ],
    paid=False, keys=("GEMINI_API_KEY",),
)


def title(params: dict) -> str:
    return params["topic"].strip().splitlines()[0][:60]


def stages(params: dict) -> list[Stage]:
    out = []
    if params.get("research"):
        out.append(Stage("Researching the topic", _research, "plan", weight=2))
    return out + [
        Stage("Writing the dialogue", _write_dialogue, "plan", weight=2),
        Stage("Recording the voices", _record, "plan", weight=3),
        Stage("Analysing the audio", _analyse, "render"),
        Stage("Drawing the studio", _draw, "render", weight=8),
        Stage("Finishing the video", _finish, "render", weight=2),
    ]


def host_names(params: dict) -> list[str]:
    a = common.clip_text(params.get("host_a"), 24) or "Alex"
    b = common.clip_text(params.get("host_b"), 24) or "Sam"
    if a.lower() == b.lower():
        b = f"{b} 2"
    return [a, b]


def host_voices(params: dict) -> list[str]:
    """Two distinct voices for the chosen provider (a voice picked for the other provider falls back to the default)."""
    provider = params.get("tts_provider", "edge")
    defaults = GEMINI_HOSTS if provider == "gemini" else EDGE_HOSTS
    out = []
    for key, default in zip(("voice_a", "voice_b"), defaults):
        v = (params.get(key) or "").strip()
        ok = v in tts.GEMINI_VOICES if provider == "gemini" else bool(re.fullmatch(r"[a-z]{2,3}-[A-Za-z0-9-]+Neural", v))
        out.append(v if ok else default)
    if out[0] == out[1]:
        out[1] = next(d for d in defaults if d != out[0])
    return out


def initials(name: str) -> str:
    letters = [w[0] for w in re.findall(r"[A-Za-z]+", name)][:2]
    return "".join(letters).upper() or "?"


# --- plan phase ---


def _research(ctx) -> None:
    text, sources = gemini_client.generate_grounded(
        f"""Research the topic below using Google Search and write 8-12 concise bullet points of accurate, current facts
a podcast could discuss (each with the key number or name where relevant). No filler.
Topic: {ctx.params['topic']}"""
    )
    ctx.data["research"] = text.strip()[:6000]
    ctx.data["sources"] = sources[:8]


def _write_dialogue(ctx) -> None:
    p = ctx.params
    names = host_names(p)
    target = int(p["duration"])
    words = int(target * common.WORDS_PER_SECOND)
    research = ctx.data.get("research")
    facts = f"Base the discussion ONLY on these researched facts (do not invent other statistics):\n{research}" if research else \
        "Keep claims general and well known; do not invent statistics, quotes or specific numbers."
    data = common.ask_json(
        f"""You write the script of a short two-host podcast episode.
Hosts: {names[0]} and {names[1]}. They are friendly, curious and a little funny; they react to each other and build on each point.
Topic: {p['topic']}
{facts}

Write about {words} words in total ({target} seconds spoken) as 8-16 dialogue turns, alternating mostly between the hosts. Each turn is one to
three short spoken sentences. {names[0]} opens with a hook and welcomes listeners in one short sentence; end with a quick sign-off.
Plain spoken English: no stage directions, no markdown, no emoji, no sound effects, no hashtags.
Return JSON: {{"title": "<max 7 words>", "lines": [{{"speaker": "{names[0]}" or "{names[1]}", "text": "<what they say>"}}]}}"""
    )
    raw = data.get("lines") if isinstance(data.get("lines"), list) else data.get("items", [])
    lines, last = [], 1
    budget = int(words * 1.3)
    used = 0
    for item in raw:
        text = common.clean_line(item.get("text") if isinstance(item, dict) else item)
        if not text:
            continue
        who = str(item.get("speaker", "")).strip().lower() if isinstance(item, dict) else ""
        if who in (names[1].lower(), "b", "2", "host 2"):
            idx = 1
        elif who in (names[0].lower(), "a", "1", "host 1"):
            idx = 0
        else:
            idx = 1 - last
        used += len(text.split())
        if lines and used > budget:
            break
        lines.append({"speaker": idx, "text": text})
        last = idx
    if len(lines) < MIN_LINES:
        raise StageError("Gemini returned too little dialogue. Retry, or make the topic more specific.")
    ctx.data["dialogue"] = {"title": common.clip_text(data.get("title"), 70) or title(p), "lines": lines, "names": names}
    ctx.plan.update(script="\n".join(f"{names[l['speaker']]}: {l['text']}" for l in lines))
    sources = ctx.data.get("sources") or []
    ctx.plan["notes"] = "Dialogue written by AI" + (f", grounded in web sources: {', '.join(s['title'] or s['url'] for s in sources[:4])}." if sources else "; no web research.")


def _record(ctx) -> None:
    p = ctx.params
    dialogue = ctx.data["dialogue"]
    voices = host_voices(p)
    texts = [line["text"] for line in dialogue["lines"]]
    result = tts.synthesize(
        texts, ctx.path("narration.wav"), provider=p.get("tts_provider", "edge"),
        voices=[voices[line["speaker"]] for line in dialogue["lines"]], language="en", progress=ctx.progress,
    )
    ctx.register("narration", result.path, label="Podcast audio", kind="audio", preview=True)
    total = result.duration + TAIL_S
    ctx.data["tts"] = {"duration": result.duration, "sentences": result.sentences}
    ctx.data["total"] = total
    ctx.data["segments"] = [[line["speaker"], s["start"], s["end"]] for line, s in zip(dialogue["lines"], result.sentences)]
    names = dialogue["names"]
    ctx.plan.update(
        summary=f"{total:.0f} s {p['aspect']} podcast, {names[0]} and {names[1]}: {dialogue['title']}.",
        scenes=[
            {"index": i + 1, "text": f"{names[line['speaker']]}: {s['text']}", "visual": f"{names[line['speaker']]} card highlighted", "duration_s": round(s["end"] - s["start"], 1)}
            for i, (line, s) in enumerate(zip(dialogue["lines"], result.sentences))
        ],
    )


# --- render phase ---


def _analyse(ctx) -> None:
    env = common.audio_envelope(ctx.asset("narration"), ffmpeg.FPS)
    ctx.path("envelope.json").write_text(json.dumps(env))


# studio_motion keeps its frame size in module globals, so renders in this process are serialised
_RENDER_LOCK = threading.Lock()


class WaveBars(sm.Element):
    """Bars that ripple outwards from the centre, their height following the voice loudness `env` (one value per frame)."""

    def __init__(self, env: np.ndarray, fps: int, x: float, y: float, w: float, h: float, color, idle_color, n: int = 19):
        super().__init__(x, y)
        self.env, self.fps, self.w, self.h, self.n = env, fps, max(int(w), 8), max(int(h), 8), n
        self.color, self.idle = sm.rgba(color), sm.rgba(idle_color)
        self.frames = np.arange(len(env), dtype=np.float32)

    def level(self, t: float) -> float:
        if len(self.env) < 2:
            return 0.0
        return float(np.interp(max(t, 0.0) * self.fps, self.frames, self.env))

    def patch(self, t):
        img = PILImage.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        slot = self.w / self.n
        bar_w = max(int(slot * 0.58), 2)
        centre = (self.n - 1) / 2
        for i in range(self.n):
            dist = abs(i - centre)
            lv = self.level(t - dist * 0.035)
            taper = 0.35 + 0.65 * math.cos(dist / (centre + 1) * math.pi / 2)
            wobble = 0.8 + 0.2 * math.sin(t * 9.0 + i * 1.7)
            frac = 0.07 + 0.93 * min(lv * taper * wobble * 1.15, 1.0)
            bh = max(int(self.h * frac), bar_w)
            x0 = int(i * slot + (slot - bar_w) / 2)
            y0 = (self.h - bh) // 2
            d.rounded_rectangle((x0, y0, x0 + bar_w, y0 + bh), radius=bar_w // 2, fill=self.color if lv > 0.04 else self.idle)
        return img


def _frame_levels(env: list[float], segments: list, host: int, fps: int, frames: int) -> np.ndarray:
    """The envelope with everything outside `host`'s lines silenced."""
    base = np.zeros(frames, dtype=np.float32)
    src = np.asarray(env, dtype=np.float32)
    for who, start, end in segments:
        if who != host:
            continue
        a, b = int(start * fps), min(int(end * fps) + 1, frames)
        n = min(b, len(src)) - a
        if n > 0:
            base[a : a + n] = src[a : a + n]
    return base


def build_scene(*, title_text: str, names: list[str], segments: list, env: list[float], fps: int, duration: float, width: int, height: int):
    """The podcast set as a studio_motion Scene (studio_motion must already be configured for this size)."""
    scene = sm.Scene(palette="midnight")
    pal = scene.palette
    colors = (pal.accent, "#FB7185")
    frames = int(duration * fps) + 2
    tall = height > width
    scene.background(pal.bg, pal.bg2)
    scene.glow(width * 0.2, height * 0.25, sm.u(700), colors[0], 0.16)
    scene.glow(width * 0.8, height * 0.7, sm.u(700), colors[1], 0.16)

    safe = scene.safe
    bar_top = height * (0.05 if tall else 0.045)
    bar = sm.Box(safe.x, bar_top, safe.w, height * (0.085 if tall else 0.12))
    region_top = bar.bottom + height * 0.025
    region_bottom = height * (0.72 if tall else 0.77 if width > height else 0.76)
    gap = sm.u(40)
    region = sm.Box(safe.x, region_top, safe.w, region_bottom - region_top)
    cards = region.rows(2, gap) if tall else region.cols(2, gap)

    title_bg = sm.RoundedRect(bar.cx, bar.cy, bar.w, bar.h, color=pal.card, radius=sm.u(36))
    title_bg.slide_in(at=0.0, from_="top", dur=0.5)
    dot = sm.Circle(bar.left + sm.u(52), bar.cy, sm.u(14), color="#F87171")
    label = sm.Text(title_text.upper(), size=sm.u(46), color=pal.fg, x=bar.cx + sm.u(26), y=bar.cy, max_width=bar.w - sm.u(160))
    label.fade_in(at=0.2, dur=0.5)
    scene.add(title_bg, dot, label)

    for host, card in enumerate(cards):
        color = colors[host]
        body = sm.RoundedRect(card.cx, card.cy, card.w, card.h, color=pal.card, radius=sm.u(44))
        body.slide_in(at=0.1 + 0.12 * host, from_="bottom" if tall else ("left" if host == 0 else "right"), dur=0.6)
        scene.add(body)
        if tall:
            r = card.h * 0.29
            ax, ay = card.left + sm.u(40) + r, card.cy
            tx = card.left + sm.u(40) + 2 * r + sm.u(44)
            tw = card.right - tx - sm.u(40)
            name_pos, tag_pos = (tx + tw / 2, card.top + card.h * 0.22), (tx + tw / 2, card.top + card.h * 0.43)
            wave = (tx + tw / 2, card.top + card.h * 0.72, tw, card.h * 0.36)
            name_w = tw
        else:
            r = min(card.h * 0.2, card.w * 0.24)
            ax, ay = card.cx, card.top + card.h * 0.3
            name_pos, tag_pos = (card.cx, card.top + card.h * 0.63), (card.cx, card.top + card.h * 0.74)
            wave = (card.cx, card.top + card.h * 0.88, card.w * 0.78, card.h * 0.17)
            name_w = card.w * 0.9
        avatar = sm.Circle(ax, ay, r, color=color)
        init = sm.Text(initials(names[host]), size=r * 1.0, color=pal.bg, x=ax, y=ay + r * 0.02)
        name = sm.Text(names[host].upper(), size=sm.u(64) if tall else sm.u(58), color=pal.fg, x=name_pos[0], y=name_pos[1], max_width=name_w)
        for el in (avatar, init, name):
            el.fade_in(at=0.3 + 0.12 * host, dur=0.4)
        host_env = _frame_levels(env, segments, host, fps, frames)
        bars = WaveBars(host_env, fps, wave[0], wave[1], wave[2], wave[3], color, pal.muted)
        bars.fade_in(at=0.4, dur=0.4)
        scene.add(avatar, init, name, bars)
        for who, start, end in segments:
            if who != host:
                continue
            ring = sm.RoundedRect(card.cx, card.cy, card.w + sm.u(14), card.h + sm.u(14), color=sm.rgba(color, 0.10), radius=sm.u(50), outline=sm.u(8), outline_color=color)
            ring.show(start, end + 0.12).fade_in(start, 0.12)
            tag = sm.Text("SPEAKING", size=sm.u(34), color=color, x=tag_pos[0], y=tag_pos[1], max_width=name_w)
            tag.show(start, end + 0.12).fade_in(start, 0.12)
            scene.add(ring, tag)
    scene.fade_in(0.3)
    scene.fade_out(0.5)
    return scene


def render_podcast(dst: Path, *, title_text: str, names: list[str], segments: list, env: list[float], width: int, height: int,
                   fps: int, duration: float, timeout: float, progress=None) -> Path:
    """Render the podcast set to a silent mp4 (frames are piped straight into ffmpeg)."""
    with _RENDER_LOCK:
        sm.configure(width, height, fps, duration)
        scene = build_scene(title_text=title_text, names=names, segments=segments, env=env, fps=fps, duration=duration, width=width, height=height)
        total = round(duration * fps)
        dst.parent.mkdir(parents=True, exist_ok=True)
        enc = subprocess.Popen(
            [
                ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst),
            ],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + timeout
        try:
            for i in range(total):
                enc.stdin.write(scene.render_frame(i / fps).tobytes())
                if i % 15 == 0:
                    if time.monotonic() > deadline:
                        raise StageError("Drawing the podcast took too long.")
                    if progress:
                        progress(i / total)
            enc.stdin.close()
            if enc.wait(timeout=120) != 0:
                raise StageError(f"FFmpeg could not encode the podcast frames: {(enc.stderr.read() or b'').decode()[-300:]}")
        except BrokenPipeError:
            raise StageError(f"FFmpeg stopped while encoding: {(enc.stderr.read() or b'').decode()[-300:]}") from None
        except BaseException:
            enc.kill()
            enc.wait()
            dst.unlink(missing_ok=True)
            raise
    return dst


def _draw(ctx) -> None:
    w, h = ffmpeg.target_size(ctx.params["aspect"])
    d = ctx.data["dialogue"]
    render_podcast(
        ctx.path("podcast.mp4"), title_text=d["title"], names=d["names"], segments=ctx.data["segments"],
        env=json.loads(ctx.path("envelope.json").read_text()), width=w, height=h, fps=ffmpeg.FPS, duration=ctx.data["total"],
        timeout=ctx.remaining(), progress=ctx.progress,
    )


def _finish(ctx) -> None:
    p = ctx.params
    _shared.compose_final(
        ctx, ctx.path("podcast.mp4"), seconds=ctx.data["total"], audio=ctx.asset("narration"), sentences=ctx.data["tts"]["sentences"],
        captions_on=bool(p.get("subtitles")), style=p.get("caption_style", "bold-pop"), position=p.get("subtitle_position", "bottom"),
    )
    ctx.path("podcast.mp4").unlink(missing_ok=True)

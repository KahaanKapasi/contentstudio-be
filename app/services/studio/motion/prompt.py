"""Gemini prompts for the motion engine: narration writer, scene-code writer, scene-code fixer."""

from app.services.studio.motion.examples import EXAMPLES

REFERENCE = '''
studio_motion API (everything is already imported by `from studio_motion import *`; no other imports are allowed
except `import math` / `import random`). Frame size and timing come from the constants W, H (pixels), FPS and
DURATION (seconds). Use `u(px)` for sizes: it scales design pixels (written for a 1080-wide frame) to the real frame.

scene = Scene(palette="madrid")     # REQUIRED top-level variable. Palettes: madrid (navy/gold/white), midnight, sunset, neon, pitch, mono, paper.
scene.palette.{bg,bg2,fg,muted,accent,accent2,card,good,bad}   # hex colours of the palette
scene.safe / scene.box              # Box: the area free of platform UI / the whole frame
scene.background(color, color2=None)  # solid or top-to-bottom gradient; scene.glow(x, y, radius, color=None, opacity=0.35) soft light
scene.add(*elements)                # draw order = add order (or Element(layer=n))
scene.fade_in(dur) / scene.fade_out(dur)   # whole-frame fade; end every scene with scene.fade_out(0.5)

Elements (x, y are pixel positions of the element's anchor; anchor="center" by default, also left right top bottom
topleft topright bottomleft bottomright). All take opacity=1, scale=1, layer=0.
  Text(text, size, color=None, x, y, align="center", max_width=None, stroke=0, stroke_color="#000000", shadow=0, upper=False)
  Counter(start, end, at=0.0, dur=1.5, decimals=0, prefix="", suffix="", use_commas=True, compact_numbers=False, **Text args)
  Rect(x, y, w, h, color, radius=0, outline=0, outline_color)      RoundedRect(x, y, w, h, color, radius=24)      Circle(x, y, r, color)
  Bar(x, y, w, h, value=1.0, color, track_color, direction="h"|"v")   # value 0..1 of the track
  ProgressRing(x, y, radius, thickness, value=1.0, color, track_color)  # clockwise from the top
  Line(x1, y1, x2, y2, color, width=6)
  Image(asset, x, y, w, h, fit="cover", radius=0)   # ONLY for asset names listed in the brief
Every element method returns the element (chainable). Times are seconds from the start of the video:
  el.fade_in(at, dur)   el.fade_out(at, dur)   el.slide_in(at, dur=0.6, from_="bottom"|"top"|"left"|"right", distance=None)
  el.pop(at, dur=0.5)   el.move_to(x=None, y=None, at, dur)   el.scale_to(s, at, dur)   el.show(at, until=None)
  el.animate(prop, to, at, dur, frm=None, ease=ease_out_cubic)   # prop: x y scale opacity (Bar/Ring/Line: value / progress; Rect: w h)
  Bar.grow(at, dur)   ProgressRing.grow(at, dur)   Line.draw_on(at, dur)
An element that has a fade_in/slide_in/pop is invisible before its start time. Elements with no animation are always visible.
Easing: ease_linear ease_in_quad ease_out_quad ease_in_out_quad ease_out_cubic ease_in_out_cubic ease_out_expo ease_in_out_sine ease_out_back ease_out_elastic.
Layout: Box has .x .y .w .h .left .right .top .bottom .cx .cy, inset(px), rows(n, gap=0), cols(n, gap=0), split(*ratios, axis="v"|"h"), point(fx, fy).
        grid(box, rows, cols, gap=0) -> row-major list of Boxes.
Math/helpers: clamp lerp remap(v, a, b, c, d) stagger(i, step=0.12, start=0) commas(n) compact(n) rgba(hex, opacity) math random.
'''

RULES = '''
Rules:
- Return ONE ```python code block and nothing else. The code must define the top-level variable `scene`.
- Never call render/save/show; the harness renders the scene. No files, network, classes, `with`, getattr/eval/open/type or names starting with underscore.
- Only the names above exist. Only one font exists (bold condensed display): it has Latin letters, digits and basic punctuation, NO emoji and no
  non-Latin scripts. ALL CAPS reads best for headlines; keep text short.
- Design for the frame in W x H. Keep every element inside scene.safe (it keeps clear of the app UI). Position with Box helpers, not magic numbers.
- Sizes through u(): headline u(80-110), big numbers u(250-450), body u(50-70), minimum readable u(44).
- Everything must happen inside DURATION seconds. Open with motion within 0.3 s, one idea at a time (a new beat every 1.5-3 s), hold the final state
  for at least 1 s and end with scene.fade_out(0.5). Stagger lists with stagger() or a per-item `at`.
- Use at most ~60 elements. Use the palette colours (scene.palette.*) rather than raw hex, except for brand colours the brief names.
- If narration timing cues are given, trigger each beat at its cue time so visuals match the voice.
'''


def _frame_line(aspect: str, w: int, h: int, duration: float) -> str:
    return f"Frame: {aspect} ({w}x{h} px at full size), {duration:g} seconds, 30 fps."


def narration_prompt(brief: str, data: str, seconds: float, words: int, language: str = "English") -> str:
    return f"""You write the voice-over for a short motion-graphics video.

Brief: {brief}
Facts/data to use (only these numbers, never invent statistics):
{data or '(none given: keep claims general and verifiable)'}

Write a voice-over of about {words} words (it must be speakable in about {max(seconds - 1.5, 3):.0f} seconds) in {language}.
Short punchy sentences, a hook in the first sentence, no emoji, no stage directions, no hashtags.
Return JSON: {{"title": "<max 6 words>", "narration": "<the voice-over text>"}}"""


def scene_prompt(*, brief: str, data: str, aspect: str, width: int, height: int, duration: float, palette: str, cues: list[dict], assets: list[str]) -> str:
    examples = "\n\n".join(f"Example: {name}\n```python\n{code.strip()}\n```" for name, code in EXAMPLES)
    cue_text = "\n".join(f"  {c['start']:.1f}s-{c['end']:.1f}s: {c['text']}" for c in cues) or "  (no narration: pace the beats yourself)"
    palette_line = f'Use palette="{palette}".' if palette and palette != "auto" else "Choose the palette that best fits the brief."
    asset_line = "Image assets available: " + ", ".join(assets) if assets else "No image assets exist: do not use Image."
    return f"""You are a motion designer who writes Python scenes for the studio_motion toolkit. The scene is rendered
frame by frame and encoded to video.

{_frame_line(aspect, width, height, duration)} W={width}, H={height}, DURATION={duration:g} are provided as constants.
{palette_line} {asset_line}

Brief: {brief}
Data (use exactly these numbers; do not invent others):
{data or '(none)'}
Narration timing cues (sync visual beats to these):
{cue_text}
{REFERENCE}{RULES}
{examples}

Now write the scene for the brief above."""


def fix_prompt(original_prompt: str, code: str, error: str) -> str:
    return f"""{original_prompt}

Your previous attempt:
```python
{code.strip()}
```
It failed with this error:
{error}

Fix the problem. Return the complete corrected scene as ONE ```python code block and nothing else."""

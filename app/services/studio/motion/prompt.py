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
- Use the whole canvas: spread the content over the full content band given under "Layout" (do not cluster everything in one part of the frame).
'''


def layout_guidance(width: int, height: int, captions: bool) -> str:
    """Where content may go. Tall (9:16) frames used to cluster in the top ~60%; give the model an explicit vertical plan."""
    if height > width:
        bottom = 0.70 if captions else 0.82
        caption_line = (
            f"Burned-in captions will cover y = {0.72 * height:.0f}-{0.84 * height:.0f} px (the lower third), so keep ALL content above y = {bottom * height:.0f} px and leave that strip empty."
            if captions else
            f"Nothing else is drawn over the frame, but platform UI covers everything below y = {0.82 * height:.0f} px."
        )
        return f"""Layout (tall {width}x{height} frame, vertical distribution matters):
- The content band is y = {0.09 * height:.0f} to {bottom * height:.0f} px ({100 * bottom - 9:.0f}% of the height). {caption_line}
- Divide the band into 3-5 rows with Box.split (e.g. `title, hero, detail = band.split(2, 5, 3)` where `band = Box(scene.safe.x, scene.safe.y, scene.safe.w, {bottom * height:.0f} - scene.safe.y)`) and put a real element in EVERY row.
- Visual centre of mass near the vertical middle of the band; the lowest main element should end close to y = {bottom * height:.0f} px, the highest start near y = {0.09 * height:.0f} px.
- A hero number/graphic belongs in the middle rows, not the top. Lists, bars and cards should stretch across the band using grid()/rows() with the full band height.
- Never leave more than ~15% of the content band empty at the bottom or the top."""
    if width > height:
        return f"""Layout (wide {width}x{height} frame): use the full width; keep content between y = {0.08 * height:.0f} and y = {(0.78 if captions else 0.92) * height:.0f} px"""
    return f"""Layout (square {width}x{height} frame): centre the composition and fill the safe box evenly; keep content above y = {(0.74 if captions else 0.93) * height:.0f} px"""


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


def scene_prompt(*, brief: str, data: str, aspect: str, width: int, height: int, duration: float, palette: str, cues: list[dict], assets: list[str], captions: bool = False) -> str:
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
{layout_guidance(width, height, captions)}
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

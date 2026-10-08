"""studio_motion: a small 2D motion-graphics toolkit. Scenes are code, frames are Pillow images.

Gemini-written scene code uses ONLY this module (`from studio_motion import *`). It runs inside the sandbox
subprocess (see sandbox.py), so it must stay dependency-light: Pillow + stdlib, no imports from `app`.

    scene = Scene(palette="madrid")
    title = Text("78%", size=u(260), y=scene.safe.cy, color=scene.palette.accent)
    title.pop(at=0.3)
    scene.add(title)

A scene is a list of elements; every element has x, y (pixels), scale and opacity that can be animated with
`animate()` or the shortcuts `fade_in`, `fade_out`, `slide_in`, `pop`, `move_to`. Times are in seconds.
The harness injects W, H, FPS and DURATION (see `configure`) and calls `scene.render_frame(t)`.
"""

import math
import random  # noqa: F401  (re-exported for scene code via `from studio_motion import *`)
from functools import lru_cache
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFilter, ImageFont

FONT_PATH = Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Anton-Regular.ttf"
SUPERSAMPLE = 3

W, H, FPS, DURATION = 1080, 1920, 30, 15.0
ASSETS: dict = {}  # name -> image path, injected by the harness


def configure(width: int, height: int, fps: int, duration: float, assets: dict | None = None) -> None:
    global W, H, FPS, DURATION, ASSETS
    W, H, FPS, DURATION, ASSETS = int(width), int(height), int(fps), float(duration), dict(assets or {})


def u(px: float) -> float:
    """Design pixels: sizes written for a 1080-wide frame, scaled to the real frame (min side / 1080)."""
    return px * min(W, H) / 1080


# --- maths & easing ---


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def lerp(a, b, k):
    return a + (b - a) * k


def remap(v, a, b, c, d):
    return c + (d - c) * clamp((v - a) / (b - a) if b != a else 0.0)


def stagger(i, step=0.12, start=0.0):
    """Start time of the i-th item in a staggered reveal."""
    return start + i * step


def ease_linear(k): return k
def ease_in_quad(k): return k * k
def ease_out_quad(k): return 1 - (1 - k) ** 2
def ease_in_out_quad(k): return 2 * k * k if k < 0.5 else 1 - (-2 * k + 2) ** 2 / 2
def ease_out_cubic(k): return 1 - (1 - k) ** 3
def ease_in_out_cubic(k): return 4 * k ** 3 if k < 0.5 else 1 - (-2 * k + 2) ** 3 / 2
def ease_out_expo(k): return 1 if k >= 1 else 1 - 2 ** (-10 * k)
def ease_in_out_sine(k): return -(math.cos(math.pi * k) - 1) / 2


def ease_out_back(k, s=1.70158):
    return 1 + (s + 1) * (k - 1) ** 3 + s * (k - 1) ** 2


def ease_out_elastic(k):
    if k <= 0 or k >= 1:
        return clamp(k)
    return 2 ** (-10 * k) * math.sin((k * 10 - 0.75) * (2 * math.pi) / 3) + 1


def commas(n, decimals=0):
    return f"{n:,.{decimals}f}"


def compact(n):
    """1234 -> 1.2K, 3_400_000 -> 3.4M"""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:.0f}"


# --- colours & palettes ---


def rgba(color, opacity=1.0):
    """'#RRGGBB' / '#RRGGBBAA' / (r,g,b[,a]) -> (r,g,b,a)"""
    if isinstance(color, str):
        c = color.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        vals = [int(c[i : i + 2], 16) for i in range(0, len(c), 2)]
    else:
        vals = list(color)
    if len(vals) == 3:
        vals.append(255)
    return (vals[0], vals[1], vals[2], round(vals[3] * clamp(opacity)))


class Palette:
    def __init__(self, bg, bg2, fg, muted, accent, accent2, card, good="#4ADE80", bad="#F87171"):
        self.bg, self.bg2, self.fg, self.muted = bg, bg2, fg, muted
        self.accent, self.accent2, self.card = accent, accent2, card
        self.good, self.bad = good, bad


PALETTES = {
    "madrid": Palette("#0A1B3D", "#142A5C", "#FFFFFF", "#9FB1D6", "#F2C14E", "#E8EEF9", "#16295A"),
    "midnight": Palette("#0B0F1A", "#151C2E", "#F5F7FB", "#8A94AD", "#38BDF8", "#818CF8", "#1B2338"),
    "sunset": Palette("#1A0B2E", "#2E1248", "#FFF4EC", "#C3A6C9", "#FF8A3D", "#FF4D6D", "#2B1646"),
    "neon": Palette("#08060F", "#140E26", "#FFFFFF", "#9B93B8", "#00F5D4", "#F15BB5", "#1B1433"),
    "pitch": Palette("#0B3D2E", "#0F5132", "#FFFFFF", "#A7D3BE", "#FDE047", "#FFFFFF", "#12513C"),
    "mono": Palette("#0A0A0A", "#171717", "#FFFFFF", "#8C8C8C", "#FFFFFF", "#B5B5B5", "#1F1F1F"),
    "paper": Palette("#F5F1E8", "#EAE3D2", "#14213D", "#6B7280", "#E63946", "#1D3557", "#FFFFFF", "#2A9D8F", "#E63946"),
}
_current_palette = PALETTES["midnight"]


def _pal() -> Palette:
    return _current_palette


# --- layout ---


class Box:
    """A rectangle with layout helpers. x, y is the top-left corner."""

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    left = property(lambda s: s.x)
    top = property(lambda s: s.y)
    right = property(lambda s: s.x + s.w)
    bottom = property(lambda s: s.y + s.h)
    cx = property(lambda s: s.x + s.w / 2)
    cy = property(lambda s: s.y + s.h / 2)

    def inset(self, px):
        return Box(self.x + px, self.y + px, self.w - 2 * px, self.h - 2 * px)

    def point(self, fx, fy):
        """(x, y) at fractions of the box, e.g. point(0.5, 0.25)."""
        return (self.x + self.w * fx, self.y + self.h * fy)

    def rows(self, n, gap=0):
        h = (self.h - gap * (n - 1)) / n
        return [Box(self.x, self.y + i * (h + gap), self.w, h) for i in range(n)]

    def cols(self, n, gap=0):
        w = (self.w - gap * (n - 1)) / n
        return [Box(self.x + i * (w + gap), self.y, w, self.h) for i in range(n)]

    def split(self, *ratios, axis="v"):
        """Split into parts by ratio, top to bottom (axis='v') or left to right (axis='h')."""
        total, out, pos = sum(ratios), [], 0.0
        for r in ratios:
            frac = r / total
            if axis == "v":
                out.append(Box(self.x, self.y + pos * self.h, self.w, self.h * frac))
            else:
                out.append(Box(self.x + pos * self.w, self.y, self.w * frac, self.h))
            pos += frac
        return out


def grid(box, rows, cols, gap=0):
    """Row-major list of rows*cols Boxes."""
    return [cell for row in box.rows(rows, gap) for cell in row.cols(cols, gap)]


def safe_box(width, height) -> Box:
    """Area that stays clear of platform UI: tall frames keep ~10% top and ~18% bottom free."""
    if height > width:
        return Box(width * 0.07, height * 0.10, width * 0.86, height * 0.72)
    if width > height:
        return Box(width * 0.06, height * 0.08, width * 0.88, height * 0.84)
    return Box(width * 0.07, height * 0.07, width * 0.86, height * 0.86)


# --- drawing helpers ---


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), max(int(size), 4))


_ANCHORS = {
    "center": (0.5, 0.5), "left": (0.0, 0.5), "right": (1.0, 0.5), "top": (0.5, 0.0), "bottom": (0.5, 1.0),
    "topleft": (0.0, 0.0), "topright": (1.0, 0.0), "bottomleft": (0.0, 1.0), "bottomright": (1.0, 1.0),
}


def _blit(canvas, patch, x, y, anchor, scale, opacity):
    """Alpha-composite `patch` so that its anchor point lands on (x, y); scale is about that point."""
    pw, ph = patch.size
    if scale != 1:
        pw, ph = max(1, round(pw * scale)), max(1, round(ph * scale))
        patch = patch.resize((pw, ph), PILImage.BICUBIC)
    if opacity < 0.999:
        patch = patch.copy()
        patch.putalpha(patch.getchannel("A").point([round(v * opacity) for v in range(256)]))
    ax, ay = _ANCHORS[anchor]
    left, top = round(x - ax * pw), round(y - ay * ph)
    patch, dest = _clip_to(patch, left, top, *canvas.size)
    canvas.alpha_composite(patch, dest)


def _clip_to(patch, left, top, cw, ch):
    """(patch cropped to the canvas, (x, y) dest) for alpha_composite; the patch may hang off any edge."""
    l0, t0 = max(left, 0), max(top, 0)
    r0, b0 = min(left + patch.width, cw), min(top + patch.height, ch)
    if r0 <= l0 or b0 <= t0:
        return PILImage.new("RGBA", (1, 1), (0, 0, 0, 0)), (0, 0)
    return patch.crop((l0 - left, t0 - top, r0 - left, b0 - top)), (l0, t0)


def _supersampled(w, h, draw_fn):
    """Draw at SUPERSAMPLE x on an RGBA patch and downscale for smooth edges. draw_fn(draw, k)."""
    k = SUPERSAMPLE
    big = PILImage.new("RGBA", (max(1, round(w * k)), max(1, round(h * k))), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(big), k)
    return big.resize((max(1, round(w)), max(1, round(h))), PILImage.LANCZOS)


# --- elements ---


class Element:
    """Base: position (x, y), scale and opacity, all animatable. `anchor` says which point of the element
    sits at (x, y): center (default), left, right, top, bottom, topleft, topright, bottomleft, bottomright."""

    def __init__(self, x=0.0, y=0.0, anchor="center", opacity=1.0, scale=1.0, layer=0):
        if anchor not in _ANCHORS:
            raise ValueError(f"anchor must be one of {sorted(_ANCHORS)}")
        self.anchor, self.layer = anchor, layer
        self._base = {"x": float(x), "y": float(y), "opacity": float(opacity), "scale": float(scale)}
        self._tracks: dict[str, list] = {}
        self._window = (0.0, None)

    # animation
    def animate(self, prop, to, at=0.0, dur=0.5, frm=None, ease=ease_out_cubic):
        """Tween `prop` to `to` starting at `at` seconds for `dur` seconds. Before `at` the element holds `frm`
        (default: the previous value of that property)."""
        if prop not in self._base:
            raise ValueError(f"cannot animate '{prop}' on {type(self).__name__}; options: {sorted(self._base)}")
        tracks = self._tracks.setdefault(prop, [])
        start = frm if frm is not None else (tracks[-1][3] if tracks else self._base[prop])
        tracks.append((float(at), float(at) + max(float(dur), 1e-6), start, to, ease))
        tracks.sort(key=lambda tr: tr[0])
        return self

    def fade_in(self, at=0.0, dur=0.5, ease=ease_out_cubic):
        return self.animate("opacity", self._base["opacity"], at, dur, frm=0.0, ease=ease)

    def fade_out(self, at, dur=0.5, ease=ease_in_quad):
        return self.animate("opacity", 0.0, at, dur, ease=ease)

    def slide_in(self, at=0.0, dur=0.6, from_="bottom", distance=None, ease=ease_out_cubic):
        """Slide in from the `from_` side ('bottom', 'top', 'left', 'right') while fading in."""
        d = u(120) if distance is None else distance
        dx, dy = {"bottom": (0, d), "top": (0, -d), "left": (-d, 0), "right": (d, 0)}[from_]
        if dx:
            self.animate("x", self._base["x"], at, dur, frm=self._base["x"] + dx, ease=ease)
        if dy:
            self.animate("y", self._base["y"], at, dur, frm=self._base["y"] + dy, ease=ease)
        return self.fade_in(at, dur * 0.7)

    def pop(self, at=0.0, dur=0.5):
        """Scale up from 60% with a little overshoot while fading in."""
        s = self._base["scale"]
        self.animate("scale", s, at, dur, frm=s * 0.6, ease=ease_out_back)
        return self.fade_in(at, dur * 0.5)

    def move_to(self, x=None, y=None, at=0.0, dur=0.6, ease=ease_in_out_cubic):
        if x is not None:
            self.animate("x", x, at, dur, ease=ease)
        if y is not None:
            self.animate("y", y, at, dur, ease=ease)
        return self

    def scale_to(self, s, at=0.0, dur=0.5, ease=ease_in_out_cubic):
        return self.animate("scale", s, at, dur, ease=ease)

    def show(self, at=0.0, until=None):
        """Only draw the element between `at` and `until` seconds."""
        self._window = (at, until)
        return self

    # evaluation
    def value(self, prop, t):
        tracks = self._tracks.get(prop)
        if not tracks:
            return self._base[prop]
        if t < tracks[0][0]:
            return tracks[0][2]
        v = tracks[0][2]
        for t0, t1, v0, v1, ease in tracks:
            if t >= t1:
                v = v1
            elif t >= t0:
                return v0 + (v1 - v0) * ease((t - t0) / (t1 - t0))
            else:
                break
        return v

    def visible(self, t):
        at, until = self._window
        return t >= at and (until is None or t <= until)

    def render(self, canvas, t):
        if not self.visible(t):
            return
        opacity = clamp(self.value("opacity", t))
        scale = self.value("scale", t)
        if opacity <= 0.004 or scale <= 0.01:
            return
        patch = self.patch(t)
        if patch is not None:
            _blit(canvas, patch, self.value("x", t), self.value("y", t), self.anchor, scale, opacity)

    def patch(self, t):  # overridden: the RGBA image for time t
        return None


class Rect(Element):
    """Filled rectangle `w` x `h` (optionally rounded or outlined). w and h can be animated too."""

    def __init__(self, x=0, y=0, w=100, h=100, color=None, radius=0, outline=0, outline_color=None, **kw):
        super().__init__(x, y, **kw)
        self._base.update(w=float(w), h=float(h))
        self.color = rgba(color if color is not None else _pal().card)
        self.radius, self.outline = radius, outline
        self.outline_color = rgba(outline_color if outline_color is not None else _pal().fg)
        self._cache: dict = {}

    def patch(self, t):
        key = (max(1, round(self.value("w", t))), max(1, round(self.value("h", t))))
        if key not in self._cache:
            if len(self._cache) > 16:
                self._cache.clear()
            self._cache[key] = self._make(*key)
        return self._cache[key]

    def _make(self, w, h):
        radius = min(self.radius, w / 2, h / 2)
        if radius <= 0 and not self.outline:
            return PILImage.new("RGBA", (w, h), self.color)

        def draw(d, k):
            box = (0, 0, w * k - 1, h * k - 1)
            d.rounded_rectangle(box, radius=radius * k, fill=self.color)
            if self.outline:
                d.rounded_rectangle(box, radius=radius * k, outline=self.outline_color, width=round(self.outline * k))

        return _supersampled(w, h, draw)


def RoundedRect(x=0, y=0, w=100, h=100, color=None, radius=24, **kw):
    """Rect with rounded corners (radius in px, default 24)."""
    return Rect(x, y, w, h, color, radius=radius, **kw)


def Circle(x=0, y=0, r=50, color=None, **kw):
    return Rect(x, y, 2 * r, 2 * r, color, radius=r, **kw)


def _wrap(text, font, max_width):
    if not max_width:
        return text.split("\n")
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if line and font.getlength(trial) > max_width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


class Text(Element):
    """Text in the bundled bold display font. `size` is px (use u()). `max_width` wraps; `align` is
    left/center/right for multi-line text. Optional `stroke` outline and soft `shadow` (px offset)."""

    def __init__(self, text="", size=64, color=None, x=0, y=0, align="center", max_width=None, stroke=0,
                 stroke_color="#000000", shadow=0, line_spacing=1.12, upper=False, **kw):
        super().__init__(x, y, **kw)
        self.text, self.size, self.align, self.max_width = str(text), size, align, max_width
        self.color = rgba(color if color is not None else _pal().fg)
        self.stroke, self.stroke_color, self.shadow = round(stroke), rgba(stroke_color), shadow
        self.line_spacing, self.upper = line_spacing, upper
        self._cache: dict = {}

    def text_at(self, t):
        return self.text

    def patch(self, t):
        text = self.text_at(t)
        if text not in self._cache:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[text] = self._make(text.upper() if self.upper else text)
        return self._cache[text]

    def _make(self, text):
        font = _font(round(self.size))
        lines = _wrap(text, font, self.max_width)
        step = round(self.size * self.line_spacing)
        boxes = [font.getbbox(line or " ", anchor="ls", stroke_width=self.stroke) for line in lines]
        pad = self.stroke + round(self.shadow * 2) + 2
        left = min(b[0] for b in boxes)
        right = max(b[2] for b in boxes)
        top = min(b[1] + i * step for i, b in enumerate(boxes))
        bottom = max(b[3] + i * step for i, b in enumerate(boxes))
        w, h = right - left + 2 * pad, bottom - top + 2 * pad
        patch = PILImage.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
        d = ImageDraw.Draw(patch)
        ax = {"left": pad - left, "center": w / 2, "right": w - pad}[self.align]
        anchor = {"left": "ls", "center": "ms", "right": "rs"}[self.align]
        if self.shadow:
            sh = PILImage.new("RGBA", patch.size, (0, 0, 0, 0))
            sd = ImageDraw.Draw(sh)
            for i, line in enumerate(lines):
                sd.text((ax + self.shadow, pad - top + i * step + self.shadow), line, font=font, fill=(0, 0, 0, 170), anchor=anchor)
            patch.alpha_composite(sh.filter(ImageFilter.GaussianBlur(self.shadow)))
        for i, line in enumerate(lines):
            d.text((ax, pad - top + i * step), line, font=font, fill=self.color, anchor=anchor,
                   stroke_width=self.stroke, stroke_fill=self.stroke_color)
        return patch


class Counter(Text):
    """A number that counts from `start` to `end` between `at` and `at + dur` seconds.
    Counter(0, 78, at=0.5, dur=1.5, suffix="%", size=u(240), y=..., decimals=0, compact_numbers=False)"""

    def __init__(self, start=0, end=100, at=0.0, dur=1.5, decimals=0, prefix="", suffix="", use_commas=True,
                 compact_numbers=False, ease=ease_out_cubic, **kw):
        super().__init__("", **kw)
        self.start, self.end, self.at, self.dur, self.ease = start, end, at, dur, ease
        self.decimals, self.prefix, self.suffix = decimals, prefix, suffix
        self.use_commas, self.compact_numbers = use_commas, compact_numbers

    def number_at(self, t):
        k = clamp((t - self.at) / self.dur) if self.dur > 0 else 1.0
        return lerp(self.start, self.end, self.ease(k))

    def text_at(self, t):
        n = self.number_at(t)
        body = compact(n) if self.compact_numbers else (commas(n, self.decimals) if self.use_commas else f"{n:.{self.decimals}f}")
        return f"{self.prefix}{body}{self.suffix}"


class Bar(Element):
    """Horizontal (or vertical, direction='v') value bar. `value` is 0..1 of the track; bar.grow(at, dur)
    animates it filling up from 0."""

    def __init__(self, x=0, y=0, w=600, h=40, value=1.0, color=None, track_color=None, radius=None, direction="h", **kw):
        super().__init__(x, y, **kw)
        self._base.update(value=float(value))
        self.w, self.h, self.direction = w, h, direction
        self.color = rgba(color if color is not None else _pal().accent)
        self.track_color = rgba(track_color if track_color is not None else _pal().card)
        self.radius = min(h, w) / 2 if radius is None else radius

    def grow(self, at=0.0, dur=1.0, ease=ease_out_cubic):
        return self.animate("value", self._base["value"], at, dur, frm=0.0, ease=ease)

    def patch(self, t):
        v = clamp(self.value("value", t))
        w, h, r = round(self.w), round(self.h), self.radius

        def draw(d, k):
            d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=r * k, fill=self.track_color)
            if v > 0.001:
                if self.direction == "h":
                    fw = max(w * v, min(2 * r, w))
                    d.rounded_rectangle((0, 0, fw * k - 1, h * k - 1), radius=r * k, fill=self.color)
                else:
                    fh = max(h * v, min(2 * r, h))
                    d.rounded_rectangle((0, (h - fh) * k, w * k - 1, h * k - 1), radius=r * k, fill=self.color)

        return _supersampled(w, h, draw)


class ProgressRing(Element):
    """Circular progress ring of `radius` px and `thickness`; `value` 0..1 sweeps clockwise from the top."""

    def __init__(self, x=0, y=0, radius=150, thickness=28, value=1.0, color=None, track_color=None, **kw):
        super().__init__(x, y, **kw)
        self._base.update(value=float(value))
        self.radius, self.thickness = radius, thickness
        self.color = rgba(color if color is not None else _pal().accent)
        self.track_color = rgba(track_color if track_color is not None else _pal().card)

    def grow(self, at=0.0, dur=1.2, ease=ease_out_cubic):
        return self.animate("value", self._base["value"], at, dur, frm=0.0, ease=ease)

    def patch(self, t):
        v = clamp(self.value("value", t))
        size = round(2 * self.radius + 4)
        th = self.thickness

        def draw(d, k):
            box = (2 * k, 2 * k, (size - 2) * k, (size - 2) * k)
            d.ellipse(box, outline=self.track_color, width=round(th * k))
            if v > 0.002:
                d.arc(box, -90, -90 + 360 * v, fill=self.color, width=round(th * k))

        return _supersampled(size, size, draw)


class Line(Element):
    """Straight line from (x1, y1) to (x2, y2); line.draw_on(at, dur) animates it being drawn."""

    def __init__(self, x1, y1, x2, y2, color=None, width=6, **kw):
        super().__init__(0, 0, anchor="topleft", **kw)
        self._base.update(progress=1.0)
        self.p1, self.p2, self.width = (x1, y1), (x2, y2), width
        self.color = rgba(color if color is not None else _pal().fg)
        self._origin = (0.0, 0.0)

    def draw_on(self, at=0.0, dur=0.8, ease=ease_in_out_cubic):
        return self.animate("progress", 1.0, at, dur, frm=0.0, ease=ease)

    def patch(self, t):
        k = clamp(self.value("progress", t))
        if k <= 0:
            return None
        (x1, y1), (x2, y2) = self.p1, self.p2
        ex, ey = x1 + (x2 - x1) * k, y1 + (y2 - y1) * k
        pad = self.width + 2
        left, top = min(x1, ex) - pad, min(y1, ey) - pad
        w, h = abs(ex - x1) + 2 * pad, abs(ey - y1) + 2 * pad
        r = self.width / 2

        def draw(d, s):
            pts = [((x1 - left) * s, (y1 - top) * s), ((ex - left) * s, (ey - top) * s)]
            d.line(pts, fill=self.color, width=round(self.width * s))
            for px, py in pts:
                d.ellipse((px - r * s, py - r * s, px + r * s, py + r * s), fill=self.color)

        self._origin = (left, top)
        return _supersampled(w, h, draw)

    def render(self, canvas, t):  # position comes from the geometry; x/y only offset it
        opacity = clamp(self.value("opacity", t))
        if not self.visible(t) or opacity <= 0.004:
            return
        patch = self.patch(t)
        if patch is not None:
            _blit(canvas, patch, self._origin[0] + self.value("x", t), self._origin[1] + self.value("y", t), "topleft", 1.0, opacity)


class Image(Element):
    """A picture from the scene's asset list (only names the brief says exist), fitted into w x h."""

    def __init__(self, asset, x=0, y=0, w=400, h=400, fit="cover", radius=0, **kw):
        super().__init__(x, y, **kw)
        if asset not in ASSETS:
            raise ValueError(f"Image asset '{asset}' not found; available: {sorted(ASSETS) or 'none'}")
        src = PILImage.open(ASSETS[asset]).convert("RGBA")
        w, h = round(w), round(h)
        ratio = max(w / src.width, h / src.height) if fit == "cover" else min(w / src.width, h / src.height)
        fitted = src.resize((max(1, round(src.width * ratio)), max(1, round(src.height * ratio))), PILImage.LANCZOS)
        patch = PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
        patch.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
        if radius:
            mask = _supersampled(w, h, lambda d, k: d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=radius * k, fill=(255, 255, 255, 255)))
            patch.putalpha(PILImage.composite(patch.getchannel("A"), PILImage.new("L", (w, h), 0), mask.getchannel("A")))
        self._patch = patch

    def patch(self, t):
        return self._patch


# --- scene ---


class Scene:
    """Holds the elements and renders frames. Create exactly one, named `scene`."""

    def __init__(self, palette="midnight", **_ignored):
        global _current_palette
        self.W, self.H, self.fps, self.duration = W, H, FPS, DURATION
        self.palette = palette if isinstance(palette, Palette) else PALETTES.get(palette, PALETTES["midnight"])
        _current_palette = self.palette
        self.safe = safe_box(self.W, self.H)
        self.box = Box(0, 0, self.W, self.H)
        self.elements: list[Element] = []
        self._bg_colors = (self.palette.bg, self.palette.bg2)
        self._glows: list = []
        self._bg_cache = None
        self._fade = (0.0, 0.0)

    def background(self, color=None, color2=None):
        """Solid colour, or a top-to-bottom gradient when color2 is given."""
        self._bg_colors = (color or self.palette.bg, color2 or color or self.palette.bg)
        self._bg_cache = None
        return self

    def glow(self, x, y, radius, color=None, opacity=0.35):
        """Soft radial light baked into the background."""
        self._glows.append((x, y, radius, rgba(color if color is not None else self.palette.accent, opacity)))
        self._bg_cache = None
        return self

    def add(self, *elements):
        self.elements.extend(elements)
        return elements[0] if len(elements) == 1 else elements

    def fade_in(self, dur=0.4):
        self._fade = (dur, self._fade[1])
        return self

    def fade_out(self, dur=0.5):
        self._fade = (self._fade[0], dur)
        return self

    def _background(self):
        if self._bg_cache is None:
            c1, c2 = rgba(self._bg_colors[0]), rgba(self._bg_colors[1])
            if c1 == c2:
                bg = PILImage.new("RGBA", (self.W, self.H), c1)
            else:
                ramp = PILImage.linear_gradient("L").resize((1, self.H), PILImage.BILINEAR)
                bg = PILImage.composite(PILImage.new("RGBA", (1, self.H), c2), PILImage.new("RGBA", (1, self.H), c1), ramp).resize((self.W, self.H))
            for x, y, r, color in self._glows:
                size = max(2, round(2 * r))
                mask = PILImage.radial_gradient("L").resize((size, size), PILImage.BILINEAR)
                layer = PILImage.new("RGBA", (size, size), color[:3] + (255,))
                # radial_gradient reaches 181 at the edge midpoints; fall off smoothly to 0 there
                layer.putalpha(mask.point([round(color[3] * max(0.0, 1 - v / 181) ** 2) for v in range(256)]))
                piece, dest = _clip_to(layer, round(x - r), round(y - r), self.W, self.H)
                bg.alpha_composite(piece, dest)
            self._bg_cache = bg
        return self._bg_cache

    def render_frame(self, t):
        canvas = self._background().copy()
        for el in sorted(self.elements, key=lambda e: e.layer):
            el.render(canvas, t)
        frame = canvas.convert("RGB")
        fade_in, fade_out = self._fade
        k = 1.0
        if fade_in > 0:
            k = min(k, clamp(t / fade_in))
        if fade_out > 0:
            k = min(k, clamp((self.duration - t) / fade_out))
        if k < 1.0:
            frame = PILImage.blend(frame, PILImage.new("RGB", frame.size, rgba(self._bg_colors[0])[:3]), 1 - k)
        return frame


__all__ = [
    "Scene", "Text", "Counter", "Rect", "RoundedRect", "Circle", "Bar", "ProgressRing", "Line", "Image",
    "Box", "grid", "Palette", "PALETTES", "rgba", "u", "clamp", "lerp", "remap", "stagger", "commas", "compact",
    "ease_linear", "ease_in_quad", "ease_out_quad", "ease_in_out_quad", "ease_out_cubic", "ease_in_out_cubic",
    "ease_out_expo", "ease_in_out_sine", "ease_out_back", "ease_out_elastic",
    "W", "H", "FPS", "DURATION", "math", "random",
]

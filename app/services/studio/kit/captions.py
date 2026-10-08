"""Caption styling and writers: an ASS file for libass burn-in, plus a Pillow PNG-overlay fallback for
FFmpeg builds without the `ass`/`subtitles` filter.

Input is the sentence timings from kit.tts: [{text, start, end, words: [{text, start, end}]}].
Styles: `bold-pop` (default; 1-3 uppercase words at a time, the spoken word pops in the accent colour),
`clean` (whole phrase on a dark box, no highlight), `karaoke` (phrase with a sweeping colour fill).
The 9:16 safe area keeps bottom captions above the Reels/Shorts UI (about 17% of the height).
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"
FONT_FILE = FONTS_DIR / "Anton-Regular.ttf"
FONT_FAMILY = "Anton"
STYLES = ("bold-pop", "clean", "karaoke")
POSITIONS = ("bottom", "center", "top")
ACCENT = (255, 212, 0)  # gold
_MAX_WORDS = {"bold-pop": 3, "karaoke": 5, "clean": 8}


@dataclass
class Chunk:
    words: list[dict]
    lines: list[list[int]]  # word indexes per rendered line
    start: float
    end: float


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_FILE), size)


def font_size(size: tuple[int, int], style: str) -> int:
    return round(min(size) * (0.078 if style == "bold-pop" else 0.062))


def _margins(size: tuple[int, int], position: str) -> tuple[int, int]:
    """(side margin, vertical margin) in px; bottom keeps clear of the platform UI on tall frames."""
    w, h = size
    tall = h > w
    if position == "bottom":
        return round(w * 0.07), round(h * (0.17 if tall else 0.09))
    if position == "top":
        return round(w * 0.07), round(h * (0.12 if tall else 0.07))
    return round(w * 0.07), 0


def build_chunks(sentences: list[dict], style: str, size: tuple[int, int]) -> list[Chunk]:
    """Group words into on-screen phrases (never across sentences) and wrap each into at most 2 lines."""
    f = font(font_size(size, style))
    max_px = size[0] - 2 * _margins(size, "bottom")[0]
    upper = style == "bold-pop"
    chunks: list[Chunk] = []
    for sent in sentences:
        words = sent["words"] or [{"text": sent["text"], "start": sent["start"], "end": sent["end"]}]
        step = _MAX_WORDS[style]
        for i in range(0, len(words), step):
            group = words[i : i + step]
            lines = _wrap([w["text"].upper() if upper else w["text"] for w in group], f, max_px)
            chunks.append(Chunk(group, lines, group[0]["start"], group[-1]["end"]))
    # Hold each phrase until the next one starts (up to 0.4 s) so captions never flash off between words.
    for cur, nxt in zip(chunks, chunks[1:]):
        gap = nxt.start - cur.end
        if gap > 0:
            cur.end += min(gap, 0.4)
    return chunks


def _wrap(texts: list[str], f: ImageFont.FreeTypeFont, max_px: int) -> list[list[int]]:
    lines: list[list[int]] = [[]]
    for i, t in enumerate(texts):
        trial = " ".join(texts[j] for j in [*lines[-1], i])
        if lines[-1] and f.getlength(trial) > max_px:
            lines.append([])
        lines[-1].append(i)
    return lines


def _ass_color(rgb: tuple[int, int, int], alpha: int = 0) -> str:
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _ts(seconds: float) -> str:
    cs = round(max(seconds, 0) * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def write_ass(
    path: Path, sentences: list[dict], *, style: str = "bold-pop", size: tuple[int, int] = (1080, 1920), position: str = "bottom",
) -> Path:
    if style not in STYLES:
        raise ValueError(f"Unknown caption style '{style}'.")
    w, h = size
    side, vmargin = _margins(size, position)
    align = {"bottom": 2, "center": 5, "top": 8}[position]
    fs = font_size(size, style)
    box = style == "clean"
    outline = round(fs * (0.12 if box else 0.09))
    # Karaoke fills Secondary -> Primary, so Primary carries the highlight colour there.
    primary = ACCENT if style == "karaoke" else (255, 255, 255)
    secondary = (255, 255, 255)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{FONT_FAMILY},{fs},{_ass_color(primary)},{_ass_color(secondary)},"
        f"{_ass_color((0, 0, 0), 0x40 if box else 0)},{_ass_color((0, 0, 0), 0x80)},0,0,0,0,100,100,0,0,"
        f"{3 if box else 1},{outline},{0 if box else 2},{align},{side},{side},{vmargin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for chunk in build_chunks(sentences, style, size):
        texts = [_escape(wd["text"].upper() if style == "bold-pop" else wd["text"]) for wd in chunk.words]
        if style == "bold-pop":
            for k, wd in enumerate(chunk.words):
                end = chunk.words[k + 1]["start"] if k + 1 < len(chunk.words) else chunk.end
                body = _join_lines(chunk.lines, texts, active=k)
                lines.append(_event(wd["start"], max(end, wd["start"] + 0.05), body))
        elif style == "karaoke":
            parts = []
            for k, wd in enumerate(chunk.words):
                nxt = chunk.words[k + 1]["start"] if k + 1 < len(chunk.words) else chunk.end
                parts.append(f"{{\\kf{max(round((nxt - wd['start']) * 100), 1)}}}{texts[k]}")
            lines.append(_event(chunk.start, chunk.end, _join_lines(chunk.lines, parts)))
        else:
            lines.append(_event(chunk.start, chunk.end, _join_lines(chunk.lines, texts)))
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _join_lines(lines: list[list[int]], texts: list[str], active: int | None = None) -> str:
    hi = f"{{\\c{_ass_color(ACCENT)}&\\fscx118\\fscy118}}"
    out = []
    for line in lines:
        out.append(" ".join(f"{hi}{texts[i]}{{\\r}}" if i == active else texts[i] for i in line))
    return "\\N".join(out)


def _event(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{text}\n"


# --- Pillow fallback ---


def render_overlays(
    outdir: Path, sentences: list[dict], *, style: str = "bold-pop", size: tuple[int, int] = (1080, 1920), position: str = "bottom",
) -> list[tuple[Path, float, float, int, int]]:
    """One transparent PNG per phrase (no per-word highlight) as `(png, start, end, x, y)` for ffmpeg overlay."""
    outdir.mkdir(parents=True, exist_ok=True)
    w, h = size
    fs = font_size(size, style)
    f = font(fs)
    stroke = round(fs * 0.09)
    _side, vmargin = _margins(size, position)
    items = []
    for n, chunk in enumerate(build_chunks(sentences, style, size)):
        upper = style == "bold-pop"
        rows = [" ".join(chunk.words[i]["text"].upper() if upper else chunk.words[i]["text"] for i in line) for line in chunk.lines]
        line_h = round(fs * 1.15)
        pw = round(max(f.getlength(r) for r in rows)) + 2 * (stroke + 12)
        ph = line_h * len(rows) + 2 * (stroke + 8)
        img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        if style == "clean":
            draw.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=fs // 3, fill=(0, 0, 0, 170))
        for r, row in enumerate(rows):
            draw.text(
                (pw / 2, stroke + 8 + r * line_h), row, font=f, fill=(255, 255, 255, 255), anchor="mt",
                stroke_width=0 if style == "clean" else stroke, stroke_fill=(0, 0, 0, 255),
            )
        x = (w - pw) // 2
        y = {"bottom": h - vmargin - ph, "top": vmargin, "center": (h - ph) // 2}[position]
        png = outdir / f"cap_{n:04d}.png"
        img.save(png)
        items.append((png, chunk.start, chunk.end, x, y))
    return items

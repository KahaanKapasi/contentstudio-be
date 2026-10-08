"""FFmpeg helpers for the Video Studio engines. FFmpeg only (no MoviePy/Remotion/Node, see docs/CLAUDE.md).

Everything runs through `run()` so there is one place that handles timeouts and error text. The system
`ffmpeg` is preferred; otherwise the static binary bundled by imageio-ffmpeg is used. ffprobe is not
assumed to exist, so `probe()` parses `ffmpeg -i` output.
"""

import functools
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FPS = 30
TARGETS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
DEFAULT_TIMEOUT_S = 600
# Uniform encode settings: every intermediate and final clip uses these so the concat demuxer can stream-copy.
VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]


class FFmpegError(RuntimeError):
    """An ffmpeg invocation failed; the message ends with the tail of its stderr."""


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only when neither is installed
        raise FFmpegError("FFmpeg is not installed (install it or `pip install imageio-ffmpeg`).") from exc


def available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except FFmpegError:
        return False


@functools.lru_cache(maxsize=1)
def _filters() -> str:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    return out.stdout


def has_filter(name: str) -> bool:
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", _filters(), re.MULTILINE) is not None


def target_size(aspect: str) -> tuple[int, int]:
    if aspect not in TARGETS:
        raise ValueError(f"Unsupported aspect ratio '{aspect}'.")
    return TARGETS[aspect]


def run(args: list[str], *, timeout: float = DEFAULT_TIMEOUT_S, cwd: Path | None = None) -> None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"FFmpeg timed out after {int(timeout)}s.") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"FFmpeg failed: {(proc.stderr or '').strip()[-600:]}")


@dataclass
class Probe:
    duration: float
    width: int | None
    height: int | None
    has_audio: bool


def probe(path: Path | str) -> Probe:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60)
    text = proc.stderr
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not dur:
        raise FFmpegError(f"Could not read {Path(path).name} (not a media file?).")
    seconds = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    size = re.search(r"Stream #\S+.*Video:.*?[, ](\d{2,5})x(\d{2,5})", text)
    return Probe(seconds, int(size.group(1)) if size else None, int(size.group(2)) if size else None, "Audio:" in text)


def duration(path: Path | str) -> float:
    return probe(path).duration


# --- scaling ---


def cover_filter(w: int, h: int) -> str:
    """Scale to fill WxH then centre-crop."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


def contain_filter(w: int, h: int, color: str = "black") -> str:
    """Scale to fit inside WxH and pad."""
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={color},setsar=1"


def normalize_clip(
    src: Path, dst: Path, w: int, h: int, seconds: float, *, start: float = 0.0, loop: bool = True,
    mode: str = "cover", extra_filters: str = "",
) -> Path:
    """Trim `seconds` from `src` (looping it if shorter and `loop`), scale to WxH, 30 fps, no audio."""
    scale = cover_filter(w, h) if mode == "cover" else contain_filter(w, h)
    vf = scale + (f",{extra_filters}" if extra_filters else "")
    args: list = []
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-ss", f"{start:.3f}", "-i", src, "-t", f"{seconds:.3f}", "-an", "-vf", vf, *VIDEO_ARGS, dst]
    run(args)
    return dst


def concat(clips: list[Path], dst: Path) -> Path:
    """Concat demuxer (stream copy). Clips must share codec/size/fps, e.g. all from normalize_clip."""
    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{str(c).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for c in clips))
    try:
        run(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", dst])
    finally:
        listing.unlink(missing_ok=True)
    return dst


def xfade_chain(clips: list[Path], seconds: list[float], dst: Path, *, transition: str = "fade", td: float = 0.5) -> Path:
    """Cross-fade clips (all the same size/fps). `seconds[i]` is clip i's length."""
    if len(clips) == 1:
        shutil.copyfile(clips[0], dst)
        return dst
    args: list = []
    for c in clips:
        args += ["-i", c]
    parts, prev, acc = [], "[0:v]", seconds[0]
    for i in range(1, len(clips)):
        out = f"[x{i}]" if i < len(clips) - 1 else "[v]"
        parts.append(f"{prev}[{i}:v]xfade=transition={transition}:duration={td}:offset={max(acc - td, 0):.3f}{out}")
        prev, acc = out, acc + seconds[i] - td
    run([*args, "-filter_complex", ";".join(parts), "-map", "[v]", "-an", *VIDEO_ARGS, dst])
    return dst


def loop_to_duration(src: Path, dst: Path, seconds: float) -> Path:
    """Repeat/trim a video to exactly `seconds` (re-encoded so loop seams are clean)."""
    run(["-stream_loop", "-1", "-i", src, "-t", f"{seconds:.3f}", "-an", *VIDEO_ARGS, dst])
    return dst


# --- audio ---


def mix_audio(voice: Path | None, bgm: Path | None, dst: Path, seconds: float, *, bgm_volume: float = 0.2, fade_out: float = 3.0) -> Path:
    """Voice at 1.0 plus looped background music at `bgm_volume`, faded out over the last `fade_out` s."""
    args: list = []
    if voice:
        args += ["-i", voice]
    if bgm:
        args += ["-stream_loop", "-1", "-i", bgm]
    bgm_idx = 1 if voice else 0
    fade_start = max(seconds - fade_out, 0)
    chains = []
    if voice and bgm:
        chains.append(f"[0:a]apad,atrim=0:{seconds:.3f},volume=1.0[v]")
        chains.append(f"[{bgm_idx}:a]atrim=0:{seconds:.3f},volume={bgm_volume},afade=t=out:st={fade_start:.3f}:d={fade_out}[b]")
        chains.append("[v][b]amix=inputs=2:duration=first:normalize=0[a]")
    elif voice:
        chains.append(f"[0:a]apad,atrim=0:{seconds:.3f}[a]")
    elif bgm:
        chains.append(f"[0:a]atrim=0:{seconds:.3f},volume={bgm_volume},afade=t=out:st={fade_start:.3f}:d={fade_out}[a]")
    else:
        raise ValueError("mix_audio needs a voice or a bgm track.")
    run([*args, "-filter_complex", ";".join(chains), "-map", "[a]", "-ar", "44100", "-ac", "2", dst])
    return dst


# --- subtitles / final encode ---


def _filter_escape(value: str) -> str:
    """Escape a path for use inside a filtergraph option value."""
    return re.sub(r"([\\:',\[\]])", r"\\\1", value)


def subtitle_filter_name() -> str | None:
    for name in ("ass", "subtitles"):
        if has_filter(name):
            return name
    return None


def finalize(
    video: Path, dst: Path, *, audio: Path | None = None, ass: Path | None = None, fonts_dir: Path | None = None,
    overlays: list[tuple[Path, float, float, int, int]] | None = None, seconds: float | None = None,
) -> Path:
    """Final encode: optional audio mux, ASS burn-in, or PNG overlays (`(png, start, end, x, y)`), then
    libx264/yuv420p/30fps + AAC 192k + faststart. Video is always re-encoded so the result is uniform."""
    args: list = ["-i", video]
    if audio:
        args += ["-i", audio]
    n_overlay_inputs = 0
    for png, *_ in overlays or []:
        args += ["-i", png]
        n_overlay_inputs += 1
    first_overlay = 2 if audio else 1

    cwd = None
    filters: list[str] = []
    last = "[0:v]"
    if ass:
        name = subtitle_filter_name()
        if not name:
            raise FFmpegError("This FFmpeg build has no ass/subtitles filter.")
        cwd = ass.parent
        opt = f"{name}=filename={_filter_escape(ass.name)}"
        if fonts_dir:
            opt += f":fontsdir={_filter_escape(str(fonts_dir))}"
        filters.append(f"{last}{opt}[s]")
        last = "[s]"
    for i, (_png, start, end, x, y) in enumerate(overlays or []):
        out = f"[o{i}]"
        filters.append(
            f"{last}[{first_overlay + i}:v]overlay={x}:{y}:enable='between(t,{start:.3f},{end:.3f})':eof_action=repeat{out}"
        )
        last = out

    out_args: list = []
    if filters:
        out_args += ["-filter_complex", ";".join(filters), "-map", last]
    else:
        out_args += ["-map", "0:v"]
    if audio:
        out_args += ["-map", "1:a", *AUDIO_ARGS]
    else:
        out_args += ["-an"]
    if seconds:
        out_args += ["-t", f"{seconds:.3f}"]
    elif audio:
        out_args += ["-shortest"]
    run([*args, *out_args, *VIDEO_ARGS, "-movflags", "+faststart", dst], cwd=cwd)
    return dst


# --- stylise filters (used by the recipe engines) ---

STYLE_FILTERS = {
    # low-res nearest-neighbour upscale, posterise, 15 fps: PlayStation-1 look
    "ps1": "fps=15,scale=iw/4:ih/4:flags=neighbor,scale=iw*4:ih*4:flags=neighbor,"
    "curves=all='0/0 0.25/0.2 0.5/0.5 0.75/0.8 1/1',eq=saturation=1.25:contrast=1.1,noise=alls=12:allf=t",
    # on-twos 12 fps, punchy contrast, small chromatic offset: spider-verse comic look
    "spiderverse": "fps=12,eq=saturation=1.5:contrast=1.2,unsharp=5:5:1.0,rgbashift=rh=-4:bh=4,noise=alls=8:allf=t",
}


def stylise_filter(name: str) -> str:
    if name not in STYLE_FILTERS:
        raise ValueError(f"Unknown style '{name}'.")
    return STYLE_FILTERS[name]

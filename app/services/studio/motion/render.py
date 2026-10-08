"""Run validated scene code in the sandbox child process (sandbox.py) and pipe its frames into ffmpeg.

`render_scene` starts ffmpeg reading rawvideo on stdin, starts the child with its stdout wired directly to
ffmpeg's stdin (no copying through Python), enforces a wall-clock timeout and a memory ceiling, and turns a
child failure into MotionRenderError carrying the user-code error text.
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from app.services.studio.kit import ffmpeg

SANDBOX = Path(__file__).resolve().parent / "sandbox.py"
RENDER_TIMEOUT_S = 120
MAX_RSS_MB = 2500


class MotionRenderError(RuntimeError):
    """User code failed at runtime (message is a short error with a line number) or the render timed out."""


def render_scene(
    code: str, dest: Path, *, width: int, height: int, fps: int, duration: float, assets: dict | None = None,
    timeout: float = RENDER_TIMEOUT_S, preflight_only: bool = False, progress=None,
) -> Path | None:
    """Render `code` to a silent mp4 at `dest` (or only run the preflight frames when `preflight_only`)."""
    with tempfile.TemporaryDirectory(prefix="motion_") as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "scene.py").write_text(code)
        cfg = {"width": width, "height": height, "fps": fps, "duration": duration, "assets": assets or {}, "preflight_only": preflight_only}
        (tmp_dir / "config.json").write_text(json.dumps(cfg))
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "OMP_NUM_THREADS": "1"}
        cmd = [sys.executable, "-I", str(SANDBOX), str(tmp_dir / "scene.py"), str(tmp_dir / "config.json")]

        if preflight_only:
            return _run_child(cmd, env, None, timeout, progress=None, preflight=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        enc = subprocess.Popen(
            [
                ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest),
            ],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            try:
                _run_child(cmd, env, enc, timeout, progress=progress, expected_frames=round(duration * fps))
            except MotionRenderError:
                if enc.poll() not in (None, 0):  # the child only saw a broken pipe: report ffmpeg's side
                    raise MotionRenderError(f"FFmpeg could not encode the frames: {(enc.stderr.read() or b'').decode()[-300:]}") from None
                raise
            if enc.wait(timeout=90) != 0:
                raise MotionRenderError(f"FFmpeg could not encode the frames: {(enc.stderr.read() or b'').decode()[-300:]}")
        except BaseException:
            enc.kill()
            enc.wait()
            dest.unlink(missing_ok=True)
            raise
        return dest


def _run_child(cmd, env, enc, timeout, *, progress, expected_frames=0, preflight=False):
    child = subprocess.Popen(cmd, env=env, stdout=enc.stdin if enc else subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=tempfile.gettempdir())
    if enc:
        enc.stdin.close()  # the child holds the only remaining writer, so ffmpeg sees EOF when it exits
    errors: list[str] = []

    def drain():
        for line in child.stderr:
            if line.startswith("PROGRESS ") and progress and expected_frames:
                progress(min(int(line.split()[1]) / expected_frames, 1.0))
            else:
                errors.append(line)
                del errors[:-40]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    start, last_mem = time.monotonic(), 0.0
    while child.poll() is None:
        now = time.monotonic()
        if now - start > timeout:
            child.kill()
            child.wait()
            raise MotionRenderError(f"The scene took longer than {int(timeout)} s to render. Simplify it (fewer elements or shorter).")
        if now - last_mem > 2 and _rss_mb(child.pid) > MAX_RSS_MB:
            child.kill()
            child.wait()
            raise MotionRenderError("The scene used too much memory. Use fewer or smaller elements.")
        if now - last_mem > 2:
            last_mem = now
        time.sleep(0.1)
    reader.join(timeout=5)
    if child.returncode != 0:
        text = "".join(errors).strip()
        raise MotionRenderError(text[-1200:] or f"The scene process exited with code {child.returncode}.")


def _rss_mb(pid: int) -> float:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) / 1024 if out else 0.0
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0



"""Shared builders for the Video Studio tests: tiny real media made with ffmpeg lavfi sources."""

import subprocess

import pytest

from app.services.studio.kit import ffmpeg

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg is not installed")


def make_test_video(path, seconds=2.0, size="160x284", rate=30):
    subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return path


def make_wav_bytes(seconds=0.5):
    out = subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    )
    return out.stdout


def fake_edge_fetch(text, voice):
    """Stands in for edge-tts: 0.3 s of tone per word plus word boundaries."""
    words = text.split()
    boundaries = [(w, i * 0.3, (i + 1) * 0.3) for i, w in enumerate(words)]
    return make_wav_bytes(0.3 * len(words)), boundaries

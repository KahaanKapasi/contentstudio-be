"""Gemini writes the scene code; we validate it, preflight-render it, and give Gemini one chance to fix errors."""

import re
from pathlib import Path

from app.services import gemini_client
from app.services.studio.motion import prompt, render, validator

MAX_ATTEMPTS = 2  # first try + one fix


class SceneCodeError(RuntimeError):
    """Gemini could not produce a working scene; the message is safe to show the user."""


def extract_code(text: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()


def write_scene(base_prompt: str, *, width: int, height: int, fps: int, duration: float, assets: dict, log=None) -> str:
    """Returns validated, preflight-clean scene code. `log(msg)` receives short progress notes."""
    preview = dict(width=max(width // 4, 16), height=max(height // 4, 16), fps=fps, duration=duration, assets=assets)
    current_prompt, code, error = base_prompt, "", ""
    for attempt in range(MAX_ATTEMPTS):
        code = extract_code(gemini_client.generate_text(current_prompt))
        try:
            validator.validate(code)
            render.render_scene(code, Path("unused.mp4"), preflight_only=True, **preview)
            return code
        except (validator.MotionCodeError, render.MotionRenderError) as exc:
            error = str(exc)
            if log:
                log(f"attempt {attempt + 1} failed: {error[:200]}")
            current_prompt = prompt.fix_prompt(base_prompt, code, error)
    raise SceneCodeError(f"The generated scene had a problem even after one automatic fix: {error[:300]}")

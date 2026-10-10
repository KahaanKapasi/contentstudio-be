"""Per-project usage accumulator for Video Studio.

The runner activates one `ProjectUsage` around every stage (`with use(ctx.usage):`). Low-level calls report into
whichever accumulator is active: `gemini_client.record_usage` (text / image / TTS responses) and
`kit.clips.generate_clip` (paid clip seconds). It is plain counters, so it serialises into project assets and
survives a resume; `cost()` turns the counters into dollars at log time using the price table.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

from app.config import settings
from app.services.costs import prices

_current: ContextVar["ProjectUsage | None"] = ContextVar("studio_project_usage", default=None)

_TEXT = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "grounded_requests": 0}
_IMAGE = {"calls": 0, "images": 0, "prompt_tokens": 0, "output_tokens": 0, "token_calls": 0, "untokened_images": 0}
_TTS = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "estimated_output_tokens": 0}
TTS_TOKENS_PER_SECOND = 25  # Gemini audio output


def current() -> "ProjectUsage | None":
    return _current.get()


@contextmanager
def use(usage: "ProjectUsage"):
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


class ProjectUsage:
    def __init__(self, data: dict | None = None):
        data = data if isinstance(data, dict) else {}
        self.text = {**_TEXT, **(data.get("text") or {})}
        self.image = {**_IMAGE, **(data.get("image") or {})}
        self.tts = {**_TTS, **(data.get("tts") or {})}
        self.clips: list[dict] = list(data.get("clips") or [])

    def to_dict(self) -> dict:
        return {"text": self.text, "image": self.image, "tts": self.tts, "clips": self.clips}

    # --- recording (all of these are called from failure-safe wrappers) ---

    def add_text(self, prompt: int, output: int, thinking: int, grounded: bool) -> None:
        t = self.text
        t["calls"] += 1
        t["prompt_tokens"] += prompt
        t["output_tokens"] += output
        t["thinking_tokens"] += thinking
        t["grounded_requests"] += 1 if grounded else 0

    def add_image(self, prompt: int, output: int, produced: int) -> None:
        """`output` = image output tokens from usage_metadata (0 when absent); `produced` = images actually returned."""
        i = self.image
        i["calls"] += 1
        i["images"] += produced
        i["prompt_tokens"] += prompt
        i["output_tokens"] += output
        if output > 0:
            i["token_calls"] += 1
        else:
            i["untokened_images"] += produced  # priced per image instead

    def add_tts(self, prompt: int, output: int, estimated_output: int = 0) -> None:
        t = self.tts
        t["calls"] += 1
        t["prompt_tokens"] += prompt
        t["output_tokens"] += output
        t["estimated_output_tokens"] += 0 if output else estimated_output

    def add_clip(self, provider: str, model: str, resolution: str, seconds: float, usd: float | None = None) -> None:
        """One finished paid clip. `usd=None` means the provider/model has no published price: the midpoint of the
        provider's default range is used and the item is marked `estimate`."""
        estimate = usd is None
        if estimate:
            try:
                lo, hi = prices.bounds(f"{provider}.default")
            except KeyError:
                lo = hi = 0.0
            usd = (lo + hi) / 2 * seconds
        self.clips.append(
            {"provider": provider, "model": model, "resolution": resolution, "seconds": seconds, "usd": round(float(usd), 6), "estimate": estimate}
        )

    # --- pricing ---

    def cost(self, on: date | str | None = None) -> tuple[float, dict]:
        """(total_usd, breakdown). The breakdown is JSON-safe and goes into the CostEvent details."""
        p_in, p_out = prices.usd("gemini.flash.text.in", on), prices.usd("gemini.flash.text.out", on)
        t = self.text
        text_usd = (t["prompt_tokens"] * p_in + (t["output_tokens"] + t["thinking_tokens"]) * p_out) / 1e6
        grounding_usd = t["grounded_requests"] * prices.usd("gemini.grounding", on)

        i = self.image
        image_usd = (
            i["output_tokens"] * prices.usd("gemini.image.out.tokens", on) + i["prompt_tokens"] * prices.usd("gemini.image.in", on)
        ) / 1e6 + i["untokened_images"] * prices.usd("gemini.image.out", on)

        family = "2_5" if "2.5" in (settings.gemini_tts_model or "") else "3_8"
        g = self.tts
        tts_usd = (
            g["prompt_tokens"] * prices.usd(f"gemini.tts.{family}.in", on)
            + (g["output_tokens"] + g["estimated_output_tokens"]) * prices.usd(f"gemini.tts.{family}.out", on)
        ) / 1e6

        clip_usd = sum(c["usd"] for c in self.clips)
        clip_estimated = sum(c["usd"] for c in self.clips if c.get("estimate"))
        # Unmetered fallbacks (priced per image / from audio length, not from usage_metadata) are estimates too.
        estimated = clip_estimated
        total = text_usd + grounding_usd + image_usd + tts_usd + clip_usd
        breakdown = {
            "gemini_text": {**t, "usd": round(text_usd, 6)},
            "grounding": {"requests": t["grounded_requests"], "usd": round(grounding_usd, 6)},
            "gemini_image": {**i, "usd": round(image_usd, 6)},
            "gemini_tts": {**g, "usd": round(tts_usd, 6)},
            "clips": {
                "count": len(self.clips), "seconds": round(sum(c["seconds"] for c in self.clips), 2),
                "usd": round(clip_usd, 6), "estimated_usd": round(clip_estimated, 6), "items": self.clips,
            },
            "total_usd": round(total, 6),
            "estimated_usd": round(estimated, 6),  # the part of total_usd that is a price guess, not a measured price
            "has_estimate": estimated > 0,
        }
        return round(total, 6), breakdown

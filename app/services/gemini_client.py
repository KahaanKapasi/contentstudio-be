import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings

log = logging.getLogger(__name__)

_client: genai.Client | None = None


class GeminiNotConfigured(RuntimeError):
    pass


# --- usage tracking (cost ledger) ---
# Wrap a block in `with track_usage() as usage:`; every Gemini call made inside it (same thread/context)
# appends its token counts to `usage`, so the caller can log the actual cost afterwards.
_usage: ContextVar[list | None] = ContextVar("gemini_usage", default=None)


def _count(obj, name: str) -> int:
    value = getattr(obj, name, None)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def record_usage(response, kind: str = "text", grounded: bool = False, fallback: int = 0) -> None:
    """Remember a response's usage_metadata for the active trackers. Never raises.

    `kind` is "text" (also feeds the `track_usage` sink used by the routers), "image" or "tts"; the latter two only
    feed the active Studio project accumulator. `fallback` is used when usage_metadata is missing: images actually
    returned (priced per image) for "image", estimated audio output tokens for "tts"."""
    try:
        from app.services.costs import usage as project_usage

        meta = getattr(response, "usage_metadata", None)
        prompt = _count(meta, "prompt_token_count") + _count(meta, "tool_use_prompt_token_count")
        output, thinking = _count(meta, "candidates_token_count"), _count(meta, "thoughts_token_count")
        project = project_usage.current()
        if project is not None:
            if kind == "image":
                project.add_image(prompt, output, fallback)
            elif kind == "tts":
                project.add_tts(prompt, output, fallback)
            else:
                project.add_text(prompt, output, thinking, grounded)
        sink = _usage.get()
        if kind == "text" and sink is not None and meta is not None:
            # thinking tokens are billed at the output rate
            sink.append({"kind": kind, "grounded": grounded, "prompt_tokens": prompt, "output_tokens": output + thinking})
    except Exception:
        pass


@contextmanager
def track_usage():
    sink: list = []
    token = _usage.set(sink)
    try:
        yield sink
    finally:
        _usage.reset(token)


def get_client() -> genai.Client:
    global _client
    if not settings.gemini_api_key:
        raise GeminiNotConfigured("GEMINI_API_KEY is not set in .env")
    if _client is None:
        _client = genai.Client(
            api_key=settings.gemini_api_key,
            # A capped/hung project otherwise blocks a request for 20+ s; image/video/TTS calls get their own longer limits.
            http_options=types.HttpOptions(timeout=90_000),
        )
    return _client


def fast_model() -> str:
    """Model for cheap/simple tasks: GEMINI_FAST_MODEL, falling back to the main text model."""
    return settings.gemini_fast_model or settings.gemini_text_model


# --- thinking control for JSON-extraction calls ---
# Gemini 3.x takes `thinking_level` (MINIMAL), 2.5 takes `thinking_budget` (0). Not every model accepts either
# (some can't turn thinking off), so a 400 that mentions thinking is retried once without the config and that
# model is remembered in `_thinking_rejected` so the rejected request is never sent again.
_thinking_rejected: set[str] = set()


def _thinking_config(model: str, effort: str | None):
    if not effort or model in _thinking_rejected:
        return None
    try:
        if "2.5" in model:
            budget = {"minimal": 0, "low": 512, "medium": 2048}.get(effort)
            return types.ThinkingConfig(thinking_budget=budget) if budget is not None else None
        level = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}.get(effort)
        return types.ThinkingConfig(thinking_level=level) if level else None
    except Exception:
        return None


def _is_thinking_rejection(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    return isinstance(exc, genai_errors.APIError) and code in (400, None) and "thinking" in str(exc).lower()


def generate_text(prompt: str, model: str | None = None) -> str:
    client = get_client()
    response = client.models.generate_content(model=model or settings.gemini_text_model, contents=prompt)
    record_usage(response)
    return response.text or ""


def generate_json(prompt: str, model: str | None = None, thinking: str | None = "minimal") -> dict | list:
    """JSON-mode generation. `thinking` ("minimal" by default, None = model default) keeps extraction calls fast
    and cheap; see the note above for how a model that rejects the setting is handled."""
    client = get_client()
    model = model or settings.gemini_text_model
    config = {"response_mime_type": "application/json"}
    thinking_config = _thinking_config(model, thinking)
    try:
        if thinking_config is not None:
            response = client.models.generate_content(
                model=model, contents=prompt, config=types.GenerateContentConfig(**config, thinking_config=thinking_config)
            )
        else:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
    except Exception as exc:
        if thinking_config is None or not _is_thinking_rejection(exc):
            raise
        _thinking_rejected.add(model)
        log.info("Gemini model %s rejected the thinking config; retrying without it", model)
        response = client.models.generate_content(model=model, contents=prompt, config=config)
    record_usage(response)
    return json.loads(response.text or "{}")


def generate_grounded(prompt: str) -> tuple[str, list[dict]]:
    """Text generation with Google Search grounding. Returns (text, sources) where sources
    are de-duplicated {"title", "url"} dicts taken from the grounding metadata."""
    client = get_client()
    response = client.models.generate_content(
        model=settings.gemini_text_model,
        contents=prompt,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
    )
    record_usage(response, grounded=True)
    return response.text or "", _grounding_sources(response)


def _grounding_sources(response) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    candidates = getattr(response, "candidates", None) or []
    metadata = getattr(candidates[0], "grounding_metadata", None) if candidates else None
    for chunk in getattr(metadata, "grounding_chunks", None) or []:
        web = getattr(chunk, "web", None)
        url = getattr(web, "uri", None)
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({"title": getattr(web, "title", None) or url, "url": url})
    return sources

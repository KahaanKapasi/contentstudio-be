import json
from contextlib import contextmanager
from contextvars import ContextVar

from google import genai
from google.genai import types

from app.config import settings

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


def record_usage(response, kind: str = "text", grounded: bool = False) -> None:
    """Remember a response's usage_metadata for the active tracker. Never raises."""
    try:
        sink = _usage.get()
        meta = getattr(response, "usage_metadata", None)
        if sink is None or meta is None:
            return
        sink.append(
            {
                "kind": kind,
                "grounded": grounded,
                "prompt_tokens": _count(meta, "prompt_token_count") + _count(meta, "tool_use_prompt_token_count"),
                # thinking tokens are billed at the output rate
                "output_tokens": _count(meta, "candidates_token_count") + _count(meta, "thoughts_token_count"),
            }
        )
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
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def generate_text(prompt: str) -> str:
    client = get_client()
    response = client.models.generate_content(model=settings.gemini_text_model, contents=prompt)
    record_usage(response)
    return response.text or ""


def generate_json(prompt: str) -> dict | list:
    client = get_client()
    response = client.models.generate_content(
        model=settings.gemini_text_model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
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

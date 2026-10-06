import json

from google import genai
from google.genai import types

from app.config import settings

_client: genai.Client | None = None


class GeminiNotConfigured(RuntimeError):
    pass


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
    return response.text or ""


def generate_json(prompt: str) -> dict | list:
    client = get_client()
    response = client.models.generate_content(
        model=settings.gemini_text_model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
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

import json

from google import genai

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


MODEL = "gemini-2.0-flash"


def generate_text(prompt: str) -> str:
    client = get_client()
    response = client.models.generate_content(model=MODEL, contents=prompt)
    return response.text or ""


def generate_json(prompt: str) -> dict | list:
    client = get_client()
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    return json.loads(response.text or "{}")

from types import SimpleNamespace

import pytest
from google.genai import errors

from app.config import settings
from app.services import gemini_client


class FakeModels:
    def __init__(self, reject_thinking=False):
        self.calls = []
        self.reject_thinking = reject_thinking

    def generate_content(self, model, contents, config=None):
        self.calls.append((model, config))
        has_thinking = getattr(config, "thinking_config", None) is not None
        if self.reject_thinking and has_thinking:
            raise errors.ClientError(400, {"error": {"code": 400, "message": "Thinking level is not supported for this model", "status": "INVALID_ARGUMENT"}})
        return SimpleNamespace(text='{"ok": true}', usage_metadata=None)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(gemini_client, "_thinking_rejected", set())

    def install(reject=False):
        models = FakeModels(reject)
        monkeypatch.setattr(gemini_client, "get_client", lambda: SimpleNamespace(models=models))
        return models

    return install


def test_json_calls_request_minimal_thinking_by_default(fake):
    models = fake()
    assert gemini_client.generate_json("p") == {"ok": True}
    config = models.calls[0][1]
    assert config.thinking_config.thinking_level.value == "MINIMAL"
    assert config.response_mime_type == "application/json"


def test_gemini_25_uses_thinking_budget_zero(fake):
    models = fake()
    gemini_client.generate_json("p", model="gemini-2.5-flash")
    assert models.calls[0][1].thinking_config.thinking_budget == 0


def test_thinking_none_sends_plain_json_config(fake):
    models = fake()
    gemini_client.generate_json("p", thinking=None)
    assert models.calls[0][1] == {"response_mime_type": "application/json"}


def test_rejected_thinking_retries_once_and_is_remembered(fake):
    models = fake(reject=True)
    assert gemini_client.generate_json("p") == {"ok": True}
    assert len(models.calls) == 2 and models.calls[1][1] == {"response_mime_type": "application/json"}
    gemini_client.generate_json("p")
    assert len(models.calls) == 3  # second call did not retry: thinking config no longer sent


def test_other_errors_are_not_swallowed(fake, monkeypatch):
    models = fake()

    def boom(**kw):
        raise errors.ClientError(429, {"error": {"code": 429, "message": "spend cap", "status": "RESOURCE_EXHAUSTED"}})

    models.generate_content = lambda **kw: boom(**kw)
    with pytest.raises(errors.ClientError):
        gemini_client.generate_json("p")


def test_model_override_and_fast_model(fake, monkeypatch):
    models = fake()
    monkeypatch.setattr(settings, "gemini_fast_model", "gemini-lite")
    assert gemini_client.fast_model() == "gemini-lite"
    gemini_client.generate_json("p", model=gemini_client.fast_model())
    assert models.calls[0][0] == "gemini-lite"
    monkeypatch.setattr(settings, "gemini_fast_model", "")
    assert gemini_client.fast_model() == settings.gemini_text_model

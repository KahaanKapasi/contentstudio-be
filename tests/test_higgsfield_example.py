"""examples/higgsfield_seedance/main.py must only report success for Completed + a real video URL.
Hermetic: the SDK is faked, .env.local is never read, no network."""

import importlib.util
from pathlib import Path

import pytest
from higgsfield_client import Cancelled, Completed, Failed, InProgress, NSFW, Queued

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "higgsfield_seedance" / "main.py"


@pytest.fixture()
def example(monkeypatch):
    spec = importlib.util.spec_from_file_location("hf_seedance_example", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "load_dotenv", lambda *a, **k: None)  # never touch the real .env.local
    monkeypatch.setenv("HF_KEY", "fake-id:fake-secret")
    return mod


def fake_subscribe(final_status, result):
    def _subscribe(model, arguments, on_enqueue=None, on_queue_update=None):
        assert model == "bytedance/seedance-2.5/text-to-video"
        assert arguments == {"prompt": "A cinematic scene at sunset", "duration": 5, "resolution": "720p", "aspect_ratio": "16:9"}
        on_enqueue("req-1")
        for status in (Queued(), InProgress(), final_status):
            on_queue_update(status)
        return result

    return _subscribe


def run(example, monkeypatch, status, result):
    monkeypatch.setattr(example.higgsfield_client, "subscribe", fake_subscribe(status, result))
    return example.main()


def test_success_prints_url(example, monkeypatch, capsys):
    code = run(example, monkeypatch, Completed(), {"video": {"url": "https://cdn.example/v.mp4"}})
    assert code == 0 and "https://cdn.example/v.mp4" in capsys.readouterr().out


def test_bare_string_video_field_is_accepted(example, monkeypatch):
    assert run(example, monkeypatch, Completed(), {"video": "https://cdn.example/v.mp4"}) == 0


@pytest.mark.parametrize("status,code,word", [(NSFW(), 3, "MODERATED"), (Failed(), 1, "FAILED"), (Cancelled(), 1, "CANCELED")])
def test_non_completed_is_never_success(example, monkeypatch, capsys, status, code, word):
    # Even if the API hands back a URL-looking payload, a non-Completed status must not be reported as success.
    assert run(example, monkeypatch, status, {"video": {"url": "https://cdn.example/v.mp4"}}) == code
    captured = capsys.readouterr()
    assert word in captured.err and "SUCCESS" not in captured.out


def test_completed_without_url_is_not_success(example, monkeypatch, capsys):
    assert run(example, monkeypatch, Completed(), {"detail": "weird"}) == 1
    assert "SUCCESS" not in capsys.readouterr().out


def test_missing_or_malformed_credentials_exit_2_without_calling_the_api(example, monkeypatch, capsys):
    monkeypatch.setattr(example.higgsfield_client, "subscribe", lambda *a, **k: pytest.fail("must not call the API"))
    for bad in ("", "no-colon", ":secret", "id:"):
        monkeypatch.setenv("HF_KEY", bad)
        assert example.main() == 2
    assert "fake-secret" not in capsys.readouterr().err


def test_sdk_exception_is_reported_as_failure(example, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("HTTP 402 insufficient credits")

    monkeypatch.setattr(example.higgsfield_client, "subscribe", boom)
    assert example.main() == 1
    assert "insufficient credits" in capsys.readouterr().err

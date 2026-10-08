"""End-to-end runs of the motion and faceless engines with Gemini/Pexels/edge-tts mocked and real (tiny) ffmpeg."""

import json
import shutil

import pytest

from app.config import settings
from app.models import VideoProject
from app.services import gemini_client
from app.services.studio import runner
from app.services.studio.engines import faceless
from app.services.studio.kit import ffmpeg, stock, tts
from app.services.studio.motion import examples
from studio_helpers import fake_edge_fetch, make_test_video, make_wav_bytes, needs_ffmpeg

pytestmark = needs_ffmpeg


def run_to_end(client, db, engine, params, files=None, auto=False):
    import json as _json

    data = {"engine": engine, "params": _json.dumps(params)}
    if auto:
        data["auto_approve"] = "true"
    created = client.post("/api/studio/projects", data=data, files=files)  # background task runs inline in TestClient
    assert created.status_code == 200, created.text
    return client.get(f"/api/studio/projects/{created.json()['id']}").json()


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test-key")
    monkeypatch.setattr(settings, "pexels_api_key", "pexels-test-key")


@pytest.fixture
def edge(monkeypatch):
    monkeypatch.setattr(tts, "_edge_fetch", fake_edge_fetch)


def test_motion_engine_with_narration_and_captions(client, db, keys, edge, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"title": "Madrid", "narration": "Madrid have won fifteen titles. Nobody else is close."})
    prompts = []
    monkeypatch.setattr(gemini_client, "generate_text", lambda p: prompts.append(p) or f"```python\n{examples.STAT_REVEAL}\n```")
    body = run_to_end(client, db, "motion", {"brief": "15 UCL titles", "duration": 5, "narration": True, "subtitles": True})
    assert body["status"] == "succeeded", body["error"]
    assert body["progress"] == 100 and body["has_file"] and body["plan"]["script"].startswith("Madrid have won")
    assert [p["name"] for p in body["previews"]] == ["narration"]
    assert len(body["plan"]["scenes"]) == 2
    assert "0.0s-" in prompts[0] and "Madrid have won fifteen titles." in prompts[0]  # narration cues reach the scene prompt
    final = runner.project_dir(body["id"]) / "final.mp4"
    probe = ffmpeg.probe(final)
    assert (probe.width, probe.height) == (1080, 1920) and probe.has_audio and probe.duration >= 5.0
    assert client.get(f"/api/studio/projects/{body['id']}/file").status_code == 200


def test_motion_engine_silent_and_resume_after_failure(client, db, keys, monkeypatch):
    attempts = {"n": 0}

    def flaky(p):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return "```python\nimport os\n```"  # both attempts (initial + fix) are rejected
        return f"```python\n{examples.STAT_REVEAL}\n```"

    monkeypatch.setattr(gemini_client, "generate_text", flaky)
    body = run_to_end(client, db, "motion", {"brief": "x", "duration": 5})
    assert body["status"] == "failed" and "problem" in body["error"]
    retried = client.post(f"/api/studio/projects/{body['id']}/retry").json()
    assert retried["status"] in ("queued", "succeeded")
    done = client.get(f"/api/studio/projects/{body['id']}").json()
    assert done["status"] == "succeeded", done["error"]
    assert not ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4").has_audio


@pytest.fixture
def stock_mocks(monkeypatch, tmp_path):
    source = make_test_video(tmp_path / "stock_src.mp4", seconds=4.0, size="320x568")
    found = [stock.StockClip(str(i), "pexels", 6, 320, 568, f"https://x/{i}") for i in range(3)]
    seen = {}

    def find(terms, aspect, min_duration, **kw):
        seen.update(terms=terms, aspect=aspect, min_duration=min_duration)
        return found

    monkeypatch.setattr(stock, "find_clips", find)
    monkeypatch.setattr(stock, "download", lambda clip, dest, **kw: (dest.parent.mkdir(parents=True, exist_ok=True), shutil.copy(source, dest))[1])
    return seen


def test_faceless_engine_full_pipeline_with_bgm(client, db, keys, edge, stock_mocks, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_text", lambda p: "# Title\n**Intro** Madrid keep winning trophies every single year. The club is huge.\n\nThey never give up.")
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: ["Real Madrid stadium", "football crowd", "Real Madrid stadium", "trophy"])
    bgm = make_wav_bytes(1.0)
    body = run_to_end(
        client, db, "faceless", {"subject": "Madrid", "clip_duration": 2, "paragraphs": 2}, files={"bgm": ("bgm.wav", bgm, "audio/wav")}
    )
    assert body["status"] == "succeeded", body["error"]
    assert stock_mocks["terms"] == ["real madrid stadium", "football crowd", "trophy"]  # normalised + de-duplicated
    assert stock_mocks["min_duration"] == 2 and stock_mocks["aspect"] == "9:16"
    script = body["plan"]["script"]
    assert "Title" not in script and "*" not in script and "Madrid keep winning" in script
    assert body["plan"]["scenes"] and "football crowd" in body["plan"]["summary"]
    folder = runner.project_dir(body["id"])
    probe = ffmpeg.probe(folder / "final.mp4")
    assert (probe.width, probe.height) == (1080, 1920) and probe.has_audio
    assert probe.duration == pytest.approx(json.loads(db.get(VideoProject, body["id"]).assets)["data"]["total"], abs=0.3)
    assert not list(folder.glob("clips/*.mp4"))  # intermediates cleaned up
    assert (folder / "captions.ass").is_file()


def test_faceless_uses_supplied_script_and_skips_writing(client, db, keys, edge, stock_mocks, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_text", lambda p: pytest.fail("script must not be generated"))
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"terms": ["goal"]})
    body = run_to_end(client, db, "faceless", {"subject": "Goals", "script": "A great goal was scored today. The crowd went wild.", "clip_duration": 3, "subtitles": False})
    assert body["status"] == "succeeded", body["error"]
    assert body["plan"]["script"].startswith("A great goal")


def test_faceless_fails_cleanly_when_no_footage(client, db, keys, edge, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: ["zzz"])
    monkeypatch.setattr(stock, "find_clips", lambda *a, **k: [])
    body = run_to_end(client, db, "faceless", {"subject": "S", "script": "One two three four. Five six seven eight."})
    assert body["status"] == "failed" and "No stock footage" in body["error"] and body["stage"] == "Finding footage"


def test_faceless_download_skips_unreadable_clips(client, db, keys, edge, stock_mocks, monkeypatch, tmp_path):
    good = tmp_path / "stock_src.mp4"
    calls = {"n": 0}

    def flaky_download(clip, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        calls["n"] += 1
        if calls["n"] == 1:
            dest.write_bytes(b"not a video")
        else:
            shutil.copy(good, dest)
        return dest

    monkeypatch.setattr(stock, "download", flaky_download)
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: ["goal"])
    body = run_to_end(client, db, "faceless", {"subject": "S", "script": "One two three four. Five six seven eight.", "clip_duration": 2, "subtitles": False})
    assert body["status"] == "succeeded", body["error"]


def test_clean_script_strips_markup():
    assert faceless.clean_script("# Title\n* **Bold** point (visual: crowd)\n1. second [pause]") == "Bold point\nsecond"

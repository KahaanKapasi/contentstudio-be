import json

import pytest

from app.config import settings
from app.services.studio import registry, runner


@pytest.fixture(autouse=True)
def no_background_run(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_project", lambda pid: calls.append(pid))
    return calls


def create(client, engine="motion", recipe=None, params=None, files=None, auto_approve=False):
    data = {"engine": engine, "params": json.dumps(params or {})}
    if recipe:
        data["recipe"] = recipe
    if auto_approve:
        data["auto_approve"] = "true"
    return client.post("/api/studio/projects", data=data, files=files)


def test_engines_listing_shape_and_keys(client):
    engines = {e["id"]: e for e in client.get("/api/studio/engines").json()}
    assert list(engines) == ["motion", "faceless", "skill", "toons", "shorts", "explainer3d"]
    motion = engines["motion"]["recipes"][0]
    assert motion["id"] == "motion" and motion["paid"] is False
    assert motion["configured"] is False and motion["missing_keys"] == ["GEMINI_API_KEY"]
    assert {f["name"] for f in motion["fields"]} >= {"brief", "data", "aspect", "duration", "narration", "tts_provider", "voice"}
    faceless = engines["faceless"]["recipes"][0]
    assert faceless["missing_keys"] == ["GEMINI_API_KEY", "PEXELS_API_KEY"]
    assert {f["name"] for f in faceless["fields"]} >= {"subject", "script", "language", "aspect", "clip_duration", "bgm", "subtitles", "paragraphs"}
    assert next(f for f in faceless["fields"] if f["name"] == "bgm")["type"] == "audio"


def test_engines_configured_when_keys_set(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "pexels_api_key", "k")
    engines = {e["id"]: e for e in client.get("/api/studio/engines").json()}
    assert engines["motion"]["recipes"][0]["configured"] is True
    assert engines["faceless"]["recipes"][0]["missing_keys"] == []


def test_placeholder_engines_have_full_recipes_and_coming_soon(client):
    engines = {e["id"]: e for e in client.get("/api/studio/engines").json()}
    assert [r["id"] for r in engines["skill"]["recipes"]] == ["singing-story", "podcast", "ps1-lowpoly", "spiderverse", "product-demo"]
    assert [r["id"] for r in engines["toons"]["recipes"]] == ["sketch", "parody-song"]
    assert [r["id"] for r in engines["shorts"]["recipes"]] == ["dance", "film"]
    assert [r["id"] for r in engines["explainer3d"]["recipes"]] == ["whatif"]
    for eid in ("skill", "toons", "shorts", "explainer3d"):
        soon = not registry.engines()[eid].implemented  # implemented engines drop the "(coming soon)" suffix
        assert engines[eid]["description"].endswith("(coming soon)") is soon
        assert all(r["description"].endswith("(coming soon)") is soon and r["fields"] for r in engines[eid]["recipes"])
    dance = {f["name"]: f for f in engines["shorts"]["recipes"][0]["fields"]}
    assert dance["keep_audio"]["default"] is False and dance["trend_video"]["type"] == "video"
    sketch = {f["name"] for f in engines["toons"]["recipes"][0]["fields"]}
    assert "motion_quality" in sketch


def test_unimplemented_engine_returns_501(client, monkeypatch):
    # toons / shorts / explainer3d are implemented now (see test_studio_scenefilm.py); the 501 gate still exists for any placeholder
    monkeypatch.setattr(registry.engines()["toons"], "implemented", False)
    assert create(client, "toons", "sketch", {"premise": "x"}).status_code == 501


def test_unknown_engine_and_missing_recipe(client):
    assert create(client, "nope").status_code == 400
    r = create(client, "skill")
    assert r.status_code == 400 and "recipe" in r.json()["detail"]


def test_params_validation_names_the_field(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    r = create(client, "motion", params={"duration": 10})
    assert r.status_code == 400 and "brief" in r.json()["detail"]
    r = create(client, "motion", params={"brief": "x", "duration": 500})
    assert r.status_code == 400 and "duration" in r.json()["detail"]
    r = create(client, "motion", params={"brief": "x", "aspect": "4:3"})
    assert r.status_code == 400 and "aspect" in r.json()["detail"]
    assert create(client, "motion", params="not-json").status_code == 400
    r = client.post("/api/studio/projects", data={"engine": "motion", "params": "[1]"})
    assert r.status_code == 400


def test_missing_keys_503_names_env_vars(client):
    r = create(client, "faceless", params={"subject": "Madrid"})
    assert r.status_code == 503
    assert "GEMINI_API_KEY" in r.json()["detail"] and "PEXELS_API_KEY" in r.json()["detail"]


def test_create_motion_project_defaults_and_queues(client, monkeypatch, no_background_run):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    r = create(client, "motion", params={"brief": "15 UCL titles"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["engine"] == "motion" and body["recipe"] is None
    assert body["params"]["duration"] == 15 and body["params"]["aspect"] == "9:16" and body["params"]["narration"] is False
    assert body["plan"] is None and body["previews"] == [] and body["has_file"] is False
    assert no_background_run == [body["id"]]
    assert client.get(f"/api/studio/projects/{body['id']}").json()["id"] == body["id"]
    assert [p["id"] for p in client.get("/api/studio/projects").json()] == [body["id"]]


def test_faceless_upload_validation_and_saved_inputs(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "pexels_api_key", "k")
    bad = create(client, "faceless", params={"subject": "x"}, files={"bgm": ("song.exe", b"x", "audio/mpeg")})
    assert bad.status_code == 400 and "bgm" in bad.json()["detail"]
    ok = create(client, "faceless", params={"subject": "x"}, files={"bgm": ("song.mp3", b"ID3data", "audio/mpeg")})
    assert ok.status_code == 200
    pid = ok.json()["id"]
    assert (runner.project_dir(pid) / "inputs" / "bgm_0.mp3").read_bytes() == b"ID3data"


def test_unknown_project_and_wrong_state_409(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    assert client.get("/api/studio/projects/999").status_code == 404
    pid = create(client, "motion", params={"brief": "x"}).json()["id"]
    assert client.post(f"/api/studio/projects/{pid}/approve").status_code == 409
    assert client.post(f"/api/studio/projects/{pid}/retry").status_code == 409
    assert client.post(f"/api/studio/projects/{pid}/replan").status_code == 409
    assert client.get(f"/api/studio/projects/{pid}/file").status_code == 404
    assert client.get(f"/api/studio/projects/{pid}/assets/narration").status_code == 404


def test_delete_removes_row_and_dir(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    pid = create(client, "motion", params={"brief": "x"}).json()["id"]
    folder = runner.project_dir(pid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "x.txt").write_text("1")
    # the stale-guard: a queued row nobody works on is deletable
    assert client.delete(f"/api/studio/projects/{pid}").status_code == 204
    assert not folder.exists()
    assert client.get(f"/api/studio/projects/{pid}").status_code == 404


def test_access_gate_applies(client, monkeypatch):
    monkeypatch.setattr(settings, "local_access_password", "pw")
    assert client.get("/api/studio/engines").status_code == 401
    assert client.get("/api/studio/engines", headers={"X-Access-Password": "pw"}).status_code == 200

import json
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types

from app.config import settings
from app.models import VideoGeneration
from app.services import gemini_client, media_hosting, video_generation
from app.services.video_providers import catalog

MP4 = b"\x00\x00\x00\x18ftypmp42FAKE-MP4-BYTES"


# ---------- helpers ----------


class FakeVeoClient:
    """Stands in for genai.Client: submit returns a pending op, get() returns done after `polls_until_done`."""

    def __init__(self, polls_until_done=1, fail=None, filtered=False):
        self.calls = {"submit": [], "get": []}
        self.polls_until_done = polls_until_done
        self.fail = fail
        self.filtered = filtered
        self.models = SimpleNamespace(generate_videos=self._submit)
        self.operations = SimpleNamespace(get=self._get)
        self.files = SimpleNamespace(download=lambda file: MP4)

    def _submit(self, *, model, prompt, config):
        self.calls["submit"].append((model, prompt, config))
        return types.GenerateVideosOperation(name="models/veo/operations/op-1", done=False)

    def _get(self, operation):
        self.calls["get"].append(operation.name)
        if len(self.calls["get"]) < self.polls_until_done:
            return types.GenerateVideosOperation(name=operation.name, done=False)
        if self.fail:
            return types.GenerateVideosOperation(name=operation.name, done=True, error={"message": self.fail})
        videos = [] if self.filtered else [types.GeneratedVideo(video=types.Video(uri="files/abc"))]
        resp = types.GenerateVideosResponse(generated_videos=videos, rai_media_filtered_reasons=["celebrity"] if self.filtered else None)
        return types.GenerateVideosOperation(name=operation.name, done=True, response=resp)


@pytest.fixture
def veo(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    def install(**kwargs):
        fake = FakeVeoClient(**kwargs)
        monkeypatch.setattr(gemini_client, "get_client", lambda: fake)
        return fake

    return install


@pytest.fixture
def http(monkeypatch):
    """Route every httpx.Client / httpx.stream through a MockTransport handler."""
    real_client = httpx.Client

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
        monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: real_client(transport=transport).stream(method, url, **kw))

    return install


def body(**over):
    data = dict(
        prompt="A striker scores a bicycle kick at dusk",
        original_idea="bicycle kick",
        provider="veo",
        model="veo-3.1-fast-generate-preview",
        aspect_ratio="9:16",
        duration_seconds=8,
        resolution="720p",
        research_sources=[{"title": "BBC", "url": "https://bbc.com/x"}],
    )
    data.update(over)
    return data


def higgs_handler(statuses, state=None, secret_check=True):
    state = state if state is not None else {"polls": 0, "submitted": None}

    def handler(request: httpx.Request):
        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=MP4)
        if secret_check:
            assert request.headers["Authorization"] == "Key id-1:secret-1"
        if request.method == "POST":
            state["submitted"] = (request.url.path, json.loads(request.content), request.headers.get("Idempotency-Key"))
            return httpx.Response(200, json={"status": "queued", "request_id": "req-1", "status_url": "https://api.higgsfield.ai/requests/req-1/status", "cancel_url": "x"})
        status = statuses[min(state["polls"], len(statuses) - 1)]
        state["polls"] += 1
        return httpx.Response(200, json=status)

    handler.state = state
    return handler


@pytest.fixture
def hf_keys(monkeypatch):
    monkeypatch.setattr(settings, "hf_api_key_id", "id-1")
    monkeypatch.setattr(settings, "hf_api_key_secret", "secret-1")


# ---------- catalog ----------


def test_catalog_accepts_valid_and_rejects_with_readable_messages():
    catalog.validate("veo", "veo-3.1-fast-generate-preview", "16:9", 8, "1080p")
    with pytest.raises(catalog.CatalogError, match="aspect ratio 1:1"):
        catalog.validate("veo", "veo-3.1-fast-generate-preview", "1:1", 8, "720p")
    with pytest.raises(catalog.CatalogError, match="5s duration"):
        catalog.validate("veo", "veo-3.1-fast-generate-preview", "16:9", 5, "720p")
    with pytest.raises(catalog.CatalogError, match="4k resolution"):
        catalog.validate("veo", "veo-3.1-lite-generate-preview", "16:9", 8, "4k")
    with pytest.raises(catalog.CatalogError, match="1080p requires a duration of 8"):
        catalog.validate("veo", "veo-3.1-fast-generate-preview", "16:9", 4, "1080p")
    with pytest.raises(catalog.CatalogError, match="Model 'nope'"):
        catalog.validate("veo", "nope", "16:9", 8, "720p")
    with pytest.raises(catalog.CatalogError, match="Unknown provider"):
        catalog.validate("sora", "x", "16:9", 8, "720p")


def test_cost_estimate_uses_price_per_second_and_is_none_when_unknown():
    assert catalog.estimate_cost("veo", "veo-3.1-fast-generate-preview", "720p", 8) == 0.8
    assert catalog.estimate_cost("veo", "veo-3.1-generate-preview", "4k", 8) == 4.8
    assert catalog.estimate_cost("higgsfield", "seedance-2.0", "720p", 5) is None
    assert catalog.estimate_cost("muapi", "wan2.5-text-to-video", "720p", 5) is None
    assert catalog.estimate_cost("veo", "bogus", "720p", 5) is None


def test_every_rest_model_has_a_path_and_a_consistent_body():
    for provider in ("higgsfield", "muapi"):
        for spec in catalog.MODELS[provider].values():
            assert spec.path, spec.id
            payload = catalog.build_body(spec, "p", spec.aspect_ratios[0], spec.durations[0], spec.resolutions[0])
            assert payload["prompt"] == "p"
            assert set(spec.body_fields) <= {"aspect_ratio", "duration", "resolution"}


def test_build_body_renames_fields_and_adds_fixed_extras():
    seedance = catalog.MUAPI_MODELS["seedance-v2.0-t2v"]
    assert catalog.build_body(seedance, "p", "9:16", 5, "high") == {"prompt": "p", "aspect_ratio": "9:16", "duration": 5, "quality": "high"}
    kling = catalog.MUAPI_MODELS["kling-v3.0-pro-text-to-video"]
    assert catalog.build_body(kling, "p", "9:16", 5, "1080p")["resolution"] == "1080p"


# ---------- providers endpoint ----------


def test_providers_report_missing_keys_then_configured(client, monkeypatch):
    by_id = {p["id"]: p for p in client.get("/api/video/providers").json()}
    assert set(by_id) == {"veo", "higgsfield", "muapi"}
    assert by_id["veo"]["configured"] is False and by_id["veo"]["missing_keys"] == ["GEMINI_API_KEY"]
    assert by_id["higgsfield"]["missing_keys"] == ["HF_API_KEY_ID", "HF_API_KEY_SECRET"]
    assert by_id["muapi"]["missing_keys"] == ["MUAPI_API_KEY"]

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "hf_api_key_id", "a")
    veo, hf = (next(p for p in client.get("/api/video/providers").json() if p["id"] == i) for i in ("veo", "higgsfield"))
    assert veo["configured"] is True and veo["missing_keys"] == []
    assert hf["configured"] is False and hf["missing_keys"] == ["HF_API_KEY_SECRET"]


def test_providers_shape_matches_contract(client):
    veo = next(p for p in client.get("/api/video/providers").json() if p["id"] == "veo")
    assert veo["default_model"] in {m["id"] for m in veo["models"]}
    fast = next(m for m in veo["models"] if m["id"] == "veo-3.1-fast-generate-preview")
    assert fast["aspect_ratios"] == ["16:9", "9:16"] and fast["durations"] == [4, 6, 8]
    assert fast["price_per_second_usd"] == {"720p": 0.10, "1080p": 0.12, "4k": 0.30}
    assert set(fast) == {"id", "label", "aspect_ratios", "durations", "resolutions", "price_per_second_usd", "notes"}
    hf = next(p for p in client.get("/api/video/providers").json() if p["id"] == "higgsfield")
    assert all(m["price_per_second_usd"] is None for m in hf["models"])


# ---------- improve prompt ----------


def test_improve_without_research_uses_plain_generation(client, configure_gemini, monkeypatch):
    seen = {}
    monkeypatch.setattr(gemini_client, "generate_text", lambda p: seen.setdefault("p", p) and "  A cinematic prompt.  ")
    monkeypatch.setattr(gemini_client, "generate_grounded", lambda p: pytest.fail("must not search"))
    resp = client.post("/api/video/prompt/improve", json={"idea": "derby goal", "research": False, "aspect_ratio": "9:16", "duration_seconds": 6})
    assert resp.status_code == 200
    assert resp.json() == {"prompt": "A cinematic prompt.", "sources": [], "research_notes": None}
    assert "6-second" in seen["p"] and "9:16" in seen["p"] and "derby goal" in seen["p"]


def test_improve_with_research_parses_notes_prompt_and_sources(client, configure_gemini, monkeypatch):
    text = "Final was 2-1.\nKits were red.\n=== VIDEO PROMPT ===\nWide shot of a red-kitted striker."
    monkeypatch.setattr(gemini_client, "generate_grounded", lambda p: (text, [{"title": "BBC", "url": "https://bbc.com/a"}]))
    out = client.post("/api/video/prompt/improve", json={"idea": "the final", "research": True, "aspect_ratio": "16:9", "duration_seconds": 8}).json()
    assert out["prompt"] == "Wide shot of a red-kitted striker."
    assert out["research_notes"] == "Final was 2-1.\nKits were red."
    assert out["sources"] == [{"title": "BBC", "url": "https://bbc.com/a"}]


def test_improve_research_tolerates_missing_marker(client, configure_gemini, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_grounded", lambda p: ("Just a prompt.", []))
    out = client.post("/api/video/prompt/improve", json={"idea": "x", "research": True, "aspect_ratio": "16:9", "duration_seconds": 8}).json()
    assert out == {"prompt": "Just a prompt.", "sources": [], "research_notes": None}


def test_grounding_sources_parsed_deduped_and_missing_metadata_tolerated():
    chunk = lambda t, u: SimpleNamespace(web=SimpleNamespace(title=t, uri=u))
    meta = SimpleNamespace(grounding_chunks=[chunk("A", "https://a"), chunk(None, "https://b"), chunk("dup", "https://a"), SimpleNamespace(web=None)])
    resp = SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=meta)])
    assert gemini_client._grounding_sources(resp) == [{"title": "A", "url": "https://a"}, {"title": "https://b", "url": "https://b"}]
    assert gemini_client._grounding_sources(SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=None)])) == []
    assert gemini_client._grounding_sources(SimpleNamespace(candidates=None)) == []


def test_generate_text_uses_configured_model(configure_gemini, monkeypatch):
    seen = {}
    fake = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: seen.update(kw) or SimpleNamespace(text="ok")))
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "gemini_text_model", "my-model")
    assert gemini_client.generate_text("hi") == "ok" and seen["model"] == "my-model"


def test_improve_503_without_gemini_key(client):
    resp = client.post("/api/video/prompt/improve", json={"idea": "x", "research": True, "aspect_ratio": "16:9", "duration_seconds": 8})
    assert resp.status_code == 503 and "GEMINI_API_KEY" in resp.json()["detail"]


def test_improve_400_for_blank_idea(client, configure_gemini):
    assert client.post("/api/video/prompt/improve", json={"idea": "  ", "research": False, "aspect_ratio": "16:9", "duration_seconds": 8}).status_code == 400


def test_video_generation_endpoints_sit_behind_the_access_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "local_access_password", "pw")
    for method, path in [("get", "/providers"), ("get", "/generations"), ("get", "/generations/1"), ("get", "/generations/1/file"), ("delete", "/generations/1")]:
        assert getattr(client, method)(f"/api/video{path}").status_code == 401, path


# ---------- create / run: Veo ----------


def test_create_veo_runs_in_background_and_succeeds(client, db, veo):
    fake = veo(polls_until_done=2)
    resp = client.post("/api/video/generations", json=body())
    assert resp.status_code == 200
    created = resp.json()
    assert created["status"] == "queued" and created["has_file"] is False
    assert created["estimated_cost_usd"] == 0.8
    assert created["research_sources"] == [{"title": "BBC", "url": "https://bbc.com/x"}]

    done = client.get(f"/api/video/generations/{created['id']}").json()
    assert done["status"] == "succeeded" and done["has_file"] is True and done["error"] is None
    assert done["completed_at"].endswith("+00:00") or done["completed_at"].endswith("Z")
    model, prompt, config = fake.calls["submit"][0]
    assert (model, config.aspect_ratio, config.duration_seconds, config.resolution) == ("veo-3.1-fast-generate-preview", "9:16", 8, "720p")
    assert fake.calls["get"] == ["models/veo/operations/op-1"] * 2
    assert client.get(f"/api/video/generations/{created['id']}/file").content == MP4


def test_create_validation_400_names_the_problem(client, veo):
    veo()
    resp = client.post("/api/video/generations", json=body(aspect_ratio="1:1"))
    assert resp.status_code == 400 and "aspect ratio 1:1" in resp.json()["detail"]
    assert client.post("/api/video/generations", json=body(prompt="  ")).status_code == 400
    assert client.get("/api/video/generations").json() == []


def test_create_503_names_missing_env_vars(client):
    resp = client.post("/api/video/generations", json=body())
    assert resp.status_code == 503 and "GEMINI_API_KEY" in resp.json()["detail"]
    resp = client.post("/api/video/generations", json=body(provider="higgsfield", model="seedance-2.0", resolution="720p", duration_seconds=5, aspect_ratio="9:16"))
    assert resp.status_code == 503
    assert "HF_API_KEY_ID" in resp.json()["detail"] and "HF_API_KEY_SECRET" in resp.json()["detail"]
    resp = client.post("/api/video/generations", json=body(provider="muapi", model="wan2.5-text-to-video", resolution="720p", duration_seconds=5))
    assert resp.status_code == 503 and "MUAPI_API_KEY" in resp.json()["detail"]


def test_veo_provider_error_becomes_clean_failed_row(client, veo):
    veo(fail="Resource exhausted: quota Traceback (most recent call last)\n  File x")
    created = client.post("/api/video/generations", json=body()).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "failed" and got["has_file"] is False
    assert "Veo could not generate this video" in got["error"]


def test_veo_safety_filter_message(client, veo):
    veo(filtered=True)
    created = client.post("/api/video/generations", json=body()).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "failed" and "safety filters" in got["error"] and "celebrity" in got["error"]


def test_submit_exception_never_leaks_traceback_or_key(client, veo, monkeypatch):
    fake = veo()
    fake.models = SimpleNamespace(generate_videos=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom test-key Traceback")))
    created = client.post("/api/video/generations", json=body()).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "failed"
    assert "test-key" not in got["error"] and "Traceback" not in got["error"] and "boom" not in got["error"]


# ---------- create / run: Higgsfield and Muapi ----------


def test_higgsfield_end_to_end(client, hf_keys, http):
    handler = higgs_handler([
        {"status": "queued", "request_id": "req-1"},
        {"status": "in_progress", "request_id": "req-1"},
        {"status": "mystery_state"},
        {"status": "completed", "request_id": "req-1", "video": {"url": "https://cdn.example.com/out.mp4"}},
    ])
    http(handler)
    payload = body(provider="higgsfield", model="seedance-2.0", aspect_ratio="9:16", duration_seconds=6, resolution="1080p")
    created = client.post("/api/video/generations", json=payload).json()
    assert created["status"] == "queued" and created["estimated_cost_usd"] is None
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "succeeded" and got["has_file"] is True
    path, sent, idem = handler.state["submitted"]
    assert path == "/bytedance/seedance-2.0/text-to-video"
    assert sent == {"prompt": payload["prompt"], "aspect_ratio": "9:16", "duration": 6, "resolution": "1080p", "generate_audio": True}
    assert idem
    assert client.get(f"/api/video/generations/{created['id']}/file").content == MP4


@pytest.mark.parametrize("status,expect", [("nsfw", "moderation"), ("failed", "could not generate"), ("canceled", "canceled")])
def test_higgsfield_terminal_failures(client, hf_keys, http, status, expect):
    http(higgs_handler([{"status": status, "error": "provider detail"}]))
    created = client.post("/api/video/generations", json=body(provider="higgsfield", model="kling-3.0-turbo", aspect_ratio="9:16", duration_seconds=5, resolution="720p")).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "failed" and expect in got["error"].lower()


def test_higgsfield_completed_without_video_fails_cleanly(client, hf_keys, http):
    http(higgs_handler([{"status": "completed"}]))
    created = client.post("/api/video/generations", json=body(provider="higgsfield", model="kling-3.0-turbo", aspect_ratio="9:16", duration_seconds=5, resolution="720p")).json()
    assert "no video" in client.get(f"/api/video/generations/{created['id']}").json()["error"]


def test_higgsfield_submit_rejection_is_user_readable(client, hf_keys, http):
    http(lambda req: httpx.Response(403, json={"detail": "Not enough credits"}))
    created = client.post("/api/video/generations", json=body(provider="higgsfield", model="seedance-2.0", aspect_ratio="9:16", duration_seconds=5, resolution="720p")).json()
    err = client.get(f"/api/video/generations/{created['id']}").json()["error"]
    assert "Not enough credits" in err and "secret-1" not in err


def test_muapi_end_to_end(client, monkeypatch, http):
    monkeypatch.setattr(settings, "muapi_api_key", "mu-key")
    state = {"polls": 0, "submitted": None}

    def handler(request):
        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=MP4)
        assert request.headers["x-api-key"] == "mu-key"
        if request.method == "POST":
            state["submitted"] = (request.url.path, json.loads(request.content))
            return httpx.Response(200, json={"request_id": "m-1", "status": "processing"})
        assert request.url.path == "/api/v1/predictions/m-1/result"
        state["polls"] += 1
        if state["polls"] < 3:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(200, json={"status": "completed", "outputs": ["https://cdn.example.com/m.mp4"]})

    http(handler)
    payload = body(provider="muapi", model="seedance-v2.0-t2v", aspect_ratio="9:16", duration_seconds=5, resolution="high")
    created = client.post("/api/video/generations", json=payload).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "succeeded" and got["has_file"] is True
    assert state["submitted"] == ("/api/v1/seedance-v2.0-t2v", {"prompt": payload["prompt"], "aspect_ratio": "9:16", "duration": 5, "quality": "high"})


def test_muapi_failure_status(client, monkeypatch, http):
    monkeypatch.setattr(settings, "muapi_api_key", "mu-key")
    http(lambda req: httpx.Response(200, json={"request_id": "m-1"} if req.method == "POST" else {"status": "failed", "error": "bad prompt"}))
    created = client.post("/api/video/generations", json=body(provider="muapi", model="wan2.5-text-to-video", aspect_ratio="9:16", duration_seconds=5, resolution="720p")).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "failed" and "bad prompt" in got["error"]


# ---------- reconcile / timeout / resume ----------


def make_row(db, **over):
    row = VideoGeneration(**{**dict(prompt="p", provider="veo", model="veo-3.1-fast-generate-preview", aspect_ratio="16:9", duration_seconds=8, resolution="720p", status="running", provider_job_id="models/veo/operations/op-9"), **over})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_get_resumes_a_running_job_after_restart(client, db, veo):
    fake = veo(polls_until_done=1)
    row = make_row(db)
    got = client.get(f"/api/video/generations/{row.id}").json()
    assert got["status"] == "succeeded" and got["has_file"] is True
    assert fake.calls["get"] == ["models/veo/operations/op-9"]
    # idempotent: terminal rows are not polled again
    client.get(f"/api/video/generations/{row.id}")
    assert len(fake.calls["get"]) == 1


def test_still_running_job_stays_running_then_times_out(client, db, veo, monkeypatch):
    veo(polls_until_done=99)
    row = make_row(db)
    assert client.get(f"/api/video/generations/{row.id}").json()["status"] == "running"
    monkeypatch.setattr(video_generation, "GENERATION_TIMEOUT_S", -1)
    got = client.get(f"/api/video/generations/{row.id}").json()
    assert got["status"] == "failed" and "Timed out" in got["error"]


def test_finished_job_is_collected_even_if_older_than_timeout(client, db, veo, monkeypatch):
    veo(polls_until_done=1)
    monkeypatch.setattr(video_generation, "GENERATION_TIMEOUT_S", -1)
    row = make_row(db)
    assert client.get(f"/api/video/generations/{row.id}").json()["status"] == "succeeded"


def test_row_that_never_reached_the_provider_fails_after_grace(client, db, veo, monkeypatch):
    veo()
    row = make_row(db, status="queued", provider_job_id=None)
    assert client.get(f"/api/video/generations/{row.id}").json()["status"] == "queued"
    monkeypatch.setattr(video_generation, "SUBMIT_GRACE_S", -1)
    got = client.get(f"/api/video/generations/{row.id}").json()
    assert got["status"] == "failed" and "restarted" in got["error"]


def test_transient_poll_error_keeps_job_running(client, db, hf_keys, http):
    http(lambda req: httpx.Response(503))
    row = make_row(db, provider="higgsfield", model="seedance-2.0", provider_job_id="req-1", provider_status_url="https://api.higgsfield.ai/requests/req-1/status")
    assert client.get(f"/api/video/generations/{row.id}").json()["status"] == "running"


def test_failed_download_is_retried_not_lost(client, db, hf_keys, http):
    calls = {"n": 0}

    def handler(req):
        if req.url.host == "cdn.example.com":
            calls["n"] += 1
            return httpx.Response(500) if calls["n"] == 1 else httpx.Response(200, content=MP4)
        return httpx.Response(200, json={"status": "completed", "video": {"url": "https://cdn.example.com/o.mp4"}})

    http(handler)
    row = make_row(db, provider="higgsfield", model="seedance-2.0", provider_job_id="req-1")
    assert client.get(f"/api/video/generations/{row.id}").json()["status"] == "running"
    assert client.get(f"/api/video/generations/{row.id}").json()["status"] == "succeeded"


# ---------- retry / delete / list / file ----------


def test_retry_only_for_failed_and_creates_new_row(client, db, veo):
    veo()
    failed = make_row(db, status="failed", error="nope", research_sources=json.dumps([{"title": "t", "url": "https://u"}]), original_idea="idea")
    resp = client.post(f"/api/video/generations/{failed.id}/retry")
    assert resp.status_code == 200
    new = resp.json()
    assert new["id"] != failed.id and new["status"] == "queued"
    assert (new["prompt"], new["original_idea"], new["model"], new["research_sources"]) == ("p", "idea", failed.model, [{"title": "t", "url": "https://u"}])
    assert client.get(f"/api/video/generations/{failed.id}").json()["status"] == "failed"  # original untouched
    assert client.get(f"/api/video/generations/{new['id']}").json()["status"] == "succeeded"

    ok = make_row(db, status="succeeded")
    assert client.post(f"/api/video/generations/{ok.id}/retry").status_code == 409
    assert client.post("/api/video/generations/9999/retry").status_code == 404


def test_retry_503_when_provider_key_removed(client, db):
    failed = make_row(db, status="failed")
    assert client.post(f"/api/video/generations/{failed.id}/retry").status_code == 503


def test_delete_removes_row_and_file(client, veo):
    veo()
    created = client.post("/api/video/generations", json=body()).json()
    client.get(f"/api/video/generations/{created['id']}")
    path = video_generation.VIDEOS_DIR / f"{created['id']}.mp4"
    assert path.exists()
    assert client.delete(f"/api/video/generations/{created['id']}").status_code == 204
    assert not path.exists()
    assert client.get(f"/api/video/generations/{created['id']}").status_code == 404
    assert client.delete(f"/api/video/generations/{created['id']}").status_code == 404


def test_list_is_newest_first_and_respects_limit(client, db):
    ids = [make_row(db, status="failed", prompt=f"p{i}").id for i in range(3)]
    listed = client.get("/api/video/generations").json()
    assert [g["id"] for g in listed] == ids[::-1]
    assert len(client.get("/api/video/generations", params={"limit": 2}).json()) == 2


def test_file_endpoint_supports_range(client, veo):
    veo()
    created = client.post("/api/video/generations", json=body()).json()
    client.get(f"/api/video/generations/{created['id']}")
    resp = client.get(f"/api/video/generations/{created['id']}/file", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206 and resp.content == MP4[:4]
    assert resp.headers["content-type"] == "video/mp4" and resp.headers["content-range"].startswith("bytes 0-3/")


def test_file_endpoint_404_without_file_and_redirects_to_hosted_url(client, db):
    row = make_row(db, status="failed")
    assert client.get(f"/api/video/generations/{row.id}/file").status_code == 404
    hosted = make_row(db, status="succeeded", video_url="https://res.cloudinary.com/x/v.mp4", local_path="gone.mp4")
    got = client.get(f"/api/video/generations/{hosted.id}").json()
    assert got["has_file"] is False and got["video_url"] == "https://res.cloudinary.com/x/v.mp4"
    resp = client.get(f"/api/video/generations/{hosted.id}/file", follow_redirects=False)
    assert resp.status_code in (302, 307) and resp.headers["location"] == "https://res.cloudinary.com/x/v.mp4"


def test_cloudinary_upload_when_configured_and_failure_is_ignored(client, veo, monkeypatch):
    veo()
    monkeypatch.setattr(settings, "cloudinary_url", "cloudinary://k:s@c")
    uploaded = []
    monkeypatch.setattr(media_hosting, "upload_video", lambda path, public_id=None: uploaded.append((path, public_id)) or "https://res.cloudinary.com/v.mp4")
    created = client.post("/api/video/generations", json=body()).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["video_url"] == "https://res.cloudinary.com/v.mp4" and got["has_file"] is True
    assert uploaded[0][1] == f"studio-video-{created['id']}"

    monkeypatch.setattr(media_hosting, "upload_video", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cloud down")))
    created = client.post("/api/video/generations", json=body()).json()
    got = client.get(f"/api/video/generations/{created['id']}").json()
    assert got["status"] == "succeeded" and got["video_url"] is None


def test_no_cloudinary_means_no_upload_and_null_video_url(client, veo, monkeypatch):
    veo()
    monkeypatch.setattr(media_hosting, "upload_video", lambda *a, **k: pytest.fail("should skip"))
    created = client.post("/api/video/generations", json=body()).json()
    assert client.get(f"/api/video/generations/{created['id']}").json()["video_url"] is None


def test_unknown_generation_404(client):
    assert client.get("/api/video/generations/404").status_code == 404

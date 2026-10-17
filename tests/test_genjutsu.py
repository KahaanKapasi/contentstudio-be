"""Genjutsu motion transfer: validation, cost guard, every terminal status, resume, retry, delete.
Hermetic: the Higgsfield SDK is faked (no network, no spend); videos are tiny ffmpeg test patterns."""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from higgsfield_client import Cancelled, Completed, Failed, InProgress, NSFW, Queued
from PIL import Image

from app.config import settings
from app.database import SessionLocal
from app.models import CostEvent, GenjutsuJob
from app.services import genjutsu
from app.services.costs import estimate
from app.services.studio.kit import ffmpeg

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg is required for the test videos")

URL = "/api/video/genjutsu"


class FakeSDK:
    def __init__(self, statuses=(Queued(), InProgress(), Completed()), result=None, submit_error=None):
        self.statuses = list(statuses)
        self.result_body = result if result is not None else {"video": {"url": "https://cdn.example/out.mp4"}}
        self.submit_error = submit_error
        self.uploads, self.submitted, self.cancelled = [], None, []

    def upload_file(self, path):
        self.uploads.append(Path(path).name)
        return f"https://hf.example/{Path(path).name}"

    def submit(self, model, arguments):
        if self.submit_error:
            raise self.submit_error
        self.submitted = (model, arguments)
        return SimpleNamespace(request_id="req-1")

    def status(self, request_id):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def result(self, request_id):
        return self.result_body

    def cancel(self, request_id):
        self.cancelled.append(request_id)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "hf_key", "fake-id:fake-secret")
    monkeypatch.setattr(genjutsu, "GENJUTSU_DIR", tmp_path / "genjutsu")
    monkeypatch.setattr(genjutsu, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(genjutsu, "_download", lambda url, dest: dest.write_bytes(b"MP4DATA"))
    monkeypatch.setattr(genjutsu, "_host_copy", lambda path: None)
    with SessionLocal() as db:
        db.query(GenjutsuJob).delete()
        db.query(CostEvent).delete()
        db.commit()
    return tmp_path


def use_sdk(monkeypatch, sdk):
    monkeypatch.setattr(genjutsu, "_sdk", lambda: sdk)
    return sdk


def make_video(tmp_path, seconds=5, name="clip.mp4"):
    path = tmp_path / name
    ffmpeg.run(["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}", "-pix_fmt", "yuv420p", path])
    return path.read_bytes()


def png(color=(200, 90, 60), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def post(client, video, images=None, *, resolution="720p", confirm="50", prompt=""):
    images = images if images is not None else [("a.png", png()), ("b.png", png((10, 200, 30)))]
    files = [("video", ("clip.mp4", video, "video/mp4"))] + [("images", (n, b, "image/png")) for n, b in images]
    return client.post(f"{URL}/jobs", files=files, data={"prompt": prompt, "resolution": resolution, "confirm_cost_usd": confirm})


def events():
    with SessionLocal() as db:
        return db.query(CostEvent).filter(CostEvent.action == "genjutsu.motion_transfer").order_by(CostEvent.id).all()


# --- happy path ---


def test_success_end_to_end(client, env, monkeypatch):
    sdk = use_sdk(monkeypatch, FakeSDK())
    r = post(client, make_video(env), prompt="Replace the man with the person in the photos", confirm="3.41")
    assert r.status_code == 200, r.text
    job = client.get(f"{URL}/jobs/{r.json()['id']}").json()
    assert job["status"] == "succeeded" and job["has_file"] and job["error"] is None
    assert job["billed_seconds"] == 5 and job["est_cost_usd"] == pytest.approx(3.405)
    model, args = sdk.submitted
    assert model == "higgsfield/genjutsu/motion-transfer/v1.0"
    assert args["resolution"] == "720p" and len(args["image_urls"]) == 2 and args["video_url"].startswith("https://hf.example/")
    assert args["prompt"].startswith("Replace the man")
    assert sdk.uploads[0].startswith("input") and sdk.uploads[1:] == ["ref1.jpg", "ref2.jpg"]
    f = client.get(f"{URL}/jobs/{job['id']}/file")
    assert f.status_code == 200 and f.content == b"MP4DATA" and f.headers["content-type"] == "video/mp4"
    assert [e.actual_usd for e in events()] == [pytest.approx(3.405)]


def test_list_is_newest_first(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    a = post(client, make_video(env)).json()["id"]
    b = post(client, make_video(env)).json()["id"]
    assert [j["id"] for j in client.get(f"{URL}/jobs").json()] == [b, a]


# --- cost guard and validation ---


def test_cost_guard_rejects_a_changed_price_and_creates_nothing(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    r = post(client, make_video(env), confirm="1.00")
    assert r.status_code == 409 and "3.40" in r.json()["detail"]
    assert client.get(f"{URL}/jobs").json() == []
    assert not list((env / "genjutsu").glob("[0-9]*"))


@pytest.mark.parametrize("seconds,msg", [(2, "at least 4")])
def test_video_too_short(client, env, monkeypatch, seconds, msg):
    use_sdk(monkeypatch, FakeSDK())
    r = post(client, make_video(env, seconds))
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_unreadable_video_and_image_and_resolution(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    assert post(client, b"not a video").status_code == 400
    r = post(client, make_video(env), images=[("x.png", b"not an image")])
    assert r.status_code == 400 and "x.png" in r.json()["detail"]
    r = post(client, make_video(env), resolution="4k")
    assert r.status_code == 400 and "480p" in r.json()["detail"]


def test_image_count_limits(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    video = make_video(env)
    assert post(client, video, images=[("a.png", png())] * 9).status_code == 400
    assert post(client, video, images=[]).status_code in (400, 422)


def test_not_configured_is_503(client, env, monkeypatch):
    monkeypatch.setattr(settings, "hf_key", "")
    r = post(client, make_video(env))
    assert r.status_code == 503 and "HF_KEY" in r.json()["detail"]
    assert client.get(f"{URL}/status").json()["configured"] is False


def test_separate_id_and_secret_also_count_as_configured(monkeypatch):
    monkeypatch.setattr(settings, "hf_key", "")
    monkeypatch.setattr(settings, "hf_api_key_id", "id")
    monkeypatch.setattr(settings, "hf_api_key_secret", "secret")
    assert genjutsu.credentials() == "id:secret"


# --- terminal statuses are never reported as success ---


@pytest.mark.parametrize(
    "status,expected,word",
    [(NSFW(), "moderated", "moderation"), (Cancelled(), "canceled", "canceled"), (Failed(), "failed", "could not generate")],
)
def test_non_completed_statuses(client, env, monkeypatch, status, expected, word):
    sdk = FakeSDK(statuses=(Queued(), status), result={"error": "boom"})
    use_sdk(monkeypatch, sdk)
    job = post(client, make_video(env)).json()
    job = client.get(f"{URL}/jobs/{job['id']}").json()
    assert job["status"] == expected and word in job["error"].lower() and not job["has_file"]
    assert [e.actual_usd for e in events()] == [0.0]


def test_failed_includes_the_providers_error(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK(statuses=(Failed(),), result={"error": "unsupported source video"}))
    job = client.get(f"{URL}/jobs/{post(client, make_video(env)).json()['id']}").json()
    assert job["status"] == "failed" and "unsupported source video" in job["error"]


def test_empty_balance_is_explained(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK(submit_error=RuntimeError("not_enough_credits")))
    job = client.get(f"{URL}/jobs/{post(client, make_video(env)).json()['id']}").json()
    assert job["status"] == "failed" and "balance is empty" in job["error"]
    assert "not_enough_credits" not in job["error"]


def test_completed_without_a_url_is_a_failure_but_logs_the_spend(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK(result={"detail": "weird"}))
    job = client.get(f"{URL}/jobs/{post(client, make_video(env)).json()['id']}").json()
    assert job["status"] == "failed" and "no video URL" in job["error"]
    assert events()[0].actual_usd == pytest.approx(3.405)  # Higgsfield reported completion, so it was billed


def test_download_failure_keeps_the_link(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())

    def boom(url, dest):
        raise RuntimeError("disk full")

    monkeypatch.setattr(genjutsu, "_download", boom)
    job = client.get(f"{URL}/jobs/{post(client, make_video(env)).json()['id']}").json()
    assert job["status"] == "failed" and "https://cdn.example/out.mp4" in job["error"]


# --- resume, cancel, retry, delete ---


def test_resume_does_not_resubmit(client, env, monkeypatch):
    sdk = use_sdk(monkeypatch, FakeSDK())
    with SessionLocal() as db:
        job = genjutsu.create_job(
            db, video_tmp=_tmp_video(env), video_name="clip.mp4", images=[("a.png", png())], prompt="", resolution="720p", confirm_cost_usd=50,
        )
        job.status, job.request_id = "running", "req-existing"
        db.commit()
        job_id = job.id
    genjutsu.run_job(job_id)
    assert sdk.submitted is None and sdk.uploads == []
    assert client.get(f"{URL}/jobs/{job_id}").json()["status"] == "succeeded"


def _tmp_video(env) -> Path:
    path = env / "genjutsu" / "_tmp" / "t.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_video(env))
    return path


def test_cancel_before_it_was_sent(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    with SessionLocal() as db:
        job = genjutsu.create_job(
            db, video_tmp=_tmp_video(env), video_name="c.mp4", images=[("a.png", png())], prompt="", resolution="480p", confirm_cost_usd=50,
        )
        job_id = job.id
    r = client.post(f"{URL}/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "canceled"


def test_cancel_calls_higgsfield_for_a_submitted_job(client, env, monkeypatch):
    sdk = use_sdk(monkeypatch, FakeSDK())
    with SessionLocal() as db:
        job = genjutsu.create_job(
            db, video_tmp=_tmp_video(env), video_name="c.mp4", images=[("a.png", png())], prompt="", resolution="480p", confirm_cost_usd=50,
        )
        job.status, job.request_id = "running", "req-9"
        db.commit()
        job_id = job.id
    assert client.post(f"{URL}/jobs/{job_id}/cancel").status_code == 200
    assert sdk.cancelled == ["req-9"]


def test_retry_makes_a_new_job_with_the_same_inputs_and_checks_cost(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK(statuses=(Failed(),), result={"error": "x"}))
    first = post(client, make_video(env)).json()
    assert client.get(f"{URL}/jobs/{first['id']}").json()["status"] == "failed"
    assert client.post(f"{URL}/jobs/{first['id']}/retry", data={"confirm_cost_usd": "0.5"}).status_code == 409
    use_sdk(monkeypatch, FakeSDK())
    second = client.post(f"{URL}/jobs/{first['id']}/retry", data={"confirm_cost_usd": "5"})
    assert second.status_code == 200 and second.json()["id"] != first["id"]
    assert client.get(f"{URL}/jobs/{second.json()['id']}").json()["status"] == "succeeded"


def test_retry_only_for_failed_or_canceled(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    done = post(client, make_video(env)).json()
    assert client.post(f"{URL}/jobs/{done['id']}/retry", data={"confirm_cost_usd": "9"}).status_code == 409


def test_delete_removes_files_but_not_a_running_paid_job(client, env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    done = post(client, make_video(env)).json()
    folder = genjutsu.job_dir(done["id"])
    assert folder.exists()
    assert client.delete(f"{URL}/jobs/{done['id']}").status_code == 204
    assert not folder.exists() and client.get(f"{URL}/jobs/{done['id']}").status_code == 404
    with SessionLocal() as db:
        job = genjutsu.create_job(
            db, video_tmp=_tmp_video(env), video_name="c.mp4", images=[("a.png", png())], prompt="", resolution="480p", confirm_cost_usd=50,
        )
        job.status, job.request_id = "running", "req-live"
        db.commit()
        job_id = job.id
    assert client.delete(f"{URL}/jobs/{job_id}").status_code == 409


# --- cost estimator ---


def test_estimate_known_duration_rounds_up_and_caps_at_30s():
    e = estimate.estimate("genjutsu.motion_transfer", {"duration_seconds": 14.2, "resolution": "720p"})
    assert e["low_usd"] == e["high_usd"] == pytest.approx(15 * 0.681)
    e = estimate.estimate("genjutsu.motion_transfer", {"duration_seconds": 95, "resolution": "1080p"})
    assert e["high_usd"] == pytest.approx(30 * 1.632)
    assert estimate.estimate("genjutsu.motion_transfer", {"duration_seconds": 4, "resolution": "480p"})["high_usd"] == pytest.approx(4 * 0.318)


def test_estimate_without_duration_is_a_range():
    e = estimate.estimate("genjutsu.motion_transfer", {"resolution": "720p"})
    assert e["low_usd"] == pytest.approx(4 * 0.681) and e["high_usd"] == pytest.approx(30 * 0.681) and not e["free"]


def test_estimate_rejects_bad_resolution():
    with pytest.raises(ValueError):
        estimate.estimate("genjutsu.motion_transfer", {"resolution": "8k"})


def test_estimate_endpoint(client):
    r = client.post("/api/costs/estimate", json={"action": "genjutsu.motion_transfer", "params": {"duration_seconds": 10, "resolution": "480p"}})
    assert r.status_code == 200 and r.json()["high_usd"] == pytest.approx(3.18)

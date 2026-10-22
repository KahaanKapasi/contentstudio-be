"""Several servers (Render, a laptop, the Latitude) share one database. A Genjutsu job must be finished, and its cost
logged, exactly once, and a server must not start a second poller for a job another server is already polling."""

from datetime import datetime, timedelta, timezone

from higgsfield_client import Completed

from app.database import SessionLocal
from app.models import GenjutsuJob
from app.services import genjutsu

from tests.test_genjutsu import FakeSDK, _tmp_video, env, events, png, use_sdk  # noqa: F401 - fixtures/helpers


def _running_job(env, **fields):
    with SessionLocal() as db:
        job = genjutsu.create_job(
            db, video_tmp=_tmp_video(env), video_name="clip.mp4", images=[("a.png", png())], prompt="", resolution="720p", confirm_cost_usd=50,
        )
        job.status, job.request_id = "running", "req-existing"
        for key, value in fields.items():
            setattr(job, key, value)
        db.commit()
        return job.id


def _ago(seconds):
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)


class _Recorder:
    """Stands in for threading.Thread so a test can see whether a poller would have been started."""

    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.args = args

    def start(self):
        _Recorder.started.append(self.args)


def test_polling_stamps_a_heartbeat(env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    job_id = _running_job(env)
    genjutsu.run_job(job_id)
    with SessionLocal() as db:
        assert db.get(GenjutsuJob, job_id).heartbeat_at is not None


def test_fresh_heartbeat_means_hands_off(env, monkeypatch):
    _Recorder.started = []
    monkeypatch.setattr(genjutsu.threading, "Thread", _Recorder)
    job_id = _running_job(env, heartbeat_at=_ago(5))
    with SessionLocal() as db:
        genjutsu.reconcile(db, db.get(GenjutsuJob, job_id))
    assert _Recorder.started == []


def test_stale_heartbeat_gets_resumed(env, monkeypatch):
    _Recorder.started = []
    monkeypatch.setattr(genjutsu.threading, "Thread", _Recorder)
    job_id = _running_job(env, heartbeat_at=_ago(genjutsu.HEARTBEAT_STALE_S + 30))
    with SessionLocal() as db:
        genjutsu.reconcile(db, db.get(GenjutsuJob, job_id))
    assert _Recorder.started == [(job_id,)]


def test_second_server_finishing_the_same_job_logs_cost_once(env, monkeypatch):
    sdk = use_sdk(monkeypatch, FakeSDK(statuses=[Completed()]))
    job_id = _running_job(env)
    # Both "servers" loaded the row while it was still running, then each tries to finish it.
    with SessionLocal() as server_a, SessionLocal() as server_b:
        job_a, job_b = server_a.get(GenjutsuJob, job_id), server_b.get(GenjutsuJob, job_id)
        genjutsu._fail(server_a, job_a, "failed", "boom", spent=1.0)
        genjutsu._fail(server_b, job_b, "failed", "boom again", spent=1.0)
        assert job_b.status == "failed"
    assert len(events()) == 1
    assert sdk.submitted is None


def test_losing_server_does_not_overwrite_a_success(env, monkeypatch):
    use_sdk(monkeypatch, FakeSDK())
    job_id = _running_job(env)
    with SessionLocal() as winner, SessionLocal() as loser:
        stale = loser.get(GenjutsuJob, job_id)  # loaded while still "running"
        genjutsu.run_job(job_id)  # the winner completes the job
        genjutsu._fail(loser, stale, "failed", "late failure", spent=0.0)
    with SessionLocal() as db:
        job = db.get(GenjutsuJob, job_id)
        assert job.status == "succeeded" and job.error is None
    assert len(events()) == 1

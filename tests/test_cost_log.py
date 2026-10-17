import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import CostEvent
from app.services import discovery
from app.services.costs import ledger
from app.services.gemini_client import GeminiNotConfigured


def add(db, action, actual=None, low=None, high=None, created=None, details=None):
    ev = CostEvent(
        action=action, actual_usd=actual, estimated_low_usd=low, estimated_high_usd=high,
        details=json.dumps(details) if details else None,
        created_at=created or datetime.now(timezone.utc),
    )
    db.add(ev)
    db.commit()
    return ev


def test_events_newest_first_with_labels_and_running_total(client, db):
    now = datetime.now(timezone.utc)
    add(db, "articles.generate", actual=0.10, created=now - timedelta(days=3))
    add(db, "studio.project", actual=0.50, details={"recipe": "3d_explainer"})
    add(db, "video.prompt_improve", low=0.02, high=0.04)
    rows = client.get("/api/costs/events").json()
    assert [r["action"] for r in rows] == ["video.prompt_improve", "studio.project", "articles.generate"]
    assert rows[1]["label"] == "Studio project: 3d explainer"
    assert rows[2]["label"] == "Article generated"
    assert [r["running_total_usd"] for r in rows] == [0.63, 0.6, 0.1]
    assert rows[0]["is_estimate"] is True and rows[0]["est_low_usd"] == 0.02 and rows[0]["actual_usd"] is None
    assert rows[1]["is_estimate"] is False


def test_events_pagination_and_day_filter(client, db):
    now = datetime.now(timezone.utc)
    old = add(db, "articles.generate", actual=1.0, created=now - timedelta(days=40))
    mid = add(db, "articles.generate", actual=2.0, created=now - timedelta(days=5))
    new = add(db, "video.titles", actual=3.0)
    page = client.get("/api/costs/events", params={"limit": 1}).json()
    assert [r["id"] for r in page] == [new.id]
    nxt = client.get("/api/costs/events", params={"limit": 5, "before_id": new.id}).json()
    assert [r["id"] for r in nxt] == [mid.id, old.id] and nxt[0]["running_total_usd"] == 3.0
    assert [r["id"] for r in client.get("/api/costs/events", params={"days": 1}).json()] == [new.id]
    assert [r["id"] for r in client.get("/api/costs/events", params={"days": 7}).json()] == [new.id, mid.id]


def test_total_buckets(client, db):
    now = datetime.now(timezone.utc)
    add(db, "articles.generate", actual=1.0, created=now - timedelta(days=60))
    add(db, "articles.generate", actual=2.0, created=now - timedelta(days=20))
    add(db, "articles.generate", actual=4.0, created=now - timedelta(days=3))
    last = add(db, "video.titles", actual=8.0)
    body = client.get("/api/costs/total").json()
    assert body["all_time_usd"] == 15.0 and body["last_30d_usd"] == 14.0 and body["last_7d_usd"] == 12.0
    assert body["today_usd"] == 8.0
    assert body["count"] == 4 and body["last_event_id"] == last.id and body["any_estimates"] is False
    add(db, "video.prompt_improve", low=0.0, high=0.2)
    body = client.get("/api/costs/total").json()
    assert body["any_estimates"] is True and body["all_time_usd"] == 15.1


def test_total_empty(client):
    body = client.get("/api/costs/total").json()
    assert body["all_time_usd"] == 0 and body["count"] == 0 and body["last_event_id"] is None
    assert client.get("/api/costs/events").json() == []


def test_failed_labels():
    assert ledger.label_for("studio.project", {"recipe": "x", "status": "failed"}) == "Studio project: X (failed)"
    assert ledger.label_for("discovery.scrape", {"status": "skipped"}) == "Discovery scrape (nothing new)"
    assert ledger.label_for("something.new") == "New"


def test_track_paid_logs_partial_spend_when_block_fails(db):
    with pytest.raises(RuntimeError):
        with ledger.track_paid("articles.generate", ref_type="topic", ref_id=1) as usage:
            usage.append({"kind": "text", "grounded": False, "prompt_tokens": 1_000_000, "output_tokens": 0})
            raise RuntimeError("boom")
    ev = db.query(CostEvent).one()
    assert ev.action == "articles.generate" and ev.actual_usd > 0 and json.loads(ev.details)["failed"] is True


def test_track_paid_does_not_log_when_nothing_was_spent(db):
    with pytest.raises(GeminiNotConfigured):
        with ledger.track_paid("articles.generate"):
            raise GeminiNotConfigured("no key")
    assert db.query(CostEvent).count() == 0


def test_instagram_refresh_is_logged_as_free(client, db, monkeypatch):
    from app.services import instagram_client

    monkeypatch.setattr(instagram_client, "get_account_metrics", lambda: {"followers_count": 10})
    monkeypatch.setattr(instagram_client, "get_account_insights", lambda: {"reach": 100, "total_interactions": 5})
    assert client.post("/api/dashboard/instagram/refresh").status_code == 200
    ev = db.query(CostEvent).one()
    assert ev.action == "dashboard.instagram_refresh" and ev.actual_usd == 0.0
    assert client.get("/api/costs/events").json()[0]["label"] == "Instagram metrics refreshed"


def test_discovery_scrape_is_logged_once_with_gemini_and_not_double_counted(client, db, configure_gemini, monkeypatch):
    from tests.test_discovery import source

    monkeypatch.setattr(discovery.rss, "fetch_all_feeds", lambda *a, **k: [source("bbc", "News A")])
    monkeypatch.setattr(discovery.twitter, "fetch_topic", lambda *a, **k: source("twitter", ok=False, error="off"))
    monkeypatch.setattr(discovery.instagram, "fetch_topic", lambda: source("instagram", ok=False, error="off"))

    def fake_json(prompt, **kw):
        from app.services.gemini_client import _usage

        _usage.get().append({"kind": "text", "grounded": False, "prompt_tokens": 1000, "output_tokens": 500})
        return []

    monkeypatch.setattr(discovery, "generate_json", fake_json)
    client.post("/api/discovery/scrape")
    events = db.query(CostEvent).all()
    assert len(events) == 1 and events[0].actual_usd > 0
    assert client.get("/api/costs/total").json()["count"] == 1


def test_failed_video_generation_is_logged_free_before_submit_and_estimated_after(db):
    from app.models import VideoGeneration
    from app.services import video_generation

    def gen(job_id):
        g = VideoGeneration(prompt="p", provider="veo", model="veo-3.1-fast-generate-preview", aspect_ratio="16:9",
                            duration_seconds=8, resolution="720p", status="running", provider_job_id=job_id)
        db.add(g)
        db.commit()
        return g

    video_generation._fail(db, gen(None), "rejected")
    video_generation._fail(db, gen("job-1"), "poll failed")
    free, maybe = db.query(CostEvent).order_by(CostEvent.id).all()
    assert free.actual_usd == 0.0 and json.loads(free.details)["failed"] is True
    assert maybe.actual_usd is None and maybe.estimated_high_usd  # may have been billed: the estimate counts

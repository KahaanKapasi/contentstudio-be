from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import Article, InstagramMetricSnapshot, PostDraft, TwitterMetricSnapshot, TwitterPostSuggestion
from app.routers import dashboard as dashboard_router


def days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


def test_kpi_baseline_is_null_until_set(client):
    resp = client.get("/api/dashboard/kpi-baseline")
    assert resp.status_code == 200 and resp.json() is None


def test_put_then_get_kpi_baseline(client):
    payload = {"label": "Trailing 30 days", "posts_per_week": 3.5, "avg_engagement_rate": 0.042}
    put = client.put("/api/dashboard/kpi-baseline", json=payload)
    assert put.status_code == 200
    got = client.get("/api/dashboard/kpi-baseline").json()
    assert got["id"] == put.json()["id"]
    assert {k: got[k] for k in payload} == payload


def test_put_baseline_keeps_history_and_get_returns_latest(client, db):
    from app.models import KpiBaseline

    client.put("/api/dashboard/kpi-baseline", json={"label": "old", "posts_per_week": 1})
    client.put("/api/dashboard/kpi-baseline", json={"label": "new", "posts_per_week": 2})
    assert db.query(KpiBaseline).count() == 2
    assert client.get("/api/dashboard/kpi-baseline").json()["label"] == "new"


def test_put_baseline_engagement_rate_is_optional(client):
    got = client.put("/api/dashboard/kpi-baseline", json={"label": "x", "posts_per_week": 2}).json()
    assert got["avg_engagement_rate"] is None


def test_put_baseline_requires_label_and_rate(client):
    assert client.put("/api/dashboard/kpi-baseline", json={"label": "x"}).status_code == 422


def test_kpi_summary_empty(client):
    assert client.get("/api/dashboard/kpi-summary").json() == {
        "since_studio_adoption": {"articles_published": 0, "posts_published": 0, "posts_per_week": None},
        "pre_studio_baseline": None,
    }


def test_kpi_summary_computes_posts_per_week_from_published_items(client, db):
    db.add_all(
        [
            Article(title="a1", status="published", created_at=days_ago(14)),
            Article(title="a2", status="published", created_at=days_ago(3)),
            Article(title="draft", status="draft", created_at=days_ago(1)),
            PostDraft(status="published", created_at=days_ago(2)),
            PostDraft(status="published", created_at=days_ago(1)),
            PostDraft(status="draft", created_at=days_ago(1)),
        ]
    )
    db.commit()
    summary = client.get("/api/dashboard/kpi-summary").json()["since_studio_adoption"]
    assert summary["articles_published"] == 2
    assert summary["posts_published"] == 2
    assert summary["posts_per_week"] == 2.0


def test_kpi_summary_uses_earliest_record_from_either_table(client, db):
    db.add_all(
        [
            Article(title="a", status="published", created_at=days_ago(7)),
            PostDraft(status="published", created_at=days_ago(28)),
        ]
    )
    db.commit()
    assert client.get("/api/dashboard/kpi-summary").json()["since_studio_adoption"]["posts_per_week"] == 0.5


def test_kpi_summary_cadence_none_when_nothing_published(client, db):
    db.add(Article(title="a", status="draft", created_at=days_ago(30)))
    db.commit()
    assert client.get("/api/dashboard/kpi-summary").json()["since_studio_adoption"]["posts_per_week"] is None


def test_kpi_summary_treats_first_day_as_at_least_one_day(client, db):
    db.add(Article(title="a", status="published", created_at=datetime.now(timezone.utc)))
    db.commit()
    assert client.get("/api/dashboard/kpi-summary").json()["since_studio_adoption"]["posts_per_week"] == 7.0


def test_kpi_summary_includes_latest_baseline(client):
    client.put("/api/dashboard/kpi-baseline", json={"label": "old", "posts_per_week": 1})
    client.put("/api/dashboard/kpi-baseline", json={"label": "pre", "posts_per_week": 4, "avg_engagement_rate": 0.03})
    assert client.get("/api/dashboard/kpi-summary").json()["pre_studio_baseline"] == {
        "label": "pre",
        "posts_per_week": 4.0,
        "avg_engagement_rate": 0.03,
    }


def test_instagram_refresh_503_when_unconfigured(client):
    resp = client.post("/api/dashboard/instagram/refresh")
    assert resp.status_code == 503 and "IG_ACCESS_TOKEN" in resp.json()["detail"]
    assert client.get("/api/dashboard/instagram/metrics").json() == []


def test_instagram_refresh_stores_snapshot(client, monkeypatch):
    monkeypatch.setattr(dashboard_router.instagram_client, "get_account_metrics", lambda: {"followers_count": 1234})
    body = client.post("/api/dashboard/instagram/refresh").json()
    assert body["followers"] == 1234 and body["top_post_ids"] == []
    assert [m["followers"] for m in client.get("/api/dashboard/instagram/metrics").json()] == [1234]


def test_twitter_refresh_503_when_unconfigured(client):
    resp = client.post("/api/dashboard/twitter/refresh", params={"username": "madridonomy"})
    assert resp.status_code == 503 and "X_BEARER_TOKEN" in resp.json()["detail"]


def test_twitter_refresh_requires_username(client):
    assert client.post("/api/dashboard/twitter/refresh").status_code == 422


def test_twitter_refresh_stores_snapshot(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        dashboard_router.twitter_client,
        "get_account_metrics",
        lambda username: seen.update(u=username) or {"followers_count": 77},
    )
    body = client.post("/api/dashboard/twitter/refresh", params={"username": "abc"}).json()
    assert seen["u"] == "abc" and body["followers"] == 77
    assert [m["followers"] for m in client.get("/api/dashboard/twitter/metrics").json()] == [77]


def test_metric_histories_are_ordered_oldest_first(client, db):
    db.add_all(
        [
            InstagramMetricSnapshot(followers=2, captured_at=days_ago(1)),
            InstagramMetricSnapshot(followers=1, captured_at=days_ago(5)),
            TwitterMetricSnapshot(followers=20, captured_at=days_ago(1)),
            TwitterMetricSnapshot(followers=10, captured_at=days_ago(5)),
        ]
    )
    db.commit()
    assert [m["followers"] for m in client.get("/api/dashboard/instagram/metrics").json()] == [1, 2]
    assert [m["followers"] for m in client.get("/api/dashboard/twitter/metrics").json()] == [10, 20]


def test_generate_suggestions_returns_empty_without_topics_and_without_gemini(client):
    resp = client.post("/api/dashboard/twitter/suggestions/generate")
    assert resp.status_code == 200 and resp.json() == []


def test_generate_suggestions_503_when_gemini_unconfigured(client, make_topic):
    make_topic()
    assert client.post("/api/dashboard/twitter/suggestions/generate").status_code == 503


def test_generate_suggestions_persists_mocked_gemini_output(client, make_topic, monkeypatch):
    kept = make_topic("Kept")
    make_topic("Dropped", status="discarded")
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return [{"topic_id": kept.id, "draft_text": "Hot take"}, {"draft_text": "No topic"}]

    monkeypatch.setattr(dashboard_router, "generate_json", fake)
    resp = client.post("/api/dashboard/twitter/suggestions/generate")
    assert resp.status_code == 200
    assert [(s["topic_id"], s["draft_text"], s["status"]) for s in resp.json()] == [
        (kept.id, "Hot take", "suggested"),
        (None, "No topic", "suggested"),
    ]
    assert "Kept" in prompts[0] and "Dropped" not in prompts[0]
    assert len(client.get("/api/dashboard/twitter/suggestions").json()) == 2


def test_post_suggestion_503_when_twitter_unconfigured_and_status_unchanged(client, db):
    s = TwitterPostSuggestion(draft_text="hi")
    db.add(s)
    db.commit()
    resp = client.post(f"/api/dashboard/twitter/suggestions/{s.id}/post")
    assert resp.status_code == 503
    db.expire_all()
    assert db.get(TwitterPostSuggestion, s.id).status == "suggested"


def test_post_suggestion_marks_posted(client, db, monkeypatch):
    sent = []
    monkeypatch.setattr(dashboard_router.twitter_client, "post_tweet", lambda text: sent.append(text) or "tweet-1")
    s = TwitterPostSuggestion(draft_text="hello world")
    db.add(s)
    db.commit()
    assert client.post(f"/api/dashboard/twitter/suggestions/{s.id}/post").json()["status"] == "posted"
    assert sent == ["hello world"]


def test_post_unknown_suggestion_is_404(client):
    assert client.post("/api/dashboard/twitter/suggestions/5/post").status_code == 404


def test_post_tweet_requires_all_oauth1_credentials(monkeypatch):
    from app.services import twitter_client

    monkeypatch.setattr(settings, "x_api_key", "k")
    with pytest.raises(twitter_client.TwitterNotConfigured):
        twitter_client.post_tweet("x")

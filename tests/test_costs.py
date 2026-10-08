import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import CostEvent
from app.services import gemini_client
from app.services.costs import estimate as est
from app.services.costs import ledger, prices


def test_every_action_estimates_and_has_sane_shape():
    params = {"video.generation": {"provider": "veo", "model": "veo-3.1-fast-generate-preview", "resolution": "720p", "duration_seconds": 8},
              "studio.project": {"engine": "faceless", "recipe": "faceless", "params": {}}}
    for action in est.ACTIONS:
        e = est.estimate(action, params.get(action, {}))
        assert e["action"] == action and e["currency"] == "USD"
        assert 0 <= e["low_usd"] <= e["high_usd"]
        assert e["free"] == (e["high_usd"] == 0)
        assert e["confidence"] in ("official", "mixed", "unknown")
        for row in e["breakdown"]:
            assert {"item", "qty_low", "qty_high", "unit", "unit_usd", "low_usd", "high_usd"} <= set(row)


@pytest.mark.parametrize("action", ["posts.render_background", "posts.render_preview", "posts.upload_to_host", "posts.publish", "dashboard.instagram_refresh"])
def test_free_actions(action):
    e = est.estimate(action)
    assert e["free"] and e["low_usd"] == e["high_usd"] == 0 and e["notes"]


def test_gemini_text_actions_are_small_ranges():
    for action in ("articles.generate", "articles.regenerate", "video.titles", "video.scripts", "video.prompt_improve", "dashboard.twitter_suggestions"):
        e = est.estimate(action, {})
        assert 0 < e["low_usd"] < e["high_usd"] < 0.2, action
    assert est.estimate("articles.generate")["high_usd"] > est.estimate("video.titles")["high_usd"]


def test_discovery_scales_with_items_and_x_reads(monkeypatch):
    small = est.estimate("discovery.scrape", {"n_items": 5, "include_x": False})
    big = est.estimate("discovery.scrape", {"n_items": 60, "include_x": False})
    assert big["high_usd"] > small["high_usd"]
    with_x = est.estimate("discovery.scrape", {"n_items": 60, "include_x": True})
    assert with_x["high_usd"] == pytest.approx(big["high_usd"] + 20 * 0.005)
    monkeypatch.setattr(settings, "x_bearer_token", "t")
    assert est.estimate("discovery.scrape", {"n_items": 60})["high_usd"] == with_x["high_usd"]


def test_prompt_improve_research_adds_grounding_fee():
    plain = est.estimate("video.prompt_improve", {"research": False})
    grounded = est.estimate("video.prompt_improve", {"research": True})
    assert any(r["price_id"] == "gemini.grounding" for r in grounded["breakdown"])
    assert grounded["high_usd"] > plain["high_usd"] + 0.014


def test_video_generation_veo_uses_catalog_price():
    e = est.estimate("video.generation", {"provider": "veo", "model": "veo-3.1-fast-generate-preview", "resolution": "720p", "duration_seconds": 8})
    assert e["low_usd"] == e["high_usd"] == pytest.approx(0.80) and e["confidence"] == "official"
    e = est.estimate("video.generation", {"provider": "veo", "model": "veo-3.1-lite-generate-preview", "resolution": "1080p", "duration_seconds": 8})
    assert e["high_usd"] == pytest.approx(0.64)


def test_video_generation_unknown_price_is_a_range():
    e = est.estimate("video.generation", {"provider": "muapi", "model": "wan2.5-text-to-video", "resolution": "720p", "duration_seconds": 5})
    assert e["confidence"] == "unknown" and e["low_usd"] == pytest.approx(0.25) and e["high_usd"] == pytest.approx(2.0)
    h = est.estimate("video.generation", {"provider": "higgsfield", "model": "seedance-2.0", "resolution": "720p", "duration_seconds": 5})
    assert h["confidence"] == "unknown" and h["low_usd"] < h["high_usd"]


def test_video_generation_bad_model_is_value_error():
    with pytest.raises(ValueError):
        est.estimate("video.generation", {"provider": "veo", "model": "nope", "resolution": "720p", "duration_seconds": 8})


def test_twitter_post_url_costs_more():
    assert est.estimate("dashboard.twitter_post", {"text": "hi"})["high_usd"] == pytest.approx(0.015)
    assert est.estimate("dashboard.twitter_post", {"text": "see https://x.co/a"})["high_usd"] == pytest.approx(0.20)
    unknown = est.estimate("dashboard.twitter_post", {})
    assert unknown["low_usd"] == pytest.approx(0.015) and unknown["high_usd"] == pytest.approx(0.20)
    assert est.estimate("dashboard.twitter_refresh")["high_usd"] == pytest.approx(0.01)
    assert est.estimate("posts.match_scrape")["high_usd"] > 0.05


def test_studio_project_estimates():
    free = est.estimate("studio.project", {"engine": "faceless", "recipe": "faceless", "params": {}})
    assert 0 < free["high_usd"] < 0.1  # only Gemini text for the script
    film = est.estimate("studio.project", {"engine": "shorts", "recipe": "film", "params": {"scenes": 5, "motion_quality": "ai"}})
    assert film["low_usd"] > 0.3 and film["high_usd"] > film["low_usd"]
    assert any("images" in r["item"] for r in film["breakdown"])
    story = est.estimate("studio.project", {"engine": "skill", "recipe": "singing-story", "params": {"lines": 8}})
    assert story["low_usd"] > 0.3 and story["high_usd"] > story["low_usd"]
    dance = est.estimate("studio.project", {"engine": "shorts", "recipe": "dance", "params": {}, "trend_seconds": 10})
    assert dance["high_usd"] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        est.estimate("studio.project", {"engine": "nope"})


def test_studio_point_value_is_widened():
    e = est.estimate("studio.project", {"engine": "skill", "recipe": "singing-story", "params": {}, "estimated_usd": 1.0})
    assert e["low_usd"] == pytest.approx(0.75) and e["high_usd"] == pytest.approx(1.25)


def test_studio_images_use_price_table_not_old_constant():
    from app.services.studio.engines import _scenefilm, _skill_common

    assert _scenefilm.IMAGE_COST_USD == _skill_common.IMAGE_COST_USD == 0.067


def test_gemini_prices_double_on_2027():
    before = est.estimate("articles.generate", {}, on=date(2026, 12, 31))
    after = est.estimate("articles.generate", {}, on=date(2027, 1, 1))
    assert after["high_usd"] == pytest.approx(before["high_usd"] * 2)
    assert prices.usd("gemini.flash.text.in", "2026-10-09") == 0.75
    assert prices.usd("gemini.flash.text.in", "2027-01-01") == 1.50
    assert prices.usd("gemini.tts.3_8.out", date(2027, 6, 1)) == 18.0
    assert prices.usd("gemini.image.out", date(2027, 6, 1)) == 0.067  # images do not change


def test_price_overrides(monkeypatch):
    monkeypatch.setattr(settings, "price_overrides_json", json.dumps({"x.user_lookup": 0.5}))
    assert est.estimate("dashboard.twitter_refresh")["high_usd"] == pytest.approx(0.5)
    monkeypatch.setattr(settings, "price_overrides_json", "not json")
    assert est.estimate("dashboard.twitter_refresh")["high_usd"] == pytest.approx(0.01)


def test_unknown_action():
    with pytest.raises(est.UnknownAction):
        est.estimate("nope.nothing")


# --- API ---


def test_estimate_endpoint(client):
    r = client.post("/api/costs/estimate", json={"action": "articles.generate"})
    assert r.status_code == 200 and r.json()["high_usd"] > 0
    assert client.post("/api/costs/estimate", json={"action": "posts.publish"}).json()["free"] is True


def test_estimate_endpoint_unknown_action_and_bad_params_are_400(client):
    assert client.post("/api/costs/estimate", json={"action": "nope"}).status_code == 400
    bad = client.post("/api/costs/estimate", json={"action": "video.generation", "params": {"provider": "veo", "model": "x"}})
    assert bad.status_code == 400


def test_prices_endpoint(client):
    body = client.get("/api/costs/prices").json()
    row = next(p for p in body["prices"] if p["id"] == "gemini.flash.text.in")
    assert row["source_url"].startswith("https://") and row["verified_on"] and row["upcoming"][0]["effective_from"] == "2027-01-01"
    assert any(p["confidence"] == "unknown" and p["low_usd"] < p["high_usd"] for p in body["prices"])


def test_costs_routes_are_behind_the_access_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "local_access_password", "secret")
    assert client.post("/api/costs/estimate", json={"action": "posts.publish"}).status_code == 401
    assert client.get("/api/costs/summary").status_code == 401
    assert client.get("/api/costs/prices").status_code == 401


# --- ledger ---


class _FakeModels:
    def generate_content(self, model, contents, config=None):
        text = '{"title": "T", "meta_description": "m", "tags": ["a"]}' if config else "body text"
        meta = SimpleNamespace(prompt_token_count=1000, candidates_token_count=400, thoughts_token_count=100, tool_use_prompt_token_count=None)
        return SimpleNamespace(text=text, usage_metadata=meta, candidates=[])


def test_article_generation_writes_ledger_event_with_actual_cost(client, db, make_topic, configure_gemini, monkeypatch):
    monkeypatch.setattr(gemini_client, "get_client", lambda: SimpleNamespace(models=_FakeModels()))
    topic = make_topic()
    r = client.post("/api/articles/generate", json={"topic_id": topic.id})
    assert r.status_code == 200
    ev = db.query(CostEvent).one()
    assert ev.action == "articles.generate" and ev.ref_type == "article" and ev.ref_id == r.json()["id"]
    # 3 calls x (1000 in, 500 out incl. thinking) at 0.75 / 3.75 per 1M tokens
    expected = 3 * (1000 / 1e6 * 0.75 + 500 / 1e6 * 3.75)
    assert ev.actual_usd == pytest.approx(expected, rel=1e-3)
    assert 0 < ev.estimated_low_usd < ev.estimated_high_usd
    assert json.loads(ev.details)["tokens"]["calls"] == 3


def test_failed_action_writes_no_event(client, db, make_topic):
    topic = make_topic()
    assert client.post("/api/articles/generate", json={"topic_id": topic.id}).status_code == 503
    assert db.query(CostEvent).count() == 0


def test_ledger_failure_never_breaks_the_action(client, make_topic, monkeypatch):
    from app.routers import articles

    monkeypatch.setattr(ledger, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(articles.article_pipeline, "generate_article", lambda db, tid: make_article_row(db, tid))
    topic = make_topic()
    assert client.post("/api/articles/generate", json={"topic_id": topic.id}).status_code == 200


def make_article_row(db, topic_id):
    from app.models import Article

    a = Article(topic_id=topic_id, title="t", body="b")
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_record_usage_ignores_missing_or_mock_metadata():
    with gemini_client.track_usage() as usage:
        gemini_client.record_usage(SimpleNamespace(text="x"))  # no usage_metadata
        gemini_client.record_usage(SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count="many")))
    assert usage == [{"kind": "text", "grounded": False, "prompt_tokens": 0, "output_tokens": 0}]
    gemini_client.record_usage(SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=5)))  # no tracker: no-op


def test_summary_aggregates_actual_and_estimated(client, db):
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            CostEvent(action="articles.generate", actual_usd=0.02, estimated_low_usd=0.01, estimated_high_usd=0.05, created_at=now),
            CostEvent(action="articles.generate", actual_usd=None, estimated_low_usd=0.02, estimated_high_usd=0.06, created_at=now - timedelta(days=1)),
            CostEvent(action="video.generation", actual_usd=0.8, details=json.dumps({"provider": "veo"}), created_at=now),
            CostEvent(action="dashboard.twitter_post", estimated_low_usd=0.015, estimated_high_usd=0.015, created_at=now - timedelta(days=3)),
            CostEvent(action="articles.generate", actual_usd=9.0, created_at=now - timedelta(days=60)),  # outside window
        ]
    )
    db.commit()
    s = client.get("/api/costs/summary?days=30").json()
    assert s["total_usd"] == pytest.approx(0.02 + 0.04 + 0.8 + 0.015)
    assert s["estimated_usd"] == pytest.approx(0.04 + 0.015)
    assert s["event_count"] == 4
    services = {r["service"]: r["usd"] for r in s["by_service"]}
    assert services == pytest.approx({"gemini": 0.06, "veo": 0.8, "x": 0.015})
    art = next(a for a in s["by_action"] if a["action"] == "articles.generate")
    assert art["count"] == 2 and art["estimated_count"] == 1
    assert len(s["daily"]) == 30 and s["daily"][-1]["usd"] == pytest.approx(0.82)
    assert sum(d["usd"] for d in s["daily"]) == pytest.approx(s["total_usd"])


def test_summary_empty(client):
    s = client.get("/api/costs/summary?days=7").json()
    assert s["total_usd"] == 0 and len(s["daily"]) == 7 and s["by_service"] == []

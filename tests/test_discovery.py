import httpx
import pytest

from app.models import CostEvent, ScrapedItem, TopicCandidate, TopicSourceLink
from app.services import discovery
from app.services.gemini_client import GeminiNotConfigured
from app.services.scraping import rss
from app.services.scraping.base import RawItem, SourceResult


def rss_xml(*entries):
    items = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description></item>" for t, l, d in entries
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'.encode()


def source(name, *texts, ok=True, error=None):
    return SourceResult(
        source_name=name,
        items=[RawItem(source=name, source_url=f"http://x/{name}/{i}", raw_text=t) for i, t in enumerate(texts)],
        ok=ok,
        error=error,
    )


@pytest.fixture
def stub_sources(monkeypatch):
    def _stub(rss_results, twitter_result=None, instagram_result=None):
        monkeypatch.setattr(discovery.rss, "fetch_all_feeds", lambda: rss_results)
        monkeypatch.setattr(discovery.twitter, "fetch_topic", lambda: twitter_result or source("twitter", ok=False, error="off"))
        monkeypatch.setattr(discovery.instagram, "fetch_topic", lambda: instagram_result or source("instagram", ok=False, error="off"))

    return _stub


def test_fetch_feed_success_builds_items(monkeypatch):
    xml = rss_xml(("Bellingham scores", "http://n/1", "A late winner"), ("Mbappe injury", "http://n/2", ""))
    monkeypatch.setattr(rss, "_download", lambda url: xml)
    result = rss.fetch_feed("bbc", "http://feed")
    assert result.ok and result.error is None
    assert [i.source for i in result.items] == ["news_rss:bbc", "news_rss:bbc"]
    assert result.items[0].source_url == "http://n/1"
    assert result.items[0].raw_text == "Bellingham scores\nA late winner"
    assert result.items[1].raw_text == "Mbappe injury"


def test_fetch_feed_respects_limit(monkeypatch):
    xml = rss_xml(*[(f"t{i}", f"http://n/{i}", "") for i in range(10)])
    monkeypatch.setattr(rss, "_download", lambda url: xml)
    assert len(rss.fetch_feed("f", "u", limit=3).items) == 3


def test_fetch_feed_unparseable_content_returns_not_ok(monkeypatch):
    monkeypatch.setattr(rss, "_download", lambda url: b"<html><body>not a feed</body>")
    result = rss.fetch_feed("bad", "u")
    assert result.ok is False
    assert result.items == []
    assert result.error


def test_fetch_feed_download_error_does_not_raise(monkeypatch):
    def boom(url):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(rss, "_download", boom)
    result = rss.fetch_feed("down", "u")
    assert result.ok is False
    assert "no route" in result.error


def test_fetch_all_feeds_isolates_failures(monkeypatch):
    def fake_download(url):
        if "bad" in url:
            raise httpx.ConnectError("boom")
        return rss_xml(("Headline", "http://n/1", "body"))

    monkeypatch.setattr(rss, "_download", fake_download)
    results = rss.fetch_all_feeds([("a", "http://good/a"), ("b", "http://bad/b"), ("c", "http://good/c")])
    assert [(r.source_name, r.ok) for r in results] == [("a", True), ("b", False), ("c", True)]


def test_fetch_all_feeds_defaults_to_default_feeds(monkeypatch):
    monkeypatch.setattr(rss, "DEFAULT_FEEDS", [("one", "http://one"), ("two", "http://two")])
    monkeypatch.setattr(rss, "_download", lambda url: rss_xml(("T", "http://n", "")))
    assert [r.source_name for r in rss.fetch_all_feeds()] == ["one", "two"]


def test_collect_dedupes_identical_items_and_persists(db, stub_sources):
    stub_sources(
        [
            source("bbc", "Madrid win 3-0", "Transfer rumour"),
            source("sky", "  madrid WIN 3-0 ", "Another story", ""),
        ]
    )
    out = discovery.collect(db)
    rows = db.query(ScrapedItem).order_by(ScrapedItem.id).all()
    assert [r.raw_text for r in rows] == ["Madrid win 3-0", "Transfer rumour", "Another story"]
    assert out["scraped_item_ids"] == [r.id for r in rows]
    assert rows[0].source == "bbc" and rows[0].source_url == "http://x/bbc/0"


def test_collect_reports_source_health_and_survives_failed_sources(db, stub_sources):
    stub_sources(
        [source("good", "story one"), source("broken", ok=False, error="timeout")],
        twitter_result=source("twitter", "a tweet"),
    )
    out = discovery.collect(db)
    health = {h["source"]: h for h in out["source_health"]}
    assert health["good"] == {"source": "good", "ok": True, "item_count": 1, "error": None}
    assert health["broken"]["ok"] is False and health["broken"]["error"] == "timeout"
    assert health["instagram"]["ok"] is False
    assert db.query(ScrapedItem).count() == 2


def test_collect_ignores_items_from_failed_sources(db, stub_sources):
    stub_sources([source("partial", "should be ignored", ok=False, error="half-parsed")])
    assert discovery.collect(db)["scraped_item_ids"] == []
    assert db.query(ScrapedItem).count() == 0


def seed_items(db, n=3):
    rows = [ScrapedItem(source="s", source_url=f"http://x/{i}", raw_text=f"item {i}") for i in range(n)]
    db.add_all(rows)
    db.commit()
    return rows


def test_generate_topics_creates_topics_and_source_links(db, configure_gemini, monkeypatch):
    items = seed_items(db, 3)
    seen_prompts = []

    def fake_json(prompt, **kw):
        seen_prompts.append(prompt)
        return [
            {"title": "T1", "rationale": "r1", "suitable_for": "video", "source_indices": [0, 1]},
            {"title": "T2", "rationale": "r2", "suitable_for": "article", "source_indices": [2]},
        ]

    monkeypatch.setattr(discovery, "generate_json", fake_json)
    topics = discovery.generate_topics(db, scraped_item_ids=[i.id for i in items])

    assert [t.title for t in topics] == ["T1", "T2"]
    assert all(t.status == "new" for t in topics)
    assert topics[0].suitable_for == "video"
    assert "item 0" in seen_prompts[0]

    by_topic = {}
    for link in db.query(TopicSourceLink).all():
        by_topic.setdefault(link.topic_id, set()).add(link.scraped_item_id)
    ids_desc = [i.id for i in sorted(items, key=lambda r: r.id, reverse=True)]
    assert by_topic[topics[0].id] == {ids_desc[0], ids_desc[1]}
    assert by_topic[topics[1].id] == {ids_desc[2]}


def test_generate_topics_ignores_out_of_range_source_indices(db, monkeypatch):
    seed_items(db, 2)
    monkeypatch.setattr(
        discovery,
        "generate_json",
        lambda prompt, **kw: [{"title": "T", "rationale": "r", "suitable_for": "both", "source_indices": [-1, 0, 2, 99]}],
    )
    discovery.generate_topics(db)
    assert db.query(TopicSourceLink).count() == 1


def test_generate_topics_applies_defaults_for_missing_fields(db, monkeypatch):
    seed_items(db, 1)
    monkeypatch.setattr(discovery, "generate_json", lambda prompt, **kw: [{}])
    topic = discovery.generate_topics(db)[0]
    assert (topic.title, topic.suitable_for, topic.rationale) == ("Untitled", "both", "")
    assert db.query(TopicSourceLink).count() == 0


def test_generate_topics_without_scraped_items_skips_gemini(db, monkeypatch):
    def fail(prompt, **kw):
        raise AssertionError("Gemini must not be called with no items")

    monkeypatch.setattr(discovery, "generate_json", fail)
    assert discovery.generate_topics(db) == []


def test_generate_topics_only_uses_requested_item_ids(db, monkeypatch):
    items = seed_items(db, 3)
    prompts = []
    monkeypatch.setattr(discovery, "generate_json", lambda p, **kw: prompts.append(p) or [])
    discovery.generate_topics(db, scraped_item_ids=[items[0].id])
    assert "item 0" in prompts[0] and "item 1" not in prompts[0]


def test_generate_topics_propagates_gemini_not_configured(db):
    seed_items(db, 1)
    with pytest.raises(GeminiNotConfigured):
        discovery.generate_topics(db)


def test_scrape_endpoint_reports_error_when_gemini_unconfigured(client, stub_sources):
    stub_sources([source("bbc", "Some news")])
    resp = client.post("/api/discovery/scrape")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error" and body["new_items"] == 1
    assert body["topics_created"] == 0
    assert "GEMINI_API_KEY" in body["error"]
    assert any(h["source"] == "bbc" and h["ok"] for h in body["source_health"])


def test_scrape_endpoint_creates_topics_with_mocked_gemini(client, stub_sources, configure_gemini, monkeypatch):
    stub_sources([source("bbc", "Some news")])
    monkeypatch.setattr(
        discovery,
        "generate_json",
        lambda p, **kw: [{"title": "T", "rationale": "r", "suitable_for": "both", "source_indices": [0]}],
    )
    body = client.post("/api/discovery/scrape").json()
    assert body["status"] == "generating" and body["new_items"] == 1
    # TestClient runs the background task before returning, so the run is finished by now.
    run = client.get(f"/api/discovery/runs/{body['run_id']}").json()
    assert run["status"] == "done" and run["topics_created"] == 1 and run["finished_at"]
    assert client.get("/api/discovery/runs/latest").json()["id"] == body["run_id"]
    assert [t["title"] for t in client.get("/api/discovery/topics").json()] == ["T"]


def test_second_scrape_without_new_items_skips_gemini_and_logs_zero(client, stub_sources, configure_gemini, monkeypatch, db):
    stub_sources([source("bbc", "Some news")])
    calls = []
    monkeypatch.setattr(discovery, "generate_json", lambda p, **kw: calls.append(p) or [])
    client.post("/api/discovery/scrape")
    body = client.post("/api/discovery/scrape").json()
    assert len(calls) == 1
    assert body["status"] == "skipped" and body["new_items"] == 0 and "No new items" in body["message"]
    events = db.query(CostEvent).filter(CostEvent.action == "discovery.scrape").order_by(CostEvent.id).all()
    assert len(events) == 2 and events[1].actual_usd == 0.0


def test_collect_only_saves_new_items_and_dedupes_by_url(db, stub_sources):
    stub_sources([source("bbc", "Madrid win", "Transfer rumour")])
    assert discovery.collect(db)["new_items"] == 2
    # same URLs, edited text -> still known; one genuinely new item
    again = SourceResult(
        source_name="bbc",
        items=[
            RawItem("bbc", "http://x/bbc/0?utm=1", "Madrid win (updated)"),
            RawItem("bbc", "http://x/bbc/9", "Brand new story"),
        ],
        ok=True,
    )
    stub_sources([again])
    out = discovery.collect(db)
    assert out["new_items"] == 1 and db.query(ScrapedItem).count() == 3


def test_generate_topics_caps_batch_and_trims_items(db, monkeypatch):
    rows = [ScrapedItem(source="s", source_url=f"http://x/{i}", raw_text=f"n{i} " + "z" * 500) for i in range(50)]
    db.add_all(rows)
    db.commit()
    prompts = []
    monkeypatch.setattr(discovery, "generate_json", lambda p, **kw: prompts.append(p) or [])
    discovery.generate_topics(db, scraped_item_ids=[r.id for r in rows])
    assert prompts[0].count("] (s)") == discovery.MAX_NEW_ITEMS
    assert "z" * 301 not in prompts[0]


def test_generation_failure_is_recorded_on_the_run(client, stub_sources, configure_gemini, monkeypatch):
    stub_sources([source("bbc", "Some news")])

    def boom(p, **kw):
        raise RuntimeError("429 RESOURCE_EXHAUSTED spend cap")

    monkeypatch.setattr(discovery, "generate_json", boom)
    body = client.post("/api/discovery/scrape").json()
    run = client.get(f"/api/discovery/runs/{body['run_id']}").json()
    assert run["status"] == "error" and "spend cap" in run["error"]


def test_unknown_run_is_404(client):
    assert client.get("/api/discovery/runs/999").status_code == 404
    assert client.get("/api/discovery/runs/latest").json() is None


def test_fetch_feed_uses_conditional_get_cache(monkeypatch):
    rss._cache.clear()
    calls = []

    def fake_get(url, headers=None, **kw):
        calls.append(dict(headers))
        if "If-None-Match" in headers:
            return httpx.Response(304, request=httpx.Request("GET", url))
        return httpx.Response(200, content=rss_xml(("T", "http://n/1", "")), headers={"ETag": '"abc"'}, request=httpx.Request("GET", url))

    monkeypatch.setattr(rss.httpx, "get", fake_get)
    first = rss.fetch_feed("f", "http://feed")
    second = rss.fetch_feed("f", "http://feed")
    assert first.ok and not first.not_modified
    assert second.ok and second.not_modified and [i.raw_text for i in second.items] == ["T"]
    assert calls[1]["If-None-Match"] == '"abc"'
    rss._cache.clear()


def test_default_feeds_are_capped_and_unique():
    assert 1 <= len(rss.DEFAULT_FEEDS) <= 12
    assert len({n for n, _ in rss.DEFAULT_FEEDS}) == len(rss.DEFAULT_FEEDS)


@pytest.fixture
def topics(db, make_topic):
    return {
        "new_article": make_topic("A", "article"),
        "new_video": make_topic("V", "video"),
        "new_both": make_topic("B", "both"),
        "discarded_video": make_topic("D", "video", status="discarded"),
    }


def test_list_topics_filter_by_status(client, topics):
    titles = {t["title"] for t in client.get("/api/discovery/topics", params={"status": "discarded"}).json()}
    assert titles == {"D"}


def test_list_topics_suitable_for_includes_both(client, topics):
    titles = {t["title"] for t in client.get("/api/discovery/topics", params={"suitable_for": "video"}).json()}
    assert titles == {"V", "B", "D"}
    titles = {t["title"] for t in client.get("/api/discovery/topics", params={"suitable_for": "article"}).json()}
    assert titles == {"A", "B"}


def test_list_topics_combined_filters_and_no_filter(client, topics):
    combined = client.get("/api/discovery/topics", params={"status": "new", "suitable_for": "video"}).json()
    assert {t["title"] for t in combined} == {"V", "B"}
    assert len(client.get("/api/discovery/topics").json()) == 4


def test_patch_topic_updates_status(client, topics, db):
    tid = topics["new_article"].id
    resp = client.patch(f"/api/discovery/topics/{tid}", json={"status": "selected"})
    assert resp.status_code == 200 and resp.json()["status"] == "selected"
    db.expire_all()
    assert db.get(TopicCandidate, tid).status == "selected"


def test_patch_unknown_topic_is_404(client):
    assert client.patch("/api/discovery/topics/999", json={"status": "selected"}).status_code == 404


def test_generate_topics_samples_sources_evenly_when_over_cap(db, monkeypatch):
    rows = [ScrapedItem(source="a", source_url=f"http://a/{i}", raw_text=f"a{i}") for i in range(60)]
    rows += [ScrapedItem(source="b", source_url=f"http://b/{i}", raw_text=f"b{i}") for i in range(5)]
    db.add_all(rows)
    db.commit()
    prompts = []
    monkeypatch.setattr(discovery, "generate_json", lambda p, **kw: prompts.append(p) or [])
    discovery.generate_topics(db, scraped_item_ids=[r.id for r in rows])
    assert prompts[0].count("(b)") == 5 and prompts[0].count("] (a)") == discovery.MAX_NEW_ITEMS - 5

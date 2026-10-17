"""Discovery workspaces: source-data contract, per-workspace collection/dedupe, gating of topics. Hermetic."""

import re

import pytest

from app.database import SessionLocal
from app.models import DiscoveryRun, ScrapedItem, TopicCandidate
from app.services import discovery
from app.services.scraping import rss, sources_footonomy, sources_madridonomy, twitter, workspaces
from app.services.scraping.base import RawItem, SourceResult

URL_RE = re.compile(r"^https?://\S+$")


@pytest.fixture(autouse=True)
def clean_tables():
    with SessionLocal() as db:
        for model in (ScrapedItem, TopicCandidate, DiscoveryRun):
            db.query(model).delete()
        db.commit()


@pytest.mark.parametrize("ws_id", workspaces.WORKSPACE_IDS)
def test_source_data_contract(ws_id):
    ws = workspaces.get(ws_id)
    assert ws.id == ws_id and ws.label and ws.description and ws.topic_focus.strip()
    names = [n for n, _ in ws.feeds]
    assert len(names) == len(set(names)) and len({u for _, u in ws.feeds}) == len(ws.feeds)
    assert all(URL_RE.match(u) for _, u in ws.feeds), "every feed needs an http(s) URL"
    assert all(re.fullmatch(r"[a-z0-9_]+", n) for n in names), "feed names are lowercase snake_case"
    assert len(ws.x_query) <= 480
    assert len(ws.feeds) <= 40


def test_workspace_sizes_and_overlap():
    assert len(sources_madridonomy.FEEDS) <= 14
    assert len(rss.DEFAULT_FEEDS) + len(sources_footonomy.EXTRA_FEEDS) <= 20
    assert not ({n for n, _ in sources_footonomy.EXTRA_FEEDS} & {n for n, _ in rss.DEFAULT_FEEDS})
    general = {n for n, _ in workspaces.get("general").feeds}
    assert general >= {n for n, _ in workspaces.get("madridonomy").feeds} | {n for n, _ in workspaces.get("footonomy").feeds}


def test_madridonomy_focus_is_real_madrid_only():
    ws = workspaces.get("madridonomy")
    assert "real madrid" in ws.topic_focus.lower() and "real madrid" in ws.x_query.lower() or "realmadrid" in ws.x_query.lower()
    assert workspaces.get("footonomy").topic_focus != ws.topic_focus


def test_unknown_workspace_is_rejected():
    with pytest.raises(ValueError):
        workspaces.get("nope")


def _fake_sources(monkeypatch, items):
    seen = {}

    def fake_feeds(feeds=None):
        seen["feeds"] = [n for n, _ in (feeds or [])]
        return [SourceResult(source_name="feed", items=[RawItem(source="news_rss:x", source_url=u, raw_text=t) for u, t in items], ok=True)]

    def fake_twitter(query="", limit=20):
        seen["x_query"] = query
        return SourceResult(source_name="twitter", items=[], ok=False, error="off")

    monkeypatch.setattr(rss, "fetch_all_feeds", fake_feeds)
    monkeypatch.setattr(twitter, "fetch_topic", fake_twitter)
    return seen


def test_collect_uses_the_workspaces_own_sources(client, monkeypatch):
    seen = _fake_sources(monkeypatch, [("https://e.com/1", "Madrid story")])
    with SessionLocal() as db:
        discovery.collect(db, "madridonomy")
    assert seen["feeds"] == [n for n, _ in workspaces.get("madridonomy").feeds]
    assert seen["x_query"] == sources_madridonomy.X_QUERY
    with SessionLocal() as db:
        discovery.collect(db, "footonomy")
    assert seen["feeds"] == [n for n, _ in workspaces.get("footonomy").feeds]


def test_the_same_article_is_stored_once_per_workspace(client, monkeypatch):
    _fake_sources(monkeypatch, [("https://e.com/shared", "Shared story")])
    with SessionLocal() as db:
        first = discovery.collect(db, "madridonomy")
        again = discovery.collect(db, "madridonomy")
        other = discovery.collect(db, "footonomy")
    assert first["new_items"] == 1 and again["new_items"] == 0  # deduped inside a workspace
    assert other["new_items"] == 1  # but still new for the other workspace
    with SessionLocal() as db:
        assert {r.workspace for r in db.query(ScrapedItem)} == {"madridonomy", "footonomy"}


def test_topics_are_gated_by_workspace(client):
    with SessionLocal() as db:
        db.add_all([
            TopicCandidate(title="Bellingham injury", suitable_for="both", workspace="madridonomy"),
            TopicCandidate(title="Haaland hat-trick", suitable_for="both", workspace="footonomy"),
        ])
        db.commit()
    titles = lambda ws: sorted(t["title"] for t in client.get(f"/api/discovery/topics?workspace={ws}").json())  # noqa: E731
    assert titles("madridonomy") == ["Bellingham injury"]
    assert titles("footonomy") == ["Haaland hat-trick"]
    assert titles("general") == ["Bellingham injury", "Haaland hat-trick"]  # ungated view
    assert titles.__call__("footonomy") and client.get("/api/discovery/topics").json()[0]["workspace"] == "footonomy"  # default
    assert client.get("/api/discovery/topics?workspace=bogus").status_code == 400


def test_topic_prompt_carries_the_workspace_focus(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(discovery, "generate_json", lambda prompt, **k: captured.setdefault("prompt", prompt) and [])
    with SessionLocal() as db:
        db.add(ScrapedItem(source="x", raw_text="Madrid sign a striker", content_hash="h1", workspace="madridonomy"))
        db.commit()
        discovery.generate_topics(db, workspace="madridonomy")
    assert "Madridonomy" in captured["prompt"] and "Ignore items that have nothing to do with Real Madrid" in captured["prompt"]


def test_workspaces_endpoint_and_scrape_param(client, monkeypatch):
    names = [w["id"] for w in client.get("/api/discovery/workspaces").json()]
    assert names == ["madridonomy", "footonomy", "general"]
    _fake_sources(monkeypatch, [])
    r = client.post("/api/discovery/scrape?workspace=madridonomy")
    assert r.status_code == 200 and r.json()["workspace"] == "madridonomy"
    assert client.get("/api/discovery/runs/latest?workspace=madridonomy").json()["workspace"] == "madridonomy"
    assert client.get("/api/discovery/runs/latest?workspace=footonomy").json() is None
    assert client.post("/api/discovery/scrape?workspace=zzz").status_code == 400

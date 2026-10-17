import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import DiscoveryRun, ScrapedItem, TopicCandidate, TopicSourceLink, utcnow
from app.services.costs import ledger, prices
from app.services.gemini_client import GeminiNotConfigured, fast_model, generate_json, track_usage
from app.services.scraping import instagram, rss, twitter
from app.services.scraping import workspaces
from app.services.scraping.base import SourceResult

log = logging.getLogger(__name__)

MAX_NEW_ITEMS = 40  # per run: only the newest NEW items go to Gemini
ITEM_CHARS = 300

TOPIC_PROMPT_TEMPLATE = """You are a football content strategist. Below is a batch of \
raw scraped items (news headlines/snippets, tweets) from the last collection run. \
Reduce this into 15-20 distinct, non-overlapping content topic candidates for a \
{focus}.

For each topic give: a short title, a one-sentence rationale (why it's trending/\
relevant right now), and which content format(s) it suits: "article", "video", or \
"both".

Respond as a JSON array of objects with exactly these keys: title, rationale, \
suitable_for, source_indices (a list of the 0-based indices from the input items \
below that this topic draws from).

Input items:
{items_block}
"""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def content_hash(raw_text: str | None, source_url: str | None = None) -> str:
    """Stable identity of an item across scrapes: its URL without query/fragment, else its normalized text."""
    key = ""
    if source_url:
        parts = urlsplit(source_url.strip().lower())
        key = f"{parts.netloc}{parts.path}".rstrip("/")
    if not key:
        key = normalize_text(raw_text or "")
    return hashlib.sha1(key.encode()).hexdigest()


def _dedupe(items: list[tuple[SourceResult, int]]) -> list[tuple[SourceResult, int]]:
    seen: set[str] = set()
    deduped = []
    for result, idx in items:
        text = normalize_text(result.items[idx].raw_text)
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append((result, idx))
    return deduped


def collect(db: Session, workspace: str = workspaces.DEFAULT_WORKSPACE) -> dict:
    """Runs the Collect + Clean steps against all configured sources.
    Any single source failing must not halt the pipeline (per doc) — each
    source's ok/error is returned so failures are visible immediately rather
    than discovered later. Only items not already stored (by content hash) are saved,
    so `scraped_item_ids` / `new_items` are the NEW items of this run.
    """
    ws = workspaces.get(workspace)
    results: list[SourceResult] = []
    results.extend(rss.fetch_all_feeds(list(ws.feeds)))
    if ws.x_query:
        results.append(twitter.fetch_topic(ws.x_query))
    results.append(instagram.fetch_topic())

    flat: list[tuple[SourceResult, int]] = [(r, i) for r in results if r.ok for i in range(len(r.items))]
    flat = _dedupe(flat)

    hashes = [content_hash(result.items[idx].raw_text, result.items[idx].source_url) for result, idx in flat]
    known: set[str] = set()
    for i in range(0, len(hashes), 500):
        known.update(
            h
            for (h,) in db.query(ScrapedItem.content_hash).filter(
                ScrapedItem.workspace == ws.id, ScrapedItem.content_hash.in_(hashes[i : i + 500])
            )
        )  # per workspace: the same article can legitimately feed both Madridonomy and Footonomy

    saved_ids = []
    seen_now: set[str] = set()
    for (result, idx), h in zip(flat, hashes):
        if h in known or h in seen_now:
            continue
        seen_now.add(h)
        raw = result.items[idx]
        row = ScrapedItem(source=raw.source, source_url=raw.source_url, raw_text=raw.raw_text, content_hash=h, workspace=ws.id)
        db.add(row)
        db.flush()
        saved_ids.append(row.id)
    db.commit()

    health = [
        {"source": r.source_name, "ok": r.ok, "item_count": len(r.items), "error": r.error}
        for r in results
    ]
    x_reads = sum(h["item_count"] for h in health if h["source"] == "twitter" and h["ok"])
    return {
        "scraped_item_ids": saved_ids,
        "new_items": len(saved_ids),
        "fetched_items": len(flat),
        "source_health": health,
        "x_reads": x_reads,
    }


def backfill_hashes(limit: int = 5000) -> int:
    """Startup: give pre-existing scraped items a content_hash so the first scrape after upgrading doesn't re-send them."""
    try:
        with SessionLocal() as db:
            rows = db.query(ScrapedItem).filter(ScrapedItem.content_hash.is_(None)).order_by(ScrapedItem.id.desc()).limit(limit).all()
            for row in rows:
                row.content_hash = content_hash(row.raw_text, row.source_url)
            db.commit()
            return len(rows)
    except Exception:  # noqa: BLE001 — best effort
        log.warning("could not backfill scraped item hashes", exc_info=True)
        return 0


def _balanced(rows: list[ScrapedItem], limit: int) -> list[ScrapedItem]:
    """Newest-first rows -> at most `limit`, taken round-robin across sources so one chatty feed can't crowd out the rest."""
    if len(rows) <= limit:
        return rows
    by_source: dict[str, list[ScrapedItem]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row)
    picked: list[ScrapedItem] = []
    queues = list(by_source.values())
    while len(picked) < limit and any(queues):
        for q in queues:
            if q and len(picked) < limit:
                picked.append(q.pop(0))
    return picked


def generate_topics(
    db: Session,
    scraped_item_ids: list[int] | None = None,
    limit: int = MAX_NEW_ITEMS,
    workspace: str = workspaces.DEFAULT_WORKSPACE,
) -> list[TopicCandidate]:
    """Runs the Topic generation step via Gemini against recently scraped items (newest `limit`, 300 chars each)."""
    ws = workspaces.get(workspace)
    query = db.query(ScrapedItem).filter(ScrapedItem.workspace == ws.id)
    if scraped_item_ids:
        query = query.filter(ScrapedItem.id.in_(scraped_item_ids))
    scraped = _balanced(query.order_by(ScrapedItem.id.desc()).limit(limit * 10).all(), limit)

    if not scraped:
        return []

    items_block = "\n".join(f"[{i}] ({item.source}) {(item.raw_text or '')[:ITEM_CHARS]}" for i, item in enumerate(scraped))
    prompt = TOPIC_PROMPT_TEMPLATE.format(items_block=items_block, focus=ws.topic_focus)

    raw_topics = generate_json(prompt, model=fast_model())
    if isinstance(raw_topics, dict):  # tolerate {"topics": [...]}
        raw_topics = next((v for v in raw_topics.values() if isinstance(v, list)), [])

    created: list[TopicCandidate] = []
    for t in raw_topics:
        topic = TopicCandidate(
            title=t.get("title", "Untitled"),
            rationale=t.get("rationale", ""),
            suitable_for=t.get("suitable_for", "both"),
            status="new",
            workspace=ws.id,
        )
        db.add(topic)
        db.flush()

        for idx in t.get("source_indices", []):
            if isinstance(idx, int) and 0 <= idx < len(scraped):
                db.add(TopicSourceLink(topic_id=topic.id, scraped_item_id=scraped[idx].id))

        created.append(topic)

    db.commit()
    return created


# --- runs: collection is synchronous, topic generation happens in the background ---


def run_to_dict(run: DiscoveryRun) -> dict:
    end = (run.finished_at or utcnow()).replace(tzinfo=None)  # SQLite hands datetimes back naive (UTC)
    started = run.started_at.replace(tzinfo=None)
    return {
        "id": run.id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "new_items": run.new_items or 0,
        "topics_created": run.topics_created or 0,
        "error": run.error,
        "source_health": json.loads(run.source_health) if run.source_health else [],
        "cost_usd": run.cost_usd,
        "elapsed_seconds": round(max((end - started).total_seconds(), 0.0), 1),
        "workspace": run.workspace or workspaces.DEFAULT_WORKSPACE,
    }


def _with_x(gemini_usd: float | None, x_reads: int) -> float:
    """Actual spend = measured Gemini cost + X reads at the per-read price."""
    return round((gemini_usd or 0.0) + x_reads * prices.usd("x.post_read"), 6)


def _log_cost(run_id: int, usage: list[dict], x_reads: int, new_items: int, topics: int, trigger: str, status: str) -> float:
    cost = _with_x(ledger.usage_cost_usd(usage), x_reads)
    if trigger == "manual" or cost > 0:  # background auto-scrapes only log when they actually spent something
        ledger.log_event(
            "discovery.scrape",
            {"n_items": new_items, "include_x": x_reads > 0, "x_reads": x_reads},
            ref_type="discovery_run", ref_id=run_id,
            usage=usage,
            actual_usd=cost,
            details={"topics_created": topics, "x_reads": x_reads, "status": status, "trigger": trigger},
        )
    return cost


def start_run(db: Session, trigger: str = "manual", workspace: str = workspaces.DEFAULT_WORKSPACE) -> dict:
    """Collect now (fast), then decide: skipped (nothing new), error (Gemini not configured) or generating.
    Returns {"run": DiscoveryRun dict, "item_ids": [...], "x_reads": int}; the caller schedules `finish_run` when
    run["status"] == "generating"."""
    workspace = workspaces.get(workspace).id  # validates
    run = DiscoveryRun(status="generating", trigger=trigger, workspace=workspace)
    db.add(run)
    db.commit()
    collected = collect(db, workspace)
    run.new_items = collected["new_items"]
    run.source_health = json.dumps(collected["source_health"])
    x_reads = collected["x_reads"]
    if not collected["scraped_item_ids"]:
        run.status, run.finished_at = "skipped", utcnow()
        run.cost_usd = _log_cost(run.id, [], x_reads, 0, 0, trigger, "skipped")
    elif not settings.gemini_api_key:
        run.status, run.finished_at = "error", utcnow()
        run.error = "GEMINI_API_KEY is not set in .env"
        run.cost_usd = _log_cost(run.id, [], x_reads, run.new_items, 0, trigger, "error")
    db.commit()
    db.refresh(run)
    return {"run": run_to_dict(run), "item_ids": collected["scraped_item_ids"], "x_reads": x_reads}


def finish_run(run_id: int, item_ids: list[int], x_reads: int = 0, trigger: str = "manual", workspace: str = workspaces.DEFAULT_WORKSPACE) -> None:
    """Background half of a scrape: topic generation + cost logging. Never raises."""
    topics_n, usage, error = 0, [], None
    try:
        with SessionLocal() as db:
            with track_usage() as usage:
                topics_n = len(generate_topics(db, scraped_item_ids=item_ids, workspace=workspace))
    except GeminiNotConfigured as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 — surfaced on the run row instead of vanishing in a background thread
        log.warning("discovery topic generation failed for run %s", run_id, exc_info=True)
        error = _friendly_error(exc)
    with SessionLocal() as db:
        run = db.get(DiscoveryRun, run_id)
        if run is None:
            return
        run.cost_usd = _log_cost(run_id, usage, x_reads, len(item_ids), topics_n, trigger, "error" if error else "done")
        run.topics_created = topics_n
        run.status = "error" if error else "done"
        run.error = error
        run.finished_at = utcnow()
        db.commit()


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "Gemini rate limit or spend cap reached. Try again later."
    return text[:300] or exc.__class__.__name__


def fail_stale_runs() -> None:
    """Startup: a restart kills the generation thread, so don't leave runs 'generating' forever."""
    try:
        with SessionLocal() as db:
            for run in db.query(DiscoveryRun).filter(DiscoveryRun.status == "generating").all():
                run.status, run.error, run.finished_at = "error", "Interrupted by a server restart.", utcnow()
            db.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not close stale discovery runs", exc_info=True)


def latest_run(db: Session, workspace: str = workspaces.DEFAULT_WORKSPACE) -> DiscoveryRun | None:
    return db.query(DiscoveryRun).filter(DiscoveryRun.workspace == workspace).order_by(DiscoveryRun.id.desc()).first()


# --- optional background scheduler (AUTO_SCRAPE_MINUTES) ---


def run_scheduled_scrape(workspace: str = workspaces.DEFAULT_WORKSPACE) -> dict:
    """One full unattended scrape (collect + generate) of one workspace in the calling thread."""
    with SessionLocal() as db:
        started = start_run(db, trigger="auto", workspace=workspace)
    if started["run"]["status"] == "generating":
        finish_run(started["run"]["id"], started["item_ids"], started["x_reads"], trigger="auto", workspace=workspace)
    return started["run"]


def _scheduler_loop(minutes: int) -> None:
    time.sleep(30)  # first run shortly after startup so topics are waiting; then every `minutes`
    while True:
        for workspace in workspaces.SCRAPED_WORKSPACES:
            try:
                run_scheduled_scrape(workspace)
            except Exception:  # noqa: BLE001
                log.warning("scheduled discovery scrape failed for %s", workspace, exc_info=True)
        time.sleep(minutes * 60)


def start_scheduler() -> threading.Thread | None:
    minutes = settings.auto_scrape_minutes
    if minutes <= 0:
        return None
    thread = threading.Thread(target=_scheduler_loop, args=(minutes,), daemon=True, name="auto-scrape")
    thread.start()
    log.info("auto-scrape enabled: every %s minutes", minutes)
    return thread

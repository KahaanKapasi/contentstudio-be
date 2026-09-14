from sqlalchemy.orm import Session

from app.models import ScrapedItem, TopicCandidate, TopicSourceLink
from app.services.gemini_client import GeminiNotConfigured, generate_json
from app.services.scraping import instagram, rss, twitter
from app.services.scraping.base import SourceResult

TOPIC_PROMPT_TEMPLATE = """You are a football content strategist. Below is a batch of \
raw scraped items (news headlines/snippets, tweets) from the last collection run. \
Reduce this into 15-20 distinct, non-overlapping content topic candidates for a \
football content creator (Madridonomy — Real Madrid-focused, expanding to broader \
football content).

For each topic give: a short title, a one-sentence rationale (why it's trending/\
relevant right now), and which content format(s) it suits: "article", "video", or \
"both".

Respond as a JSON array of objects with exactly these keys: title, rationale, \
suitable_for, source_indices (a list of the 0-based indices from the input items \
below that this topic draws from).

Input items:
{items_block}
"""


def _dedupe(items: list[tuple[SourceResult, int]]) -> list[tuple[SourceResult, int]]:
    seen: set[str] = set()
    deduped = []
    for result, idx in items:
        text = result.items[idx].raw_text.strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append((result, idx))
    return deduped


def collect(db: Session) -> dict:
    """Runs the Collect + Clean steps against all configured sources.
    Any single source failing must not halt the pipeline (per doc) — each
    source's ok/error is returned so failures are visible immediately rather
    than discovered later.
    """
    results: list[SourceResult] = []
    results.extend(rss.fetch_all_feeds())
    results.append(twitter.fetch_topic())
    results.append(instagram.fetch_topic())

    flat: list[tuple[SourceResult, int]] = [(r, i) for r in results if r.ok for i in range(len(r.items))]
    flat = _dedupe(flat)

    saved_ids = []
    for result, idx in flat:
        raw = result.items[idx]
        row = ScrapedItem(source=raw.source, source_url=raw.source_url, raw_text=raw.raw_text)
        db.add(row)
        db.flush()
        saved_ids.append(row.id)
    db.commit()

    health = [{"source": r.source_name, "ok": r.ok, "item_count": len(r.items), "error": r.error} for r in results]
    return {"scraped_item_ids": saved_ids, "source_health": health}


def generate_topics(db: Session, scraped_item_ids: list[int] | None = None, limit: int = 60) -> list[TopicCandidate]:
    """Runs the Topic generation step via Gemini against recently scraped items."""
    query = db.query(ScrapedItem)
    if scraped_item_ids:
        query = query.filter(ScrapedItem.id.in_(scraped_item_ids))
    scraped = query.order_by(ScrapedItem.id.desc()).limit(limit).all()

    if not scraped:
        return []

    items_block = "\n".join(f"[{i}] ({item.source}) {item.raw_text[:400]}" for i, item in enumerate(scraped))
    prompt = TOPIC_PROMPT_TEMPLATE.format(items_block=items_block)

    try:
        raw_topics = generate_json(prompt)
    except GeminiNotConfigured:
        raise

    created: list[TopicCandidate] = []
    for t in raw_topics:
        topic = TopicCandidate(
            title=t.get("title", "Untitled"),
            rationale=t.get("rationale", ""),
            suitable_for=t.get("suitable_for", "both"),
            status="new",
        )
        db.add(topic)
        db.flush()

        for idx in t.get("source_indices", []):
            if 0 <= idx < len(scraped):
                db.add(TopicSourceLink(topic_id=topic.id, scraped_item_id=scraped[idx].id))

        created.append(topic)

    db.commit()
    return created

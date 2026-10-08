from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import TopicCandidate
from app.schemas import TopicCandidateOut, TopicStatusUpdate
from app.services import discovery
from app.services.costs import ledger, prices
from app.services.gemini_client import GeminiNotConfigured, track_usage

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/topics", response_model=list[TopicCandidateOut])
def list_topics(
    status: str | None = None,
    suitable_for: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(TopicCandidate)
    if status:
        query = query.filter(TopicCandidate.status == status)
    if suitable_for:
        query = query.filter(TopicCandidate.suitable_for.in_([suitable_for, "both"]))
    return query.order_by(TopicCandidate.created_at.desc()).all()


@router.patch("/topics/{topic_id}", response_model=TopicCandidateOut)
def update_topic_status(topic_id: int, payload: TopicStatusUpdate, db: Session = Depends(get_db)):
    topic = db.query(TopicCandidate).filter(TopicCandidate.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.status = payload.status
    db.commit()
    db.refresh(topic)
    return topic


@router.post("/scrape")
def trigger_scrape(db: Session = Depends(get_db)):
    """Manual 'trigger scrape now' — runs Collect+Clean, then Topic generation.
    Runs inline (FastAPI request, not a background task) since the doc's
    fallback-acceptable BackgroundTasks approach was chosen for this build;
    a scrape+generate cycle is a few seconds, acceptable for a manual trigger.
    """
    collect_result = discovery.collect(db)
    try:
        with track_usage() as usage:
            topics = discovery.generate_topics(db, scraped_item_ids=collect_result["scraped_item_ids"])
    except GeminiNotConfigured as exc:
        return {
            "source_health": collect_result["source_health"],
            "topics_created": 0,
            "error": str(exc),
        }
    x_reads = sum(h["item_count"] for h in collect_result["source_health"] if h["source"] == "twitter" and h["ok"])
    if usage or x_reads:  # nothing billable happened when there were no items and no X reads
        ledger.log_event(
            "discovery.scrape",
            {"n_items": len(collect_result["scraped_item_ids"]), "include_x": x_reads > 0, "x_reads": x_reads},
            usage=usage,
            actual_usd=_with_x(ledger.usage_cost_usd(usage), x_reads),
            details={"topics_created": len(topics), "x_reads": x_reads},
        )
    return {
        "source_health": collect_result["source_health"],
        "topics_created": len(topics),
    }


def _with_x(gemini_usd: float | None, x_reads: int) -> float | None:
    """Actual spend = measured Gemini cost + X reads at the per-read price (None if neither is known)."""
    if gemini_usd is None and not x_reads:
        return None
    return round((gemini_usd or 0.0) + x_reads * prices.usd("x.post_read"), 6)

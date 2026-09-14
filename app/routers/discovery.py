from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import TopicCandidate
from app.schemas import TopicCandidateOut, TopicStatusUpdate
from app.services import discovery
from app.services.gemini_client import GeminiNotConfigured

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
        topics = discovery.generate_topics(db, scraped_item_ids=collect_result["scraped_item_ids"])
    except GeminiNotConfigured as exc:
        return {
            "source_health": collect_result["source_health"],
            "topics_created": 0,
            "error": str(exc),
        }
    return {
        "source_health": collect_result["source_health"],
        "topics_created": len(topics),
    }

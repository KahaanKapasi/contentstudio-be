from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DiscoveryRun, TopicCandidate
from app.schemas import TopicCandidateOut, TopicStatusUpdate
from app.services import discovery

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
def trigger_scrape(background: BackgroundTasks, db: Session = Depends(get_db)):
    """Manual 'trigger scrape now'. Collection (parallel RSS/X/IG fetch, a few seconds) happens in this request;
    topic generation (Gemini) continues in a background task. Poll GET /runs/{id} until status != 'generating'.
    Only items not seen before are sent to Gemini; with none, Gemini is skipped (cost $0)."""
    started = discovery.start_run(db, trigger="manual")
    run = started["run"]
    if run["status"] == "generating":
        background.add_task(discovery.finish_run, run["id"], started["item_ids"], started["x_reads"], "manual")
    return _scrape_response(run)


def _scrape_response(run: dict) -> dict:
    out = {
        "run_id": run["id"],
        "status": run["status"],
        "new_items": run["new_items"],
        "source_health": run["source_health"],
        "topics_created": run["topics_created"],
    }
    if run["error"]:
        out["error"] = run["error"]
    if run["status"] == "skipped":
        out["message"] = "No new items since the last scrape, so topic generation was skipped (cost $0)."
    return out


@router.get("/runs/latest")
def latest_run(db: Session = Depends(get_db)):
    run = discovery.latest_run(db)
    return discovery.run_to_dict(run) if run else None


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(DiscoveryRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return discovery.run_to_dict(run)

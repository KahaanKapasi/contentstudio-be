from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DiscoveryRun, TopicCandidate
from app.schemas import TopicCandidateOut, TopicStatusUpdate
from app.services import discovery
from app.services.scraping import workspaces

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


def _workspace(value: str) -> str:
    try:
        return workspaces.get(value).id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces")
def list_workspaces():
    return workspaces.describe()


@router.get("/topics", response_model=list[TopicCandidateOut])
def list_topics(
    status: str | None = None,
    suitable_for: str | None = None,
    workspace: str = workspaces.DEFAULT_WORKSPACE,
    db: Session = Depends(get_db),
):
    workspace = _workspace(workspace)
    query = db.query(TopicCandidate)
    if workspace != "general":  # General is the ungated view: every topic from every workspace
        query = query.filter(TopicCandidate.workspace == workspace)
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
def trigger_scrape(background: BackgroundTasks, workspace: str = workspaces.DEFAULT_WORKSPACE, db: Session = Depends(get_db)):
    """Manual 'trigger scrape now'. Collection (parallel RSS/X/IG fetch, a few seconds) happens in this request;
    topic generation (Gemini) continues in a background task. Poll GET /runs/{id} until status != 'generating'.
    Only items not seen before are sent to Gemini; with none, Gemini is skipped (cost $0)."""
    workspace = _workspace(workspace)
    started = discovery.start_run(db, trigger="manual", workspace=workspace)
    run = started["run"]
    if run["status"] == "generating":
        background.add_task(discovery.finish_run, run["id"], started["item_ids"], started["x_reads"], "manual", workspace)
    return _scrape_response(run)


def _scrape_response(run: dict) -> dict:
    out = {
        "run_id": run["id"],
        "status": run["status"],
        "new_items": run["new_items"],
        "source_health": run["source_health"],
        "topics_created": run["topics_created"],
        "workspace": run["workspace"],
    }
    if run["error"]:
        out["error"] = run["error"]
    if run["status"] == "skipped":
        out["message"] = "No new items since the last scrape, so topic generation was skipped (cost $0)."
    return out


@router.get("/runs/latest")
def latest_run(workspace: str = workspaces.DEFAULT_WORKSPACE, db: Session = Depends(get_db)):
    run = discovery.latest_run(db, _workspace(workspace))
    return discovery.run_to_dict(run) if run else None


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(DiscoveryRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return discovery.run_to_dict(run)

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Script, VideoTopic
from app.schemas import (
    ScriptGenerateRequest,
    ScriptOut,
    ScriptUpdate,
    VideoTitlesGenerateRequest,
    VideoTopicOut,
)
from app.services import video_pipeline
from app.services.gemini_client import GeminiNotConfigured

router = APIRouter(prefix="/api/video", tags=["video"])


@router.post("/generate-titles", response_model=list[VideoTopicOut])
def generate_titles(payload: VideoTitlesGenerateRequest, db: Session = Depends(get_db)):
    try:
        return video_pipeline.generate_video_titles(db, payload.topic_id)
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/topics", response_model=list[VideoTopicOut])
def list_video_topics(topic_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(VideoTopic)
    if topic_id is not None:
        query = query.filter(VideoTopic.topic_id == topic_id)
    return query.order_by(VideoTopic.suggestion_score.desc()).all()


@router.post("/scripts/generate", response_model=list[ScriptOut])
def generate_scripts(payload: ScriptGenerateRequest, db: Session = Depends(get_db)):
    try:
        return video_pipeline.generate_scripts(db, payload.video_topic_id)
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/scripts", response_model=list[ScriptOut])
def list_scripts(video_topic_id: int, db: Session = Depends(get_db)):
    return (
        db.query(Script)
        .filter(Script.video_topic_id == video_topic_id)
        .order_by(Script.id.asc())
        .all()
    )


@router.patch("/scripts/{script_id}", response_model=ScriptOut)
def update_script(script_id: int, payload: ScriptUpdate, db: Session = Depends(get_db)):
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    if payload.selected is True:
        db.query(Script).filter(
            Script.video_topic_id == script.video_topic_id,
            Script.id != script.id,
        ).update({Script.selected: False})
        script.selected = True
    elif payload.selected is False:
        script.selected = False

    db.commit()
    db.refresh(script)
    return script

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Script, VideoGeneration, VideoTopic
from app.schemas import (
    PromptImproveOut,
    PromptImproveRequest,
    ProviderInfoOut,
    ScriptGenerateRequest,
    ScriptOut,
    ScriptUpdate,
    VideoGenerationCreate,
    VideoGenerationOut,
    VideoTitlesGenerateRequest,
    VideoTopicOut,
)
from app.services import video_generation, video_pipeline
from app.services.gemini_client import GeminiNotConfigured
from app.services.video_providers import ProviderNotConfigured, catalog

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


# --- Video generation (prompt in -> mp4 out) ---


def _out(gen: VideoGeneration) -> VideoGenerationOut:
    return VideoGenerationOut.model_validate(
        {
            **{c.name: getattr(gen, c.name) for c in gen.__table__.columns},
            "has_file": video_generation.local_file(gen) is not None,
            "estimated_cost_usd": catalog.estimate_cost(gen.provider, gen.model, gen.resolution, gen.duration_seconds),
        }
    )


def _get_generation(db: Session, generation_id: int) -> VideoGeneration:
    gen = db.get(VideoGeneration, generation_id)
    if not gen:
        raise HTTPException(status_code=404, detail="Video generation not found")
    return gen


@router.get("/providers", response_model=list[ProviderInfoOut])
def list_providers():
    return video_generation.list_providers()


@router.post("/prompt/improve", response_model=PromptImproveOut)
def improve_prompt(payload: PromptImproveRequest):
    try:
        return video_generation.improve_prompt(
            payload.idea, payload.research, payload.aspect_ratio, payload.duration_seconds
        )
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Gemini could not improve the prompt. Please try again.") from exc


@router.post("/generations", response_model=VideoGenerationOut)
def create_generation(payload: VideoGenerationCreate, background: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        gen = video_generation.create_generation(
            db,
            prompt=payload.prompt,
            original_idea=payload.original_idea,
            provider=payload.provider,
            model=payload.model,
            aspect_ratio=payload.aspect_ratio,
            duration_seconds=payload.duration_seconds,
            resolution=payload.resolution,
            research_sources=[s.model_dump() for s in payload.research_sources],
        )
    except ProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background.add_task(video_generation.run_generation, gen.id)
    return _out(gen)


@router.get("/generations", response_model=list[VideoGenerationOut])
def list_generations(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(VideoGeneration)
        .order_by(VideoGeneration.created_at.desc(), VideoGeneration.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [_out(g) for g in rows]


@router.get("/generations/{generation_id}", response_model=VideoGenerationOut)
def get_generation(generation_id: int, db: Session = Depends(get_db)):
    gen = _get_generation(db, generation_id)
    return _out(video_generation.reconcile(db, gen))


@router.delete("/generations/{generation_id}", status_code=204)
def delete_generation(generation_id: int, db: Session = Depends(get_db)):
    video_generation.delete_generation(db, _get_generation(db, generation_id))
    return Response(status_code=204)


@router.post("/generations/{generation_id}/retry", response_model=VideoGenerationOut)
def retry_generation(generation_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    gen = _get_generation(db, generation_id)
    try:
        new = video_generation.retry_generation(db, gen)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background.add_task(video_generation.run_generation, new.id)
    return _out(new)


@router.get("/generations/{generation_id}/file")
def generation_file(generation_id: int, db: Session = Depends(get_db)):
    gen = _get_generation(db, generation_id)
    path = video_generation.local_file(gen)
    if path:
        return FileResponse(path, media_type="video/mp4", filename=f"video-{gen.id}.mp4", content_disposition_type="inline")
    if gen.video_url:
        return RedirectResponse(gen.video_url)
    raise HTTPException(status_code=404, detail="No video file for this generation")

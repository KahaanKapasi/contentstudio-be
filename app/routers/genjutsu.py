"""Genjutsu motion transfer API (mounted under /api/video/genjutsu, behind the same access gate as /api/video)."""

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GenjutsuJob
from app.services import genjutsu
from app.services.costs import prices

router = APIRouter(prefix="/genjutsu", tags=["genjutsu"])

MAX_VIDEO_BYTES = 300 * 1024 * 1024


class JobOut(BaseModel):
    id: int
    prompt: str | None
    resolution: str
    status: str  # queued | running | succeeded | failed | moderated | canceled
    error: str | None
    video_name: str | None
    image_count: int
    input_seconds: float | None
    billed_seconds: int | None
    est_cost_usd: float | None
    has_file: bool
    video_url: str | None
    created_at: datetime
    completed_at: datetime | None


def _out(job: GenjutsuJob) -> JobOut:
    return JobOut(
        id=job.id, prompt=job.prompt, resolution=job.resolution, status=job.status, error=job.error,
        video_name=job.video_name, image_count=job.image_count or 0, input_seconds=job.input_seconds,
        billed_seconds=job.billed_seconds, est_cost_usd=job.est_cost_usd, has_file=genjutsu.output_file(job) is not None,
        video_url=job.video_url, created_at=job.created_at, completed_at=job.completed_at,
    )


def _get(db: Session, job_id: int) -> GenjutsuJob:
    job = db.get(GenjutsuJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Genjutsu job not found")
    return job


def _raise(exc: Exception) -> None:
    if isinstance(exc, genjutsu.GenjutsuNotConfigured):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, genjutsu.CostChanged):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, genjutsu.GenjutsuInputError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


async def _save_upload(upload: UploadFile, dest: Path, limit: int, what: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dest.open("wb") as fh:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > limit:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"The {what} is larger than {limit // (1 << 20)} MB.")
            fh.write(chunk)


@router.get("/status")
def status():
    keys = genjutsu.missing_keys()
    return {
        "configured": not keys,
        "missing_keys": keys,
        "model": genjutsu.MODEL,
        "resolutions": [{"id": r, "price_per_second_usd": prices.usd(f"higgsfield.genjutsu.{r}")} for r in genjutsu.RESOLUTIONS],
        "limits": {"min_video_seconds": genjutsu.MIN_VIDEO_S, "max_billed_seconds": genjutsu.MAX_BILLED_S, "max_images": genjutsu.MAX_IMAGES},
    }


@router.post("/jobs", response_model=JobOut)
async def create_job(
    background: BackgroundTasks,
    video: UploadFile = File(...),
    images: list[UploadFile] = File(...),
    prompt: str = Form(""),
    resolution: str = Form("720p"),
    confirm_cost_usd: float = Form(...),
    db: Session = Depends(get_db),
):
    if genjutsu.missing_keys():
        raise HTTPException(status_code=503, detail="Higgsfield is not configured: set HF_KEY=<key-id>:<key-secret> in .env.")
    suffix = (Path(video.filename or "").suffix or ".mp4").lower()[:8]
    tmp = genjutsu.GENJUTSU_DIR / "_tmp" / f"{uuid.uuid4().hex}{suffix}"
    try:
        await _save_upload(video, tmp, MAX_VIDEO_BYTES, "video")
        blobs = [(img.filename or f"photo{i}", await img.read()) for i, img in enumerate(images, 1)]
        try:
            job = await run_in_threadpool(
                genjutsu.create_job, db, video_tmp=tmp, video_name=video.filename or "video", images=blobs,
                prompt=prompt, resolution=resolution, confirm_cost_usd=confirm_cost_usd,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to the right HTTP status in _raise
            _raise(exc)
    finally:
        tmp.unlink(missing_ok=True)
    background.add_task(genjutsu.run_job, job.id)
    return _out(job)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(limit: int = 50, db: Session = Depends(get_db)):
    jobs = db.query(GenjutsuJob).order_by(GenjutsuJob.id.desc()).limit(max(1, min(limit, 200))).all()
    return [_out(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = genjutsu.reconcile(db, _get(db, job_id))
    db.refresh(job)
    return _out(job)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    if job.status in genjutsu.ACTIVE and job.request_id:
        raise HTTPException(status_code=409, detail="This job is still running at Higgsfield (and billing). Cancel it or wait for it to finish first.")
    genjutsu.delete_job(db, job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    try:
        await run_in_threadpool(genjutsu.cancel_job, db, job)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    db.refresh(job)
    return _out(job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: int, background: BackgroundTasks, confirm_cost_usd: float = Form(...), db: Session = Depends(get_db)):
    source = _get(db, job_id)
    if source.status not in ("failed", "canceled"):
        raise HTTPException(status_code=409, detail="Only a failed or canceled job can be retried.")
    try:
        job = await run_in_threadpool(genjutsu.clone_inputs, db, source, confirm_cost_usd)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    background.add_task(genjutsu.run_job, job.id)
    return _out(job)


@router.get("/jobs/{job_id}/file")
def job_file(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    path = genjutsu.output_file(job)
    if path:
        return FileResponse(path, media_type="video/mp4", filename=f"genjutsu-{job.id}.mp4", content_disposition_type="inline")
    if job.video_url and job.status == "succeeded":
        return RedirectResponse(job.video_url)
    raise HTTPException(status_code=404, detail="No video file for this job")

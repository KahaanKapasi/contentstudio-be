"""Publish a finished video (Studio project or clip generation) to Instagram as a Reel.

Shared by routers/studio.py and routers/video.py. Both models carry nullable
instagram_{media_id,permalink,status,error} columns; `instagram_status` is the lock: only one
publish may be 'publishing' at a time and a 'published' row is never re-posted.
"""

import logging
import re
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.services import instagram_client, media_hosting
from app.services.costs import ledger

log = logging.getLogger(__name__)

MIN_SECONDS = 3
MAX_SECONDS = 15 * 60  # hard Instagram limit
SOFT_MAX_SECONDS = 90  # beyond this the Reel is unlikely to be treated as a short-form Reel
CAPTION_MAX = 2200
HASHTAGS_MAX = 30


def _ratio(aspect: str | None) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*", aspect or "")
    return float(m.group(1)) / float(m.group(2)) if m and float(m.group(2)) else None


def check_reel(local: Path | None, aspect: str | None, duration: float | None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Prefers measuring the local file; falls back to the DB metadata."""
    if local:
        try:
            from app.services.studio.kit import ffmpeg

            p = ffmpeg.probe(local)
            duration = p.duration
            if p.width and p.height:
                aspect = f"{p.width}:{p.height}"
        except Exception:
            log.debug("probe failed for %s", local, exc_info=True)
    errors: list[str] = []
    warnings: list[str] = []
    if duration is not None:
        if duration < MIN_SECONDS:
            errors.append(f"Reels must be at least {MIN_SECONDS} s (this video is {duration:.1f} s).")
        elif duration > MAX_SECONDS:
            errors.append(f"Reels can be at most {MAX_SECONDS // 60} minutes (this video is {duration / 60:.1f} min).")
        elif duration > SOFT_MAX_SECONDS:
            warnings.append(f"This video is {duration:.0f} s; Reels over {SOFT_MAX_SECONDS} s may not reach as many people.")
    r = _ratio(aspect)
    if r is not None and abs(r - 9 / 16) > 0.03:
        warnings.append(f"Aspect ratio is {aspect}; 9:16 is recommended, otherwise Instagram crops the Reel's middle.")
    return errors, warnings


def check_caption(caption: str) -> None:
    if len(caption) > CAPTION_MAX:
        raise HTTPException(status_code=422, detail=f"Caption is {len(caption)} characters; Instagram allows {CAPTION_MAX}.")
    if len(re.findall(r"#\w+", caption)) > HASHTAGS_MAX:
        raise HTTPException(status_code=422, detail=f"Instagram allows at most {HASHTAGS_MAX} hashtags per post.")


def begin(
    db: Session,
    model,
    row,
    *,
    ref_type: str,
    local_file: Path | None,
    aspect: str | None,
    duration: float | None,
    caption: str,
    share_to_feed: bool,
    background: BackgroundTasks,
) -> list[str]:
    """Validates, atomically claims the row ('publishing'), and schedules the background publish.
    Returns the soft warnings. Raises HTTPException for everything that should stop the click."""
    if row.status != "succeeded":
        raise HTTPException(status_code=409, detail="Only a finished video can be published.")
    if not settings.ig_access_token or not settings.ig_business_account_id:
        raise HTTPException(status_code=503, detail="IG_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID not set in .env")
    if not row.video_url and not local_file:
        raise HTTPException(status_code=409, detail="This video has no file to publish.")
    if not row.video_url and not settings.cloudinary_url:
        raise HTTPException(status_code=503, detail="CLOUDINARY_URL is not set; Instagram needs a public video URL, so the file can't be uploaded.")
    check_caption(caption)
    errors, warnings = check_reel(local_file, aspect, duration)
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))

    claimed = db.execute(
        update(model)
        .where(model.id == row.id, or_(model.instagram_status.is_(None), model.instagram_status == "failed"))
        .values(instagram_status="publishing", instagram_error=None)
    ).rowcount
    db.commit()
    if not claimed:
        db.refresh(row)
        if row.instagram_status == "published":
            raise HTTPException(status_code=409, detail="This video is already published to Instagram.")
        raise HTTPException(status_code=409, detail="A publish to Instagram is already in progress.")
    db.refresh(row)
    background.add_task(run, model, row.id, ref_type, caption, share_to_feed)
    return warnings


def run(model, row_id: int, ref_type: str, caption: str, share_to_feed: bool) -> None:
    """Background task (own session): host the file if needed, publish the Reel, record the outcome."""
    db = SessionLocal()
    try:
        row = db.get(model, row_id)
        if row is None:
            return
        try:
            video_url = row.video_url
            if not video_url:
                local = _local_path(row, ref_type)
                if not local:
                    raise instagram_client.InstagramPublishError("The video file is no longer on disk.")
                video_url = media_hosting.upload_video(str(local), public_id=f"{ref_type}-{row.id}")
                row.video_url = video_url
                db.commit()
            result = instagram_client.publish_reel(video_url, caption, share_to_feed)
            row.instagram_media_id = result["media_id"]
            row.instagram_permalink = result["permalink"]
            row.instagram_status = "published"
            row.instagram_error = None
            db.commit()
            ledger.log_event("instagram.publish_reel", ref_type=ref_type, ref_id=row.id)
        except Exception as exc:
            log.warning("Instagram reel publish failed for %s %s: %s", ref_type, row_id, exc)
            db.rollback()
            row = db.get(model, row_id)
            row.instagram_status = "failed"
            row.instagram_error = str(exc)[:1000] or exc.__class__.__name__
            db.commit()
    finally:
        db.close()


def _local_path(row, ref_type: str) -> Path | None:
    if ref_type == "studio_project":
        from app.services.studio import runner

        return runner.local_file(row)
    from app.services import video_generation

    return video_generation.local_file(row)

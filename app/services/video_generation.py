"""Prompt-to-video orchestration: prompt improvement, job lifecycle, reconciliation.

The provider does the real work asynchronously. A row only needs provider_job_id (+ status url) to
be resumable, so `reconcile` (called from GET) can finish a job after a restart or a sleeping host.
"""

import contextlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import VIDEOS_DIR, settings
from app.database import SessionLocal
from app.models import VideoGeneration, utcnow
from app.services import gemini_client, media_hosting
from app.services.video_providers import (
    JobHandle,
    JobParams,
    PollResult,
    ProviderError,
    ProviderNotConfigured,
    TransientProviderError,
    catalog,
    get_provider,
)

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed")
GENERATION_TIMEOUT_S = 15 * 60  # running jobs older than this are marked failed
SUBMIT_GRACE_S = 120  # a row with no provider job id this long after creation never reached the provider
POLL_INTERVAL_S = 5
_PROMPT_MARKER = "=== VIDEO PROMPT ==="

_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# --- providers listing ---


def list_providers() -> list[dict]:
    infos = []
    for pid, models in catalog.MODELS.items():
        missing = get_provider(pid).missing_keys()
        infos.append(
            {
                "id": pid,
                "label": catalog.PROVIDER_LABELS[pid],
                "configured": not missing,
                "missing_keys": missing,
                "default_model": catalog.DEFAULT_MODELS[pid],
                "models": [
                    {
                        "id": m.id,
                        "label": m.label,
                        "aspect_ratios": list(m.aspect_ratios),
                        "durations": list(m.durations),
                        "resolutions": list(m.resolutions),
                        "price_per_second_usd": m.price_per_second_usd,
                        "notes": m.notes,
                    }
                    for m in models.values()
                ],
            }
        )
    return infos


# --- prompt improvement ---


def improve_prompt(idea: str, research: bool, aspect_ratio: str, duration_seconds: int) -> dict:
    idea = idea.strip()
    if not idea:
        raise ValueError("Describe the video you want first.")
    orientation = "vertical (9:16)" if aspect_ratio == "9:16" else f"{aspect_ratio} frame"
    brief = f"""You write prompts for AI text-to-video models (Veo, Seedance, Kling, Wan).

Turn the idea below into ONE production-ready video prompt for a {duration_seconds}-second clip in a \
{orientation}. Cover: subject and setting, the action beat by beat across the {duration_seconds} seconds, \
camera movement and framing, lighting and colour, visual style, and audio cues (ambient sound, music, \
and any short spoken line in quotes). Be concrete and visual, one flowing paragraph, no bullet points, \
no markdown, no title. Keep it under 180 words.

Idea: {idea}
"""
    if not research:
        text = gemini_client.generate_text(brief + "\nReturn only the prompt.")
        return {"prompt": _strip(text), "sources": [], "research_notes": None}

    text, sources = gemini_client.generate_grounded(
        "First use Google Search to check the real, current facts behind this idea (people, teams, scores, "
        "kits, stadiums, dates, events) so nothing in the video contradicts reality.\n\n"
        + brief
        + f"\nFormat your answer exactly as:\n<2-5 short lines of the verified facts you will rely on>\n"
        f"{_PROMPT_MARKER}\n<the video prompt>"
    )
    notes, _, prompt = text.partition(_PROMPT_MARKER)
    if not prompt.strip():  # model ignored the format: treat everything as the prompt
        notes, prompt = "", text
    return {"prompt": _strip(prompt), "sources": sources, "research_notes": _strip(notes) or None}


def _strip(text: str) -> str:
    return text.strip().strip("`").strip()


# --- lifecycle ---


def create_generation(
    db: Session,
    *,
    prompt: str,
    provider: str,
    model: str,
    aspect_ratio: str,
    duration_seconds: int,
    resolution: str,
    original_idea: str | None = None,
    research_sources: list[dict] | None = None,
) -> VideoGeneration:
    """Validates and stores a queued row. Raises catalog.CatalogError (400) / ProviderNotConfigured (503)."""
    if not prompt.strip():
        raise catalog.CatalogError("A prompt is required.")
    catalog.validate(provider, model, aspect_ratio, duration_seconds, resolution)
    missing = get_provider(provider).missing_keys()
    if missing:
        raise ProviderNotConfigured(provider, missing)

    gen = VideoGeneration(
        prompt=prompt.strip(),
        original_idea=original_idea,
        provider=provider,
        model=model,
        aspect_ratio=aspect_ratio,
        duration_seconds=duration_seconds,
        resolution=resolution,
        status="queued",
        research_sources=json.dumps(research_sources or []),
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def retry_generation(db: Session, gen: VideoGeneration) -> VideoGeneration:
    if gen.status != "failed":
        raise ValueError("Only failed generations can be retried.")
    return create_generation(
        db,
        prompt=gen.prompt,
        original_idea=gen.original_idea,
        provider=gen.provider,
        model=gen.model,
        aspect_ratio=gen.aspect_ratio,
        duration_seconds=gen.duration_seconds,
        resolution=gen.resolution,
        research_sources=json.loads(gen.research_sources or "[]"),
    )


def delete_generation(db: Session, gen: VideoGeneration) -> None:
    path = local_file(gen)
    if path:
        path.unlink(missing_ok=True)
    db.delete(gen)
    db.commit()


def local_file(gen: VideoGeneration) -> Path | None:
    """The servable local mp4, or None. Only the file name is trusted from the DB."""
    if not gen.local_path:
        return None
    path = VIDEOS_DIR / Path(gen.local_path).name
    return path if path.is_file() else None


def run_generation(generation_id: int) -> None:
    """Background-task entry point. Uses its own session — the request's is closed by then."""
    db = SessionLocal()
    try:
        gen = db.get(VideoGeneration, generation_id)
        if gen is None or gen.status != "queued":
            return
        gen.status = "running"
        db.commit()
        _submit(db, gen)
        while True:
            db.commit()  # end the read transaction before sleeping
            _sleep(POLL_INTERVAL_S)
            db.expire_all()
            gen = db.get(VideoGeneration, generation_id)
            if gen is None or gen.status in TERMINAL:
                return
            _advance(db, gen)
    except Exception:  # last resort: never leave a row stuck in "running" with a stack trace
        log.exception("video generation %s crashed", generation_id)
        db.rollback()
        gen = db.get(VideoGeneration, generation_id)
        if gen is not None and gen.status not in TERMINAL:
            _fail(db, gen, "Something went wrong while generating the video. Please retry.")
    finally:
        db.close()


def reconcile(db: Session, gen: VideoGeneration) -> VideoGeneration:
    """Idempotent: one poll step for a non-terminal row. Safe to call from GET."""
    if gen.status not in TERMINAL:
        _advance(db, gen)
        db.refresh(gen)
    return gen


@contextlib.contextmanager
def _claim(generation_id: int):
    """Only one thread works on a row at a time (background loop vs. a GET reconcile)."""
    with _inflight_lock:
        free = generation_id not in _inflight
        if free:
            _inflight.add(generation_id)
    try:
        yield free
    finally:
        if free:
            with _inflight_lock:
                _inflight.discard(generation_id)


def _submit(db: Session, gen: VideoGeneration) -> None:
    with _claim(gen.id) as claimed:
        if not claimed:
            return
        try:
            job = get_provider(gen.provider).submit(
                JobParams(
                    prompt=gen.prompt,
                    model=gen.model,
                    aspect_ratio=gen.aspect_ratio,
                    duration_seconds=gen.duration_seconds,
                    resolution=gen.resolution,
                    idempotency_key=str(uuid.uuid5(uuid.NAMESPACE_URL, f"content-studio/video/{gen.id}/{gen.created_at.isoformat()}")),
                )
            )
        except (ProviderError, ProviderNotConfigured) as exc:
            _fail(db, gen, str(exc))
            return
        gen.provider_job_id = job.job_id
        gen.provider_status_url = job.status_url
        db.commit()


def _advance(db: Session, gen: VideoGeneration) -> None:
    with _claim(gen.id) as claimed:
        if not claimed:
            return
        db.refresh(gen)
        if gen.status in TERMINAL:
            return
        try:
            _step(db, gen)
        except (ProviderError, ProviderNotConfigured) as exc:
            _fail(db, gen, str(exc))
        except Exception:
            log.exception("video generation %s failed while polling", gen.id)
            db.rollback()
            _fail(db, gen, "Something went wrong while generating the video. Please retry.")


def _step(db: Session, gen: VideoGeneration) -> None:
    age = _age_seconds(gen)
    if not gen.provider_job_id:
        if age > SUBMIT_GRACE_S:
            _fail(db, gen, "The server restarted before this video was submitted. Please retry.")
        return

    provider = get_provider(gen.provider)
    try:
        result = provider.poll(JobHandle(gen.provider_job_id, gen.provider_status_url))
    except TransientProviderError:
        result = PollResult("running")

    if gen.status == "queued":
        gen.status = "running"
        db.commit()
    if result.state == "failed":
        _fail(db, gen, result.error or "The provider could not generate this video.")
    elif result.state == "succeeded":
        _finalize(db, gen, provider, result.output, age)
    elif age > GENERATION_TIMEOUT_S:
        _fail(db, gen, f"Timed out after {GENERATION_TIMEOUT_S // 60} minutes without a finished video. Please retry.")


def _finalize(db: Session, gen: VideoGeneration, provider, output, age: float) -> None:
    dest = VIDEOS_DIR / f"{gen.id}.mp4"
    try:
        provider.download(output, dest)
    except ProviderError as exc:
        # Transient download problems get retried on the next poll until the timeout.
        if age > GENERATION_TIMEOUT_S:
            _fail(db, gen, str(exc))
        return
    gen.local_path = dest.name
    if settings.cloudinary_url:
        try:
            gen.video_url = media_hosting.upload_video(str(dest), public_id=f"studio-video-{gen.id}")
        except Exception:  # hosting is a bonus; the local file is already safe
            log.warning("cloudinary upload failed for video generation %s", gen.id, exc_info=True)
    gen.status = "succeeded"
    gen.error = None
    gen.completed_at = utcnow()
    db.commit()


def _fail(db: Session, gen: VideoGeneration, message: str) -> None:
    gen.status = "failed"
    gen.error = _clean(message)
    gen.completed_at = utcnow()
    db.commit()


def _clean(message: str) -> str:
    for secret in (settings.gemini_api_key, settings.hf_api_key_id, settings.hf_api_key_secret, settings.muapi_api_key):
        if secret:
            message = message.replace(secret, "***")
    return message.strip()[:500]


def _age_seconds(gen: VideoGeneration) -> float:
    created = gen.created_at
    if created.tzinfo is None:  # SQLite hands back naive UTC
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()

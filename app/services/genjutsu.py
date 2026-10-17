"""Higgsfield Genjutsu motion transfer: upload a video + 1-8 reference photos, get the same motion performed by the
people in the photos (the technique behind the "Made with Higgsfield Genjutsu" football videos).

Flow: validate -> create a DB row + job folder (storage/genjutsu/<id>/) -> background task uploads the inputs through
the official SDK, submits, stores the request id, polls to a terminal status, downloads the result, logs the cost.
The request id is persisted before polling, so a restarted server resumes instead of paying twice. `subscribe()` is
deliberately NOT used: it never raises for failed/moderated jobs, and we need the status, request id and cancel.
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from app.config import STORAGE_DIR, settings
from app.database import SessionLocal
from app.models import GenjutsuJob
from app.services.costs import ledger, prices
from app.services.studio.kit import ffmpeg

log = logging.getLogger(__name__)

GENJUTSU_DIR = STORAGE_DIR / "genjutsu"
MODEL = "higgsfield/genjutsu/motion-transfer/v1.0"
RESOLUTIONS = ("480p", "720p", "1080p")
MIN_VIDEO_S = 4.0  # the API rejects shorter source videos
MAX_BILLED_S = 30  # longer videos are trimmed to 30 s by the API
MAX_IMAGES = 8
MAX_PROMPT_CHARS = 10_000
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_SIDE = 2048
TRANSCODE_ABOVE_BYTES = 80 * 1024 * 1024  # big/odd-format sources are re-encoded before upload
POLL_INTERVAL_S = 5.0
TIMEOUT_S = 30 * 60
MAX_STATUS_ERRORS = 6  # consecutive transient failures while polling before giving up

ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "failed", "moderated", "canceled")

_inflight: set[int] = set()
_inflight_lock = threading.Lock()


class GenjutsuNotConfigured(RuntimeError):
    pass


class GenjutsuInputError(ValueError):
    """The user's inputs are unusable; the message says exactly what to change."""


class CostChanged(RuntimeError):
    def __init__(self, expected: float, actual: float):
        super().__init__(f"The cost changed: you confirmed ${expected:.2f} but this job would cost ${actual:.2f}. Review it and confirm again.")
        self.expected, self.actual = expected, actual


# --- credentials / SDK ---


def credentials() -> str:
    """'key-id:key-secret' from HF_KEY, or from the older HF_API_KEY_ID / HF_API_KEY_SECRET pair. Never logged."""
    key = (settings.hf_key or "").strip()
    if ":" in key and all(part.strip() for part in key.split(":", 1)):
        return key
    if settings.hf_api_key_id and settings.hf_api_key_secret:
        return f"{settings.hf_api_key_id}:{settings.hf_api_key_secret}"
    return ""


def missing_keys() -> list[str]:
    return [] if credentials() else ["HF_KEY (format key-id:key-secret)"]


def _sdk():
    creds = credentials()
    if not creds:
        raise GenjutsuNotConfigured("Higgsfield is not configured: set HF_KEY=<key-id>:<key-secret> in .env.")
    os.environ["HF_KEY"] = creds  # the official SDK reads its credential from the process environment
    import higgsfield_client

    return higgsfield_client


def friendly_error(exc: BaseException | str) -> str:
    text = str(exc)
    low = text.lower()
    if "not_enough_credits" in low or "insufficient" in low or "402" in low:
        return "Your Higgsfield API balance is empty. Top it up at console.higgsfield.ai (a subscription does not include API credits), then retry."
    if "401" in low or "403" in low or "unauthor" in low or "forbidden" in low:
        return "Higgsfield rejected the API key. Check HF_KEY (key-id:key-secret)."
    return (text or type(exc).__name__)[:300]


# --- cost ---


def billed_seconds(duration: float) -> int:
    return min(MAX_BILLED_S, max(1, math.ceil(duration - 1e-9)))


def estimate_cost(duration: float, resolution: str) -> float:
    return round(billed_seconds(duration) * prices.usd(f"higgsfield.genjutsu.{resolution}"), 4)


# --- input handling ---


def job_dir(job_id: int) -> Path:
    return GENJUTSU_DIR / str(job_id)


def prepare_image(data: bytes, name: str) -> bytes:
    """Reference photo -> upright RGB JPEG, long side <= 2048 px, metadata stripped."""
    if len(data) > MAX_IMAGE_BYTES:
        raise GenjutsuInputError(f"'{name}' is larger than 30 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise GenjutsuInputError(f"'{name}' is not a readable image (use JPG, PNG or WebP).") from exc
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def probe_video(path: Path, name: str) -> float:
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        raise GenjutsuInputError(f"'{name}' is not a readable video file.") from exc
    if not info.width or not info.height:
        raise GenjutsuInputError(f"'{name}' has no video track.")
    if info.duration < MIN_VIDEO_S:
        raise GenjutsuInputError(f"'{name}' is only {info.duration:.1f} s long; Genjutsu needs a source video of at least {MIN_VIDEO_S:.0f} s.")
    return info.duration


def create_job(
    db: Session,
    *,
    video_tmp: Path,
    video_name: str,
    images: list[tuple[str, bytes]],
    prompt: str,
    resolution: str,
    confirm_cost_usd: float,
) -> GenjutsuJob:
    """Validates everything, then creates the row and moves the inputs into the job folder. Raises
    GenjutsuInputError (400), GenjutsuNotConfigured (503) or CostChanged (409); nothing is created on failure."""
    if not credentials():
        raise GenjutsuNotConfigured("Higgsfield is not configured: set HF_KEY=<key-id>:<key-secret> in .env.")
    if resolution not in RESOLUTIONS:
        raise GenjutsuInputError(f"Resolution must be one of {', '.join(RESOLUTIONS)}.")
    prompt = (prompt or "").strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        raise GenjutsuInputError(f"The prompt is too long ({len(prompt)} characters; the limit is {MAX_PROMPT_CHARS}).")
    if not 1 <= len(images) <= MAX_IMAGES:
        raise GenjutsuInputError(f"Add 1 to {MAX_IMAGES} reference photos (got {len(images)}).")
    prepared = [prepare_image(data, name) for name, data in images]
    duration = probe_video(video_tmp, video_name)

    seconds = billed_seconds(duration)
    cost = estimate_cost(duration, resolution)
    if cost > float(confirm_cost_usd) + 0.01:
        raise CostChanged(float(confirm_cost_usd), cost)

    job = GenjutsuJob(
        prompt=prompt, resolution=resolution, status="queued", video_name=video_name[:200], image_count=len(prepared),
        input_seconds=round(duration, 2), billed_seconds=seconds, est_cost_usd=cost,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    folder = job_dir(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.move(str(video_tmp), folder / f"input{video_tmp.suffix.lower() or '.mp4'}")
    for i, jpeg in enumerate(prepared, 1):
        (folder / f"ref{i}.jpg").write_bytes(jpeg)
    return job


def clone_inputs(db: Session, source: GenjutsuJob, confirm_cost_usd: float) -> GenjutsuJob:
    """Retry = a new job from the stored inputs (so the cost guard applies again)."""
    folder = job_dir(source.id)
    inputs = sorted(folder.glob("input.*"))
    refs = sorted(folder.glob("ref*.jpg"))
    if not inputs or not refs:
        raise GenjutsuInputError("The original files are gone; upload them again.")
    tmp = GENJUTSU_DIR / "_tmp" / f"retry-{source.id}-{int(time.time())}{inputs[0].suffix}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(inputs[0], tmp)
    try:
        return create_job(
            db, video_tmp=tmp, video_name=source.video_name or inputs[0].name,
            images=[(p.name, p.read_bytes()) for p in refs], prompt=source.prompt or "",
            resolution=source.resolution, confirm_cost_usd=confirm_cost_usd,
        )
    finally:
        tmp.unlink(missing_ok=True)


def delete_job(db: Session, job: GenjutsuJob) -> None:
    shutil.rmtree(job_dir(job.id), ignore_errors=True)
    db.delete(job)
    db.commit()


def output_file(job: GenjutsuJob) -> Path | None:
    if not job.local_path:
        return None
    path = job_dir(job.id) / job.local_path
    return path if path.is_file() else None


# --- the background job ---


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(db: Session, job: GenjutsuJob, status: str, message: str, *, spent: float | None = 0.0) -> None:
    job.status, job.error, job.completed_at = status, message, _now()
    db.commit()
    _log_cost(job, spent)


def _log_cost(job: GenjutsuJob, actual: float | None) -> None:
    ledger.log_event(
        "genjutsu.motion_transfer",
        {"duration_seconds": job.input_seconds, "resolution": job.resolution},
        ref_type="genjutsu", ref_id=job.id, actual_usd=actual,
        details={"status": job.status, "billed_seconds": job.billed_seconds, "request_id": job.request_id},
    )


def _prepare_video(job: GenjutsuJob) -> Path:
    """The file to upload: the original, unless it is huge or in an unusual container (then a 1080p H.264 copy)."""
    folder = job_dir(job.id)
    src = next(iter(sorted(folder.glob("input.*"))), None)
    if src is None:
        raise GenjutsuInputError("The uploaded video is missing from disk; upload it again.")
    if src.suffix.lower() in (".mp4", ".mov") and src.stat().st_size <= TRANSCODE_ABOVE_BYTES:
        return src
    dst = folder / "prepared.mp4"
    ffmpeg.run(
        ["-i", src, "-t", str(MAX_BILLED_S), "-vf", "scale=-2:'min(1080,ih)'", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", dst],
        timeout=900,
    )
    return dst


def _download(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=180)) as resp:
        resp.raise_for_status()
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    if part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError("Higgsfield returned an empty video file.")
    part.replace(dest)


def _video_url_from(result) -> str | None:
    video = result.get("video") if isinstance(result, dict) else None
    if isinstance(video, dict):
        video = video.get("url")
    return video if isinstance(video, str) and video.startswith("http") else None


def run_job(job_id: int) -> None:
    """Background entry point. Own DB session; safe to call again for a stuck job (it resumes by request id)."""
    with _inflight_lock:
        if job_id in _inflight:
            return
        _inflight.add(job_id)
    try:
        with SessionLocal() as db:
            job = db.get(GenjutsuJob, job_id)
            if job is None or job.status in TERMINAL:
                return
            try:
                _drive(db, job)
            except GenjutsuNotConfigured as exc:
                _fail(db, job, "failed", str(exc))
            except GenjutsuInputError as exc:
                _fail(db, job, "failed", str(exc))
            except Exception as exc:  # noqa: BLE001 - any SDK/network error must end in a clean, readable failed row
                log.warning("genjutsu job %s failed: %s", job_id, type(exc).__name__)
                _fail(db, job, "failed", friendly_error(exc))
    finally:
        with _inflight_lock:
            _inflight.discard(job_id)


def _drive(db: Session, job: GenjutsuJob) -> None:
    sdk = _sdk()
    job.status = "running"
    db.commit()
    if not job.request_id:
        video = _prepare_video(job)
        video_url = sdk.upload_file(str(video))
        image_urls = [sdk.upload_file(str(p)) for p in sorted(job_dir(job.id).glob("ref*.jpg"))]
        arguments = {"prompt": job.prompt or "", "video_url": video_url, "image_urls": image_urls, "resolution": job.resolution}
        controller = sdk.submit(MODEL, arguments=arguments)
        job.request_id = controller.request_id
        db.commit()  # persisted before polling: a restart resumes instead of resubmitting (and paying again)
    _poll(db, job, sdk)


def _poll(db: Session, job: GenjutsuJob, sdk) -> None:
    from higgsfield_client import Cancelled, Completed, Failed, NSFW

    created = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=timezone.utc)
    errors = 0
    while True:
        try:
            status = sdk.status(request_id=job.request_id)
            errors = 0
        except Exception as exc:  # noqa: BLE001 - brief network/API hiccups must not kill a paid job
            errors += 1
            if errors >= MAX_STATUS_ERRORS:
                raise
            log.info("genjutsu status check failed (%s), retrying", type(exc).__name__)
            time.sleep(POLL_INTERVAL_S)
            continue

        if isinstance(status, Completed):
            return _succeed(db, job, sdk)
        if isinstance(status, NSFW):
            return _fail(
                db, job, "moderated",
                "Higgsfield's moderation blocked this request, so no video was made. Try different photos or a different source video.",
                spent=0.0,
            )
        if isinstance(status, Cancelled):
            return _fail(db, job, "canceled", "The job was canceled before it ran; nothing was generated.", spent=0.0)
        if isinstance(status, Failed):
            detail = ""
            try:
                body = sdk.result(request_id=job.request_id)
                detail = str(body.get("error") or "") if isinstance(body, dict) else ""
            except Exception:  # noqa: BLE001 - the failure body is a nicety
                pass
            return _fail(db, job, "failed", f"Higgsfield could not generate the video. {friendly_error(detail) if detail else ''}".strip(), spent=0.0)

        if (_now() - created).total_seconds() > TIMEOUT_S:
            return _fail(
                db, job, "failed",
                f"Gave up after {TIMEOUT_S // 60} minutes. The request ({job.request_id}) may still finish on Higgsfield's side and be billed; check the console.",
                spent=None,
            )
        time.sleep(POLL_INTERVAL_S)


def _succeed(db: Session, job: GenjutsuJob, sdk) -> None:
    result = sdk.result(request_id=job.request_id)
    url = _video_url_from(result)
    if not url:
        return _fail(db, job, "failed", "Higgsfield reported success but returned no video URL.", spent=job.est_cost_usd)
    dest = job_dir(job.id) / "output.mp4"
    try:
        _download(url, dest)
    except Exception as exc:  # noqa: BLE001
        # The result lives on Higgsfield for a while; keep the URL so nothing paid is lost.
        job.video_url = url
        return _fail(db, job, "failed", f"The video was generated but downloading it failed ({friendly_error(exc)}). Its link: {url}", spent=job.est_cost_usd)
    job.local_path = "output.mp4"
    job.video_url = _host_copy(dest) or url
    job.status, job.error, job.completed_at = "succeeded", None, _now()
    db.commit()
    _log_cost(job, job.est_cost_usd)


def _host_copy(path: Path) -> str | None:
    """Optional durable public copy (Render's disk is ephemeral). Failure just means the local file is the only copy."""
    if not settings.cloudinary_url:
        return None
    try:
        from app.services import media_hosting

        return media_hosting.upload_video(str(path), public_id=f"genjutsu_{path.parent.name}")
    except Exception:  # noqa: BLE001
        log.info("genjutsu: cloudinary upload failed; keeping the local copy only")
        return None


def cancel_job(db: Session, job: GenjutsuJob) -> GenjutsuJob:
    """Only a not-yet-started request can be canceled by Higgsfield; otherwise it keeps running (and billing)."""
    if job.status not in ACTIVE:
        raise GenjutsuInputError("Only a queued or running job can be canceled.")
    if not job.request_id:
        _fail(db, job, "canceled", "Canceled before it was sent to Higgsfield; nothing was billed.", spent=0.0)
        return job
    _sdk().cancel(request_id=job.request_id)
    return job  # the polling task sees the Cancelled status and finishes the row


def reconcile(db: Session, job: GenjutsuJob) -> GenjutsuJob:
    """Self-heal after a restart/sleep: an active job with nothing polling it is resumed in the background."""
    if job.status not in ACTIVE:
        return job
    with _inflight_lock:
        if job.id in _inflight:
            return job
    created = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=timezone.utc)
    age = (_now() - created).total_seconds()
    if not job.request_id and age > 180:
        _fail(db, job, "failed", "The server restarted before this job was sent to Higgsfield, so nothing was billed. Retry it.", spent=0.0)
    elif job.request_id or age > 20:
        threading.Thread(target=run_job, args=(job.id,), daemon=True).start()
    return job

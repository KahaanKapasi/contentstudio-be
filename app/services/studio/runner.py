"""Stage runner for Video Studio projects.

Each engine is an ordered list of stages (see registry.py). The runner executes them in a background
thread with its own DB session, records progress, and keeps everything needed to resume in
`project.assets` (completed stage names, registered files, free-form stage data). `retry` re-enters at the
first stage that is not done; paid recipes pause at `awaiting_approval` after the plan phase.
"""

import json
import logging
import shutil
import threading
import time
from datetime import timezone
from pathlib import Path

from google.genai import errors as genai_errors
from sqlalchemy.orm import Session

from app.config import PROJECTS_DIR, settings
from app.database import SessionLocal
from app.models import VideoProject, utcnow
from app.services import media_hosting
from app.services.costs import ledger
from app.services.gemini_client import GeminiNotConfigured
from app.services.studio import registry
from app.services.studio.kit.ffmpeg import FFmpegError
from app.services.studio.kit.images import ImageGenError
from app.services.studio.kit.stock import StockError
from app.services.studio.kit.tts import TTSError
from app.services.video_providers import ProviderError, ProviderNotConfigured, catalog

log = logging.getLogger(__name__)

ACTIVE = ("queued", "planning", "rendering")
OVERALL_TIMEOUT_S = 45 * 60
STALE_AFTER_S = 120  # an active row nobody in this process is working on, untouched this long, is orphaned
FINAL_NAME = registry.FINAL_NAME
_USER_ERRORS = (
    registry.StageError, FFmpegError, TTSError, StockError, ImageGenError, GeminiNotConfigured, ProviderError,
    ProviderNotConfigured, catalog.CatalogError,
)

_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


class Timeout(registry.StageError):
    pass


class Ctx:
    """What a stage sees: params, the project folder, a place to keep data/plan between stages."""

    def __init__(self, project: VideoProject, deadline: float, on_progress):
        self.project_id: int = project.id
        self.dir = project_dir(project.id)
        self.params: dict = json.loads(project.params or "{}")
        self.plan: dict = json.loads(project.plan or "{}")
        assets: dict = json.loads(project.assets or "{}")
        self.data: dict = assets.get("data", {})
        self.files: dict[str, dict] = assets.get("files", {})
        self.inputs: dict[str, list[Path]] = {k: [self.dir / p for p in v] for k, v in assets.get("inputs", {}).items()}
        self.deadline = deadline
        self._on_progress = on_progress

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def progress(self, fraction: float) -> None:
        """Progress inside the current stage, 0..1."""
        self._on_progress(max(0.0, min(fraction, 1.0)))

    def remaining(self) -> float:
        """Seconds left of the overall job budget; use as the timeout for long subprocesses."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Timeout(f"Timed out after {OVERALL_TIMEOUT_S // 60} minutes.")
        return left

    def register(self, name: str, path: Path, *, label: str | None = None, kind: str | None = None, preview: bool = False) -> None:
        """Record a produced file (relative to the project folder). `preview` files are served by /assets/{name}."""
        self.files[name] = {"path": str(path.relative_to(self.dir)), "label": label or name, "kind": kind or _kind(path), "preview": preview}

    def asset(self, name: str) -> Path | None:
        entry = self.files.get(name)
        return self.dir / entry["path"] if entry else None


def _kind(path: Path) -> str:
    ext = path.suffix.lower()
    return "audio" if ext in (".wav", ".mp3", ".m4a", ".ogg") else "video" if ext in (".mp4", ".mov", ".webm") else "image"


# --- bookkeeping ---


def _assets(project: VideoProject) -> dict:
    return json.loads(project.assets or "{}")


def _save(db: Session, project: VideoProject, **fields) -> None:
    for key, value in fields.items():
        setattr(project, key, value)
    db.commit()


def _persist(db: Session, project: VideoProject, ctx: Ctx, done: list[str], **fields) -> None:
    assets = _assets(project)
    assets.update({"done": done, "data": ctx.data, "files": ctx.files, "touched": time.time()})
    _save(db, project, assets=json.dumps(assets), plan=json.dumps(ctx.plan), **fields)


def clean(message: str) -> str:
    for secret in (settings.gemini_api_key, settings.pexels_api_key, settings.pixabay_api_key, settings.muapi_api_key, settings.hf_api_key_secret):
        if secret:
            message = message.replace(secret, "***")
    return message.strip()[:500]


# --- public lifecycle ---


def create_project(db: Session, *, engine: registry.Engine, recipe: registry.RecipeSpec, params: dict, auto_approve: bool, saved_inputs: dict[str, list[str]] | None = None) -> VideoProject:
    project = VideoProject(
        engine=engine.id, recipe=recipe.id if len(engine.recipes) > 1 else None, title=engine.title(recipe, params)[:120],
        params=json.dumps(params), plan=json.dumps({}),
        assets=json.dumps({"done": [], "approved": auto_approve or not recipe.paid, "auto_approve": auto_approve, "inputs": saved_inputs or {}, "files": {}, "data": {}, "touched": time.time()}),
        status="queued", stage="Queued", progress=0,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def approve(db: Session, project: VideoProject) -> VideoProject:
    if project.status != "awaiting_approval":
        raise ValueError("Only a project awaiting approval can be approved.")
    assets = _assets(project)
    assets["approved"] = True
    _save(db, project, assets=json.dumps(assets), status="queued", stage="Queued")
    return project


def replan(db: Session, project: VideoProject) -> VideoProject:
    if project.status not in ("awaiting_approval", "failed"):
        raise ValueError("Only a project awaiting approval or a failed one can be re-planned.")
    engine = registry.engines()[project.engine]
    recipe = engine.recipe(project.recipe)
    stages = engine.stages(recipe, json.loads(project.params or "{}"))
    first_plan = next((i for i, s in enumerate(stages) if s.phase == "plan"), 0)
    assets = _assets(project)
    assets["done"] = [s.name for s in stages[:first_plan]]
    assets["approved"] = bool(assets.get("auto_approve") or not recipe.paid)
    _save(db, project, assets=json.dumps(assets), plan=json.dumps({}), status="queued", stage="Queued", progress=0, error=None, completed_at=None)
    return project


def retry(db: Session, project: VideoProject) -> VideoProject:
    if project.status != "failed":
        raise ValueError("Only failed projects can be retried.")
    _save(db, project, status="queued", stage="Queued", error=None, completed_at=None)
    return project


def delete(db: Session, project: VideoProject) -> None:
    if is_running(project.id):
        raise ValueError("This project is still working. Wait for it to finish or fail.")
    shutil.rmtree(project_dir(project.id), ignore_errors=True)
    db.delete(project)
    db.commit()


def is_running(project_id: int) -> bool:
    return project_id in _inflight


def reconcile(db: Session, project: VideoProject) -> VideoProject:
    """Fail an active project that no thread in this process is working on (server restarted mid-run)."""
    if project.status in ACTIVE and not is_running(project.id):
        touched = _assets(project).get("touched") or _created_ts(project)
        if time.time() - touched > STALE_AFTER_S:
            _save(db, project, status="failed", error="The server restarted while this was running. Retry to resume from the last finished step.", completed_at=utcnow())
    return project


def _created_ts(project: VideoProject) -> float:
    created = project.created_at
    return (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created).timestamp()


def local_file(project: VideoProject) -> Path | None:
    if not project.local_path:
        return None
    path = project_dir(project.id) / Path(project.local_path).name
    return path if path.is_file() else None


# --- execution ---


def run_project(project_id: int) -> None:
    """Background-task entry point (own session). Runs stages until done, paused for approval, or failed."""
    with _inflight_lock:
        if project_id in _inflight:
            return
        _inflight.add(project_id)
    db = SessionLocal()
    try:
        _execute(db, project_id)
    except Exception:  # last resort: never leave a row stuck in an active state
        log.exception("studio project %s crashed", project_id)
        db.rollback()
        project = db.get(VideoProject, project_id)
        if project is not None and project.status in ACTIVE:
            _save(db, project, status="failed", error="Something went wrong while rendering. Retry to resume.", completed_at=utcnow())
    finally:
        db.close()
        with _inflight_lock:
            _inflight.discard(project_id)


def _execute(db: Session, project_id: int) -> None:
    project = db.get(VideoProject, project_id)
    if project is None or project.status != "queued":
        return
    engine = registry.engines()[project.engine]
    recipe = engine.recipe(project.recipe)
    params = json.loads(project.params or "{}")
    stages = engine.stages(recipe, params)
    total = sum(s.weight for s in stages)
    done: list[str] = list(_assets(project).get("done", []))
    approved = bool(_assets(project).get("approved"))
    deadline = time.monotonic() + OVERALL_TIMEOUT_S
    finished_weight = sum(s.weight for s in stages if s.name in done)

    state = {"stage": None, "weight": 0, "last": 0.0}

    def on_progress(fraction: float) -> None:
        now = time.monotonic()
        if now - state["last"] < 1.0:  # throttle DB writes
            return
        state["last"] = now
        pct = int(100 * (finished_weight + fraction * state["weight"]) / total)
        _save(db, project, progress=min(pct, 99))

    ctx = Ctx(project, deadline, on_progress)
    for stage in stages:
        if stage.name in done:
            continue
        if stage.phase == "render" and not approved:
            cost = engine.estimate_cost(recipe, params, ctx.plan)
            _persist(db, project, ctx, done, status="awaiting_approval", stage="Awaiting approval", progress=int(100 * finished_weight / total), estimated_cost_usd=cost)
            return
        state.update(stage=stage.name, weight=stage.weight, last=0.0)
        _persist(db, project, ctx, done, status="planning" if stage.phase == "plan" else "rendering", stage=stage.name, progress=int(100 * finished_weight / total))
        try:
            ctx.remaining()
            stage.fn(ctx)
        except genai_errors.APIError as exc:
            return _fail(db, project, ctx, done, f"Gemini request failed ({getattr(exc, 'code', 'error')}) during '{stage.name}'. Retry to resume.")
        except _USER_ERRORS as exc:
            return _fail(db, project, ctx, done, str(exc))
        except Exception:
            log.exception("studio project %s failed in stage %r", project_id, stage.name)
            return _fail(db, project, ctx, done, f"Something went wrong during '{stage.name}'. Retry to resume.")
        finished_weight += stage.weight
        done.append(stage.name)
        _persist(db, project, ctx, done, progress=min(int(100 * finished_weight / total), 99))

    final = ctx.dir / FINAL_NAME
    if not final.is_file():
        return _fail(db, project, ctx, done, "The render finished without producing a video. Please retry.")
    video_url = None
    if settings.cloudinary_url:
        try:
            video_url = media_hosting.upload_video(str(final), public_id=f"studio-project-{project.id}")
        except Exception:  # hosting is a bonus; the local file is safe
            log.warning("cloudinary upload failed for studio project %s", project.id, exc_info=True)
    _persist(db, project, ctx, done, status="succeeded", stage="Done", progress=100, local_path=FINAL_NAME, video_url=video_url, error=None, completed_at=utcnow())
    ledger.log_event(
        "studio.project",
        {"engine": project.engine, "recipe": project.recipe, "params": params, "estimated_usd": project.estimated_cost_usd},
        ref_type="video_project", ref_id=project.id,
        details={"engine": project.engine, "recipe": project.recipe},
    )


def _fail(db: Session, project: VideoProject, ctx: Ctx, done: list[str], message: str) -> None:
    db.rollback()
    _persist(db, project, ctx, done, status="failed", error=clean(message), completed_at=utcnow())


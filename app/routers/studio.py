"""Video Studio engines API (docs/10_Video_Studio_Engines.md). Jobs run in a background thread via the runner."""

import json
import mimetypes
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import VideoProject
from app.schemas import StudioProjectOut
from app.services.studio import registry, runner

router = APIRouter(prefix="/api/studio", tags=["studio"])


def _out(project: VideoProject) -> StudioProjectOut:
    assets = json.loads(project.assets or "{}")
    plan = json.loads(project.plan or "{}")
    previews = [
        {"name": name, "label": f["label"], "kind": f["kind"]}
        for name, f in (assets.get("files") or {}).items()
        if f.get("preview")
    ]
    return StudioProjectOut.model_validate(
        {
            **{c.name: getattr(project, c.name) for c in project.__table__.columns if c.name not in ("params", "plan", "assets")},
            "params": json.loads(project.params or "{}"),
            "plan": plan if plan else None,
            "previews": previews,
            "has_file": runner.local_file(project) is not None,
            # +-25% around the engine's point estimate (same widening as POST /api/costs/estimate)
            "estimated_cost_low_usd": None if project.estimated_cost_usd is None else round(project.estimated_cost_usd * 0.75, 4),
            "estimated_cost_high_usd": None if project.estimated_cost_usd is None else round(project.estimated_cost_usd * 1.25, 4),
        }
    )


def _get(db: Session, project_id: int) -> VideoProject:
    project = db.get(VideoProject, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/engines")
def list_engines():
    return registry.listing()


@router.post("/projects", response_model=StudioProjectOut)
async def create_project(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    form = await request.form()
    engine = registry.engines().get(str(form.get("engine") or ""))
    if engine is None:
        raise HTTPException(status_code=400, detail="Unknown engine. See GET /api/studio/engines.")
    recipe = engine.recipe(str(form.get("recipe") or "") or None)
    if recipe is None:
        raise HTTPException(status_code=400, detail=f"'recipe' is required for engine '{engine.id}': one of {', '.join(r.id for r in engine.recipes)}.")
    if not engine.implemented:
        raise HTTPException(status_code=501, detail=f"The '{engine.id}' engine is coming soon.")
    try:
        raw = json.loads(str(form.get("params") or "{}"))
        if not isinstance(raw, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="'params' must be a JSON object.") from None
    uploads: dict[str, list] = {}
    for name, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            uploads.setdefault(name, []).append(value)
    known_files = {f.name for f in recipe.fields if f.type in registry.FILE_TYPES}
    uploads = {k: v for k, v in uploads.items() if k in known_files}
    spec_by_name = {f.name: f for f in recipe.fields}
    try:
        params = engine.validate(recipe, raw, {k: [u.filename for u in v] for k, v in uploads.items()})
    except registry.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    missing = registry.missing_env([*recipe.keys, *engine.extra_keys(recipe, params)])
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing environment variables: {', '.join(missing)}. Set them in .env.")

    auto = str(form.get("auto_approve") or "").lower() in ("1", "true", "on", "yes")
    project = runner.create_project(db, engine=engine, recipe=recipe, params=params, auto_approve=auto)
    try:
        saved = await _save_uploads(project.id, uploads, spec_by_name)
    except ValueError as exc:
        runner.delete(db, project)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if saved:
        assets = json.loads(project.assets)
        assets["inputs"] = saved
        project.assets = json.dumps(assets)
        db.commit()
    background.add_task(runner.run_project, project.id)
    return _out(project)


async def _save_uploads(project_id: int, uploads: dict[str, list], specs: dict) -> dict[str, list[str]]:
    saved: dict[str, list[str]] = {}
    folder = runner.project_dir(project_id) / "inputs"
    for field, files in uploads.items():
        limit = registry.FILE_MAX_BYTES[specs[field].type]
        for i, upload in enumerate(files):
            suffix = re.sub(r"[^.a-z0-9]", "", Path(upload.filename).suffix.lower())[:8]
            dest = folder / f"{field}_{i}{suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with dest.open("wb") as fh:
                while chunk := await upload.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        shutil.rmtree(folder.parent, ignore_errors=True)
                        raise ValueError(f"'{field}' is larger than {limit >> 20} MB.")
                    fh.write(chunk)
            saved.setdefault(field, []).append(str(dest.relative_to(folder.parent)))
    return saved


@router.get("/projects", response_model=list[StudioProjectOut])
def list_projects(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.query(VideoProject).order_by(VideoProject.created_at.desc(), VideoProject.id.desc()).limit(max(1, min(limit, 200))).all()
    return [_out(runner.reconcile(db, p)) for p in rows]


@router.get("/projects/{project_id}", response_model=StudioProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    return _out(runner.reconcile(db, _get(db, project_id)))


def _transition(fn, db: Session, project_id: int, background: BackgroundTasks) -> StudioProjectOut:
    project = _get(db, project_id)
    try:
        fn(db, project)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background.add_task(runner.run_project, project.id)
    return _out(project)


@router.post("/projects/{project_id}/approve", response_model=StudioProjectOut)
def approve_project(project_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    return _transition(runner.approve, db, project_id, background)


@router.post("/projects/{project_id}/replan", response_model=StudioProjectOut)
def replan_project(project_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    return _transition(runner.replan, db, project_id, background)


@router.post("/projects/{project_id}/retry", response_model=StudioProjectOut)
def retry_project(project_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    return _transition(runner.retry, db, project_id, background)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)):
    project = _get(db, project_id)
    try:
        runner.delete(db, project)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get("/projects/{project_id}/file")
def project_file(project_id: int, db: Session = Depends(get_db)):
    project = _get(db, project_id)
    path = runner.local_file(project)
    if path:
        return FileResponse(path, media_type="video/mp4", filename=f"studio-{project.id}.mp4", content_disposition_type="inline")
    if project.video_url:
        return RedirectResponse(project.video_url)
    raise HTTPException(status_code=404, detail="No video file for this project")


@router.get("/projects/{project_id}/assets/{name}")
def project_asset(project_id: int, name: str, db: Session = Depends(get_db)):
    project = _get(db, project_id)
    entry = (json.loads(project.assets or "{}").get("files") or {}).get(name)
    path = runner.project_dir(project.id) / entry["path"] if entry and entry.get("preview") else None
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", content_disposition_type="inline")

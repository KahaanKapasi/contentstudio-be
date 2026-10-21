import json
import threading

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Template
from app import auth
from app.auth import require_access
from app.migrations import run_migrations
from app.routers import articles, costs, dashboard, discovery, footonomy, posts, studio, video
from app.services import discovery as discovery_service, ig_token, image_processing

app = FastAPI(title="Content Studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://studiodecontent.vercel.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
for _router in (discovery.router, articles.router, posts.router, dashboard.router, video.router, studio.router, costs.router, footonomy.router):
    app.include_router(_router, dependencies=[Depends(require_access)])

# The 3 fixed templates per 04_Posts_Carousel_Studio.md. A 4th template was
# mentioned as possibly existing but not fully specified — not built until
# confirmed (open item in that doc).
DEFAULT_TEMPLATES = [
    {
        "name": "Darkened background",
        "layout_config": {
            "renderer": "darkened_background",
            "text": {"position": "center", "max_lines": 5, "font_size": 64},
            "watermark": {"position": "bottom_right"},
        },
    },
    {
        "name": "Transparency overlay",
        "layout_config": {
            "renderer": "transparency_overlay",
            "text": {"position": "center", "max_lines": 5, "font_size": 60},
            "image": {"opacity": 0.45, "saturation": 0.35},
        },
    },
    {
        "name": "Background-removed",
        "layout_config": {
            "renderer": "background_removed",
            "text": {"position": "below_subject", "max_lines": 5, "font_size": 56},
            "background": {"color": "#000000"},
        },
    },
]


def seed_templates():
    db = SessionLocal()
    try:
        if db.query(Template).count() == 0:
            for t in DEFAULT_TEMPLATES:
                db.add(Template(name=t["name"], layout_config=json.dumps(t["layout_config"])))
            db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    seed_templates()
    # Off the request path: a due refresh is one HTTP call and must not delay cold start.
    threading.Thread(target=ig_token.refresh_if_due, daemon=True).start()
    discovery_service.fail_stale_runs()
    discovery_service.backfill_hashes()
    discovery_service.start_scheduler()  # no-op unless AUTO_SCRAPE_MINUTES > 0
    image_processing.warm_up()  # loads the cutout model in a daemon thread so the first template click isn't cold


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Optional: serve the built frontend from this same process (self-hosted setup, see docs/12_Self_Hosting.md).
# Registered last so every /api route above wins over the catch-all.
_dist = Path(settings.frontend_dist) if settings.frontend_dist else None
if _dist and (_dist / "index.html").exists():
    if (_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="ui-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_ui(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (_dist / full_path).resolve()
        if full_path and candidate.is_file() and _dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")  # SPA route

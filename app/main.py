import json
import threading

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import Base, SessionLocal, engine
from app.models import Template
from app import auth
from app.auth import require_access
from app.routers import articles, dashboard, discovery, posts, video
from app.services import ig_token

app = FastAPI(title="Content Studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://studiodecontent.vercel.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
for _router in (discovery.router, articles.router, posts.router, dashboard.router, video.router):
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
    seed_templates()
    # Off the request path: a due refresh is one HTTP call and must not delay cold start.
    threading.Thread(target=ig_token.refresh_if_due, daemon=True).start()


@app.get("/api/health")
def health():
    return {"status": "ok"}

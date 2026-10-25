import hashlib
import json

import cloudinary.exceptions
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import CustomFont, PostDraft, Template
from app.schemas import PostDraftCreate, PostDraftOut, PostDraftUpdate, TemplateOut
from app.services import getty_client, image_processing, match_day, media_hosting
from app.services.costs import ledger
from app.services.gemini_client import GeminiNotConfigured, track_usage
from app.services.instagram_client import InstagramNotConfigured, publish_carousel, publish_single_image
from app.services.media_hosting import MediaHostingNotConfigured

router = APIRouter(prefix="/api/posts", tags=["posts"])


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)):
    return db.query(Template).all()


@router.get("/drafts", response_model=list[PostDraftOut])
def list_drafts(db: Session = Depends(get_db)):
    return db.query(PostDraft).order_by(PostDraft.created_at.desc()).all()


@router.post("/drafts", response_model=PostDraftOut)
def create_draft(payload: PostDraftCreate, db: Session = Depends(get_db)):
    draft = PostDraft(**payload.model_dump())
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


@router.patch("/drafts/{draft_id}", response_model=PostDraftOut)
def update_draft(draft_id: int, payload: PostDraftUpdate, db: Session = Depends(get_db)):
    draft = db.query(PostDraft).filter(PostDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "final_image_config" and value is not None:
            value = json.dumps(value)
        setattr(draft, key, value)
    db.commit()
    db.refresh(draft)
    return draft


@router.get("/aspect-ratios")
def list_aspect_ratios():
    """Canvas shape options for the Posts editor — kept backend-authoritative
    so the frontend never hardcodes pixel dimensions that could drift from
    what render-background/render-preview actually produce."""
    return [
        {"id": key, "width": w, "height": h}
        for key, (w, h) in image_processing.ASPECT_RATIOS.items()
    ]


@router.post("/render-preview")
async def render_preview(
    template_name: str,
    text: str,
    aspect_ratio: str = image_processing.DEFAULT_ASPECT_RATIO,
    image: UploadFile = File(...),
):
    """Auto-fit endpoint: applies the given template's treatment to the
    uploaded image + text, returns the composed JPEG directly (for the
    fabric.js editor's initial canvas load / live preview refresh)."""
    image_bytes = await image.read()
    try:
        rendered = image_processing.render_template(template_name, image_bytes, text, aspect_ratio)
    except image_processing.CutoutUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=rendered, media_type="image/jpeg")


@router.post("/render-background")
async def render_background(
    template_name: str,
    aspect_ratio: str = image_processing.DEFAULT_ASPECT_RATIO,
    image: UploadFile = File(...),
):
    """Treatment only, no text baked in — feeds the live fabric.js editor's
    canvas background so text stays a separately draggable layer client-side."""
    image_bytes = await image.read()
    try:
        rendered = image_processing.render_background(template_name, image_bytes, aspect_ratio)
    except image_processing.CutoutUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=rendered, media_type="image/jpeg")


@router.post("/cutout")
async def cutout(image: UploadFile = File(...)):
    """Subject on a transparent background (PNG, long side <= 1024). Cached by content hash, so the
    Posts editor can call this once per photo and lay the cutout out client-side."""
    image_bytes = await image.read()
    try:
        png = await run_in_threadpool(image_processing.cutout_png, image_bytes)
    except image_processing.CutoutUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # unreadable image / missing model
        raise HTTPException(status_code=400, detail=f"Could not cut out subject: {exc}") from exc
    return Response(content=png, media_type="image/png")


@router.post("/drafts/{draft_id}/publish")
def publish_draft(draft_id: int, db: Session = Depends(get_db)):
    """Uploads the final composed image(s) to Cloudinary, then publishes via
    the Instagram Graph API. final_image_config is expected to hold, at
    minimum, the composed image bytes location — for now this endpoint expects
    the frontend to have already rendered the final image client-side/via
    render-preview and stored a data reference; wire-up to the exact fabric.js
    export format happens on the frontend integration pass."""
    draft = db.query(PostDraft).filter(PostDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if not draft.final_image_config:
        raise HTTPException(status_code=400, detail="Draft has no final_image_config to publish")

    config = json.loads(draft.final_image_config)
    image_urls = config.get("hosted_image_urls")
    if not image_urls:
        raise HTTPException(status_code=400, detail="final_image_config missing hosted_image_urls")

    try:
        if len(image_urls) > 1:
            media_id = publish_carousel(image_urls, draft.final_text or "")
        else:
            media_id = publish_single_image(image_urls[0], draft.final_text or "")
    except (InstagramNotConfigured, MediaHostingNotConfigured) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    draft.status = "published"
    db.commit()
    return {"media_id": media_id}


@router.post("/match-scrape", response_model=list[PostDraftOut])
def match_scrape(team: str = "Real Madrid", db: Session = Depends(get_db)):
    """Runs the match-day scraping + opinion generation, persisting each
    suggestion as its own PostDraft (source=match_scrape) so the Posts
    screen's suggestions list is just the normal drafts list."""
    try:
        with track_usage() as usage:
            opinions = match_day.suggest_opinions(team)
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    created = []
    for opinion in opinions:
        draft = PostDraft(source="match_scrape", suggested_opinion_text=opinion, status="draft")
        db.add(draft)
        created.append(draft)
    db.commit()
    for d in created:
        db.refresh(d)
    if usage or opinions:
        # X search reads are not metered per call, so the ledger keeps the estimate midpoint and notes the measured Gemini part.
        ledger.log_event("posts.match_scrape", details={"team": team, "drafts": len(created), "gemini_actual_usd": ledger.usage_cost_usd(usage)})
    return created


@router.get("/getty-search")
async def getty_search(query: str):
    """Always fails — see app/services/getty_client.py docstring: Getty
    blocks headless/automated access with a bot-detection challenge, verified
    directly against the live site. Kept as a route (rather than removed) so
    the frontend has one clear place to surface that explanation to the user,
    who should download from gettyimages.com manually and use the normal
    image-upload input instead."""
    try:
        await getty_client.search_preview_images(query)
    except getty_client.GettyBlocked as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@router.get("/hosting-status")
def hosting_status():
    """What publishing and Send to phone need, and which of it is missing. Cheap and side-effect free, so the editor
    can check before it renders heavy files."""
    ig_missing = [
        name
        for name, value in (("IG_ACCESS_TOKEN", settings.ig_access_token), ("IG_BUSINESS_ACCOUNT_ID", settings.ig_business_account_id))
        if not value
    ]
    return {"hosting": media_hosting.status(), "instagram": {"configured": not ig_missing, "missing": ig_missing}}


@router.post("/upload-to-host")
async def upload_to_host(image: UploadFile = File(...)):
    """Uploads a composed image to Cloudinary and returns the public URL,
    for use in final_image_config.hosted_image_urls before publish."""
    image_bytes = await image.read()
    if len(image_bytes) > media_hosting.MAX_IMAGE_BYTES:
        mb = len(image_bytes) / 1024 / 1024
        raise HTTPException(status_code=413, detail=f"This image is {mb:.1f} MB; Cloudinary's free plan accepts at most 10 MB per image.")
    try:
        url = await run_in_threadpool(media_hosting.upload_image, image_bytes)  # big PNGs: don't block the event loop
    except MediaHostingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except cloudinary.exceptions.Error as exc:
        too_big = "too large" in str(exc).lower()
        raise HTTPException(status_code=413 if too_big else 502, detail=f"Cloudinary rejected the upload: {exc}") from exc
    return {"url": url}


# --- Custom fonts (Posts editor) ----------------------------------------------------------------------------------

FONT_MAX_BYTES = 12 * 1024 * 1024
FONT_FORMATS = {"ttf", "otf", "woff", "woff2"}


def _sniff_font(data: bytes) -> str | None:
    """Format from the file's magic bytes, so a renamed .png can't be stored as a font."""
    head = data[:4]
    if head == b"wOFF":
        return "woff"
    if head == b"wOF2":
        return "woff2"
    if head == b"OTTO":
        return "otf"
    if head in (b"\x00\x01\x00\x00", b"true"):
        return "ttf"
    return None


def _font_out(f: CustomFont) -> dict:
    return {"id": f.id, "family": f.family, "format": f.format, "size": f.size, "sha256": f.sha256}


@router.get("/fonts")
def list_fonts(db: Session = Depends(get_db)):
    return [_font_out(f) for f in db.query(CustomFont).order_by(CustomFont.created_at.desc()).all()]


@router.post("/fonts")
async def upload_font(font: UploadFile = File(...), family: str = Form(""), db: Session = Depends(get_db)):
    data = await font.read()
    ext = (font.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in FONT_FORMATS:
        raise HTTPException(status_code=400, detail="Fonts must be TTF, OTF, WOFF or WOFF2 files")
    if len(data) > FONT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Font file is larger than 12 MB")
    fmt = _sniff_font(data)
    if not fmt:
        raise HTTPException(status_code=400, detail="That file is not a valid font")
    digest = hashlib.sha256(data).hexdigest()
    existing = db.query(CustomFont).filter(CustomFont.sha256 == digest).first()
    if existing:
        return _font_out(existing)
    name = (family or "").strip() or (font.filename or "Custom font").rsplit(".", 1)[0]
    row = CustomFont(family=name[:120], format=fmt, size=len(data), sha256=digest, data=data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _font_out(row)


@router.get("/fonts/{font_id}/file")
def font_file(font_id: int, db: Session = Depends(get_db)):
    row = db.get(CustomFont, font_id)
    if not row:
        raise HTTPException(status_code=404, detail="Font not found")
    return Response(content=row.data, media_type=f"font/{row.format}")


@router.delete("/fonts/{font_id}")
def delete_font(font_id: int, db: Session = Depends(get_db)):
    row = db.get(CustomFont, font_id)
    if not row:
        raise HTTPException(status_code=404, detail="Font not found")
    db.delete(row)
    db.commit()
    return {"ok": True}

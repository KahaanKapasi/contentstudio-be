import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PostDraft, Template
from app.schemas import PostDraftCreate, PostDraftOut, PostDraftUpdate, TemplateOut
from app.services import getty_client, image_processing, match_day, media_hosting
from app.services.gemini_client import GeminiNotConfigured
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=rendered, media_type="image/jpeg")


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


@router.post("/upload-to-host")
async def upload_to_host(image: UploadFile = File(...)):
    """Uploads a composed image to Cloudinary and returns the public URL,
    for use in final_image_config.hosted_image_urls before publish."""
    image_bytes = await image.read()
    try:
        url = media_hosting.upload_image(image_bytes)
    except MediaHostingNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"url": url}

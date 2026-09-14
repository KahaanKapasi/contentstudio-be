import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Article
from app.schemas import ArticleGenerateRequest, ArticleOut, ArticleUpdate
from app.services import article_pipeline
from app.services.gemini_client import GeminiNotConfigured

router = APIRouter(prefix="/api/articles", tags=["articles"])


@router.get("", response_model=list[ArticleOut])
def list_articles(db: Session = Depends(get_db)):
    return db.query(Article).order_by(Article.created_at.desc()).all()


@router.get("/{article_id}", response_model=ArticleOut)
def get_article(article_id: int, db: Session = Depends(get_db)):
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@router.post("/generate", response_model=ArticleOut)
def generate(payload: ArticleGenerateRequest, db: Session = Depends(get_db)):
    try:
        return article_pipeline.generate_article(db, payload.topic_id)
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{article_id}", response_model=ArticleOut)
def update_article(article_id: int, payload: ArticleUpdate, db: Session = Depends(get_db)):
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key == "tags":
            value = json.dumps(value)
        setattr(article, key, value)

    # "Publish" here means marking status/timestamp only — the real publish
    # target (own blog vs. copy/paste vs. future Futonomy integration) is an
    # explicitly open item in 02_Article_Pipeline.md, not resolved by this build.
    if data.get("status") == "published" and article.published_at is None:
        article.published_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(article)
    return article


@router.post("/{article_id}/publish", response_model=ArticleOut)
def publish_article(article_id: int, db: Session = Depends(get_db)):
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    article.status = "published"
    if article.published_at is None:
        article.published_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(article)
    return article


@router.post("/{article_id}/regenerate", response_model=ArticleOut)
def regenerate(article_id: int, db: Session = Depends(get_db)):
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article or article.topic_id is None:
        raise HTTPException(status_code=404, detail="Article or its source topic not found")
    try:
        new_article = article_pipeline.generate_article(db, article.topic_id)
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.delete(article)
    db.commit()
    return new_article

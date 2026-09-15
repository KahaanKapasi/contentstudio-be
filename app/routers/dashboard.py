import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Article,
    InstagramMetricSnapshot,
    KpiBaseline,
    PostDraft,
    TopicCandidate,
    TwitterMetricSnapshot,
    TwitterPostSuggestion,
)
from app.schemas import InstagramMetricOut, KpiBaselineIn, KpiBaselineOut, TwitterMetricOut, TwitterSuggestionOut
from app.services import instagram_client, twitter_client
from app.services.gemini_client import GeminiNotConfigured, generate_json
from app.services.instagram_client import InstagramNotConfigured
from app.services.twitter_client import TwitterNotConfigured

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/instagram/metrics", response_model=list[InstagramMetricOut])
def instagram_history(db: Session = Depends(get_db)):
    return db.query(InstagramMetricSnapshot).order_by(InstagramMetricSnapshot.captured_at).all()


@router.post("/instagram/refresh", response_model=InstagramMetricOut)
def instagram_refresh(db: Session = Depends(get_db)):
    try:
        data = instagram_client.get_account_metrics()
    except InstagramNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    snapshot = InstagramMetricSnapshot(
        followers=data.get("followers_count"),
        reach_30d=None,  # requires Insights API call with a time window — follow-up
        engagement_rate=None,
        top_post_ids=json.dumps([]),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.get("/twitter/metrics", response_model=list[TwitterMetricOut])
def twitter_history(db: Session = Depends(get_db)):
    return db.query(TwitterMetricSnapshot).order_by(TwitterMetricSnapshot.captured_at).all()


@router.post("/twitter/refresh", response_model=TwitterMetricOut)
def twitter_refresh(username: str, db: Session = Depends(get_db)):
    try:
        metrics = twitter_client.get_account_metrics(username)
    except TwitterNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    snapshot = TwitterMetricSnapshot(
        followers=metrics.get("followers_count"),
        impressions_30d=None,  # X API v2 doesn't expose this on the free/basic tier — needs tier confirmation
        engagement_rate=None,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.get("/twitter/suggestions", response_model=list[TwitterSuggestionOut])
def list_suggestions(db: Session = Depends(get_db)):
    return db.query(TwitterPostSuggestion).order_by(TwitterPostSuggestion.id.desc()).all()


@router.post("/twitter/suggestions/generate", response_model=list[TwitterSuggestionOut])
def generate_suggestions(db: Session = Depends(get_db)):
    """Reframes recent Discovery topics into short-form Twitter suggestions."""
    topics = db.query(TopicCandidate).filter(TopicCandidate.status != "discarded").order_by(TopicCandidate.created_at.desc()).limit(10).all()
    if not topics:
        return []

    topics_block = "\n".join(f"[{t.id}] {t.title} — {t.rationale}" for t in topics)
    prompt = f"""Reframe these football content topics into short, punchy Twitter/X posts \
(under 280 chars each, opinionated, no hashtag spam). Respond as a JSON array of objects \
with keys: topic_id (int, matching the bracketed id), draft_text.

Topics:
{topics_block}
"""
    try:
        raw = generate_json(prompt)
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    created = []
    for item in raw:
        suggestion = TwitterPostSuggestion(
            topic_id=item.get("topic_id"), draft_text=item.get("draft_text", ""), status="suggested"
        )
        db.add(suggestion)
        created.append(suggestion)
    db.commit()
    for s in created:
        db.refresh(s)
    return created


@router.post("/twitter/suggestions/{suggestion_id}/post", response_model=TwitterSuggestionOut)
def post_suggestion(suggestion_id: int, db: Session = Depends(get_db)):
    suggestion = db.query(TwitterPostSuggestion).filter(TwitterPostSuggestion.id == suggestion_id).first()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    try:
        twitter_client.post_tweet(suggestion.draft_text)
    except TwitterNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    suggestion.status = "posted"
    db.commit()
    db.refresh(suggestion)
    return suggestion


@router.get("/kpi-baseline", response_model=KpiBaselineOut | None)
def get_baseline(db: Session = Depends(get_db)):
    return db.query(KpiBaseline).order_by(KpiBaseline.created_at.desc()).first()


@router.put("/kpi-baseline", response_model=KpiBaselineOut)
def set_baseline(payload: KpiBaselineIn, db: Session = Depends(get_db)):
    """A single fixed reference point, not a logged series — see KpiBaseline's
    docstring. PUT always records a new row (keeping prior ones as history)
    but kpi-summary only ever reads the latest."""
    baseline = KpiBaseline(**payload.model_dump())
    db.add(baseline)
    db.commit()
    db.refresh(baseline)
    return baseline


def _weeks_since(start: datetime) -> float:
    now = datetime.now(timezone.utc)
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start
    now_naive = now.replace(tzinfo=None)
    days = max((now_naive - start_naive).total_seconds() / 86400, 1)
    return days / 7


@router.get("/kpi-summary")
def kpi_summary(db: Session = Depends(get_db)):
    """Posting cadence/output-volume signal. 'Since adoption' cadence is
    computed from this Studio's own records (articles + published post_drafts)
    against the earliest such record as a stand-in for the adoption date,
    since there's no explicit 'Studio launch' marker stored anywhere. The
    before/after comparison itself needs the manual baseline below — that
    data doesn't exist retroactively (open item in 05_Dashboard_Analytics.md)."""
    articles_published = db.query(Article).filter(Article.status == "published").count()
    posts_published = db.query(PostDraft).filter(PostDraft.status == "published").count()

    earliest = db.query(func.min(Article.created_at)).scalar()
    earliest_post = db.query(func.min(PostDraft.created_at)).scalar()
    if earliest_post and (not earliest or earliest_post < earliest):
        earliest = earliest_post

    posts_per_week_since = None
    if earliest and (articles_published + posts_published) > 0:
        posts_per_week_since = round((articles_published + posts_published) / _weeks_since(earliest), 2)

    baseline = db.query(KpiBaseline).order_by(KpiBaseline.created_at.desc()).first()

    return {
        "since_studio_adoption": {
            "articles_published": articles_published,
            "posts_published": posts_published,
            "posts_per_week": posts_per_week_since,
        },
        "pre_studio_baseline": {
            "label": baseline.label,
            "posts_per_week": baseline.posts_per_week,
            "avg_engagement_rate": baseline.avg_engagement_rate,
        }
        if baseline
        else None,
    }

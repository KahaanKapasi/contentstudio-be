from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScrapedItem(Base):
    __tablename__ = "scraped_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str] = mapped_column(String, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # sha1 of the normalized url (or text when there is none): lets a re-scrape skip items we already have.
    content_hash: Mapped[str] = mapped_column(String, nullable=True, index=True)
    workspace: Mapped[str] = mapped_column(String, default="footonomy", index=True)  # madridonomy | footonomy | general


class DiscoveryRun(Base):
    """One Discovery scrape: collection is synchronous, topic generation runs in the background.
    status: generating | done | skipped (no new items) | error."""

    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="generating")
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    topics_created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    source_health: Mapped[str] = mapped_column(Text, nullable=True)  # JSON
    cost_usd: Mapped[float] = mapped_column(Float, nullable=True)
    trigger: Mapped[str] = mapped_column(String, nullable=True)  # manual | auto
    workspace: Mapped[str] = mapped_column(String, default="footonomy")


class TopicCandidate(Base):
    __tablename__ = "topic_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)
    suitable_for: Mapped[str] = mapped_column(String, nullable=False)  # article, video, both
    status: Mapped[str] = mapped_column(String, default="new")  # new, selected, discarded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    workspace: Mapped[str] = mapped_column(String, default="footonomy", index=True)  # madridonomy | footonomy | general


class TopicSourceLink(Base):
    __tablename__ = "topic_source_links"

    topic_id: Mapped[int] = mapped_column(ForeignKey("topic_candidates.id"), primary_key=True)
    scraped_item_id: Mapped[int] = mapped_column(ForeignKey("scraped_items.id"), primary_key=True)


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic_candidates.id"), nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=True)
    meta_description: Mapped[str] = mapped_column(String, nullable=True)
    slug: Mapped[str] = mapped_column(String, nullable=True)
    tags: Mapped[str] = mapped_column(Text, nullable=True)  # JSON array stored as text
    status: Mapped[str] = mapped_column(String, default="draft")  # draft, published
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


# --- Video pipeline (text-generation steps only: titles + scripts via Gemini; voice/avatar
# generation is out of scope until an avatar service is chosen, see docs/03_Video_Pipeline.md) ---


class VideoTopic(Base):
    __tablename__ = "video_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic_candidates.id"), nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    suggestion_score: Mapped[float] = mapped_column(Float, nullable=True)
    format: Mapped[str] = mapped_column(String, nullable=True)  # long, short


class Script(Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_topic_id: Mapped[int] = mapped_column(ForeignKey("video_topics.id"), nullable=True)
    variant: Mapped[str] = mapped_column(String, nullable=True)  # long, short
    body: Mapped[str] = mapped_column(Text, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)


class VideoAsset(Base):
    __tablename__ = "video_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=True)
    voice_audio_path: Mapped[str] = mapped_column(String, nullable=True)
    avatar_clip_path: Mapped[str] = mapped_column(String, nullable=True)
    composed_video_path: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")


class VideoGeneration(Base):
    """One prompt-to-video job (Veo / Higgsfield). The provider runs it asynchronously; the
    provider_* columns are what let a restarted server resume polling."""

    __tablename__ = "video_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    original_idea: Mapped[str] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # veo, higgsfield
    model: Mapped[str] = mapped_column(String, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="queued")  # queued, running, succeeded, failed
    error: Mapped[str] = mapped_column(Text, nullable=True)
    research_sources: Mapped[str] = mapped_column(Text, nullable=True)  # JSON array of {title, url}
    provider_job_id: Mapped[str] = mapped_column(String, nullable=True)  # Veo operation name / Higgsfield request_id
    provider_status_url: Mapped[str] = mapped_column(String, nullable=True)
    local_path: Mapped[str] = mapped_column(String, nullable=True)  # file name inside storage/videos/, e.g. 12.mp4
    video_url: Mapped[str] = mapped_column(String, nullable=True)  # public hosted URL (Cloudinary), if configured
    instagram_media_id: Mapped[str] = mapped_column(String, nullable=True)
    instagram_permalink: Mapped[str] = mapped_column(String, nullable=True)
    instagram_status: Mapped[str] = mapped_column(String, nullable=True)  # publishing, published, failed
    instagram_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class VideoProject(Base):
    """One multi-stage Video Studio job (motion / faceless / skill / toons / shorts / explainer3d engines, see
    docs/10_Video_Studio_Engines.md). params/plan/assets are JSON stored as text; `assets` also
    carries the runner's resume bookkeeping (completed stages)."""

    __tablename__ = "video_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engine: Mapped[str] = mapped_column(String, nullable=False)  # motion, faceless, skill, toons, shorts, explainer3d
    recipe: Mapped[str] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    params: Mapped[str] = mapped_column(Text, nullable=True)  # JSON
    plan: Mapped[str] = mapped_column(Text, nullable=True)  # JSON
    assets: Mapped[str] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(String, default="queued")  # queued, planning, awaiting_approval, rendering, succeeded, failed
    stage: Mapped[str] = mapped_column(String, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    local_path: Mapped[str] = mapped_column(String, nullable=True)  # file name inside storage/projects/{id}/
    video_url: Mapped[str] = mapped_column(String, nullable=True)  # public hosted URL (Cloudinary), if configured
    instagram_media_id: Mapped[str] = mapped_column(String, nullable=True)
    instagram_permalink: Mapped[str] = mapped_column(String, nullable=True)
    instagram_status: Mapped[str] = mapped_column(String, nullable=True)  # publishing, published, failed
    instagram_error: Mapped[str] = mapped_column(Text, nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


# --- Posts / Carousel Studio ---


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    layout_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON


class PostDraft(Base):
    __tablename__ = "post_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String, default="manual")  # match_scrape, manual
    suggested_opinion_text: Mapped[str] = mapped_column(Text, nullable=True)
    image_source_url: Mapped[str] = mapped_column(String, nullable=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"), nullable=True)
    final_text: Mapped[str] = mapped_column(Text, nullable=True)
    final_image_config: Mapped[str] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(String, default="draft")  # draft, finalized, published
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CustomFont(Base):
    """A font the user uploaded in the Posts editor (TTF / OTF / WOFF / WOFF2). The bytes live in the row, not on
    disk, so they survive hosts with an ephemeral filesystem and follow the user to other devices."""

    __tablename__ = "custom_fonts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family: Mapped[str] = mapped_column(String, nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False)  # ttf | otf | woff | woff2
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MatchNote(Base):
    """A Notes sheet: one match or session, a title plus an ordered list of bullet opinions (Posts editor and the
    mobile Notes page). `bullets` is a JSON array of {id, text}. `client_id` is generated by the browser, so a sheet
    created offline can be pushed later (and retried) without ever being created twice."""

    __tablename__ = "match_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    workspace: Mapped[str] = mapped_column(String, default="madridonomy", index=True)  # madridonomy | footonomy | general
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    bullets: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PhoneHandoff(Base):
    """"Send to phone": the hosted slide URLs of a carousel plus its caption, opened on the phone through
    /send/<id>. The id is an unguessable token (it is the only credential the public read needs); rows expire."""

    __tablename__ = "phone_handoffs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    slide_urls: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array of https URLs, in slide order
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


# --- Dashboard / Analytics ---


class InstagramMetricSnapshot(Base):
    __tablename__ = "instagram_metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    followers: Mapped[int] = mapped_column(Integer, nullable=True)
    reach_30d: Mapped[int] = mapped_column(Integer, nullable=True)
    engagement_rate: Mapped[float] = mapped_column(Float, nullable=True)
    top_post_ids: Mapped[str] = mapped_column(Text, nullable=True)  # JSON array


class TwitterMetricSnapshot(Base):
    __tablename__ = "twitter_metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    followers: Mapped[int] = mapped_column(Integer, nullable=True)
    impressions_30d: Mapped[int] = mapped_column(Integer, nullable=True)
    engagement_rate: Mapped[float] = mapped_column(Float, nullable=True)


class TwitterPostSuggestion(Base):
    __tablename__ = "twitter_post_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic_candidates.id"), nullable=True)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="suggested")  # suggested, posted, discarded


class KpiBaseline(Base):
    """Manual pre-Studio baseline entry — 05_Dashboard_Analytics.md flags that
    the 'before' window can't be backfilled automatically, so this is a single
    fixed reference point the user records once, not an event-logged series.
    Not part of the original Data Dictionary; added to satisfy that doc's own
    explicit open item ("needs a manual baseline entry")."""

    __tablename__ = "kpi_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "Trailing 30 days pre-Studio"
    posts_per_week: Mapped[float] = mapped_column(Float, nullable=False)
    avg_engagement_rate: Mapped[float] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSecret(Base):
    """Key/value store for credentials the app rotates itself (e.g. the Instagram
    long-lived token). Render's env vars are read-only from inside the app, so a
    refreshed token has to live in the database. Not part of the Data Dictionary."""

    __tablename__ = "app_secrets"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CostEvent(Base):
    """Ledger of paid actions (docs/11_Cost_Awareness.md). `actual_usd` is only set when measurable
    (Gemini usage_metadata tokens x price, Veo seconds x price); otherwise the estimate range is all we know."""

    __tablename__ = "cost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ref_type: Mapped[str] = mapped_column(String, nullable=True)
    ref_id: Mapped[int] = mapped_column(Integer, nullable=True)
    estimated_low_usd: Mapped[float] = mapped_column(Float, nullable=True)
    estimated_high_usd: Mapped[float] = mapped_column(Float, nullable=True)
    actual_usd: Mapped[float] = mapped_column(Float, nullable=True)
    details: Mapped[str] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FootonomySnapshot(Base):
    """Point-in-time copy of Footonomy's headline numbers. Footonomy keeps no history of its own
    operations metrics, so Studio records one every few minutes while the Footonomy tab is in use."""

    __tablename__ = "footonomy_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    total_users: Mapped[int] = mapped_column(Integer, nullable=True)
    new_users_7d: Mapped[int] = mapped_column(Integer, nullable=True)
    follows_total: Mapped[int] = mapped_column(Integer, nullable=True)
    reactions_total: Mapped[int] = mapped_column(Integer, nullable=True)
    requests_total: Mapped[int] = mapped_column(Integer, nullable=True)
    errors_5xx_total: Mapped[int] = mapped_column(Integer, nullable=True)
    p95_seconds: Mapped[float] = mapped_column(Float, nullable=True)
    cache_hit_ratio: Mapped[float] = mapped_column(Float, nullable=True)
    websocket_sessions: Mapped[int] = mapped_column(Integer, nullable=True)


class GenjutsuJob(Base):
    """One Higgsfield Genjutsu motion-transfer job: an uploaded video + 1-8 reference photos -> the same motion with
    the photographed people. Inputs live in storage/genjutsu/<id>/; provider_* lets a restarted server resume polling."""

    __tablename__ = "genjutsu_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=True)
    resolution: Mapped[str] = mapped_column(String, nullable=False)  # 480p, 720p, 1080p
    status: Mapped[str] = mapped_column(String, default="queued")  # queued, running, succeeded, failed, moderated, canceled
    error: Mapped[str] = mapped_column(Text, nullable=True)
    request_id: Mapped[str] = mapped_column(String, nullable=True)  # Higgsfield request id
    video_name: Mapped[str] = mapped_column(String, nullable=True)  # original upload name, for display
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    input_seconds: Mapped[float] = mapped_column(Float, nullable=True)  # source video length
    billed_seconds: Mapped[int] = mapped_column(Integer, nullable=True)  # ceil(input), max 30
    est_cost_usd: Mapped[float] = mapped_column(Float, nullable=True)
    local_path: Mapped[str] = mapped_column(String, nullable=True)  # output.mp4 inside the job folder
    video_url: Mapped[str] = mapped_column(String, nullable=True)  # public hosted copy (Cloudinary), if configured
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # last time ANY server polled this job
    model: Mapped[str] = mapped_column(String, default="genjutsu")  # genjutsu | light

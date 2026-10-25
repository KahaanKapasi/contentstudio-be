import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _parse_json_text(v: Any, default: Any):
    if v is None:
        return default
    if isinstance(v, str):
        if not v:
            return default
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return default
        return default if parsed is None else parsed
    return v


class TopicCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    rationale: str | None
    suitable_for: str
    status: str
    created_at: datetime
    workspace: str = "footonomy"


class TopicStatusUpdate(BaseModel):
    status: str


class ScrapedItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    source_url: str | None
    raw_text: str | None
    scraped_at: datetime


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: int | None
    title: str
    body: str | None
    meta_description: str | None
    slug: str | None
    tags: list[str] = []
    status: str
    created_at: datetime
    published_at: datetime | None

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, v):
        return _parse_json_text(v, [])


class ArticleGenerateRequest(BaseModel):
    topic_id: int


class ArticleUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    meta_description: str | None = None
    slug: str | None = None
    tags: list[str] | None = None
    status: str | None = None


class VideoTopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: int | None
    title: str
    suggestion_score: float | None
    format: str | None


class VideoTitlesGenerateRequest(BaseModel):
    topic_id: int


class ScriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_topic_id: int | None
    variant: str | None
    body: str | None
    selected: bool


class ScriptGenerateRequest(BaseModel):
    video_topic_id: int


class ScriptUpdate(BaseModel):
    selected: bool


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    layout_config: dict = {}

    @field_validator("layout_config", mode="before")
    @classmethod
    def _parse_layout(cls, v):
        return _parse_json_text(v, {})


class PostDraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    suggested_opinion_text: str | None
    image_source_url: str | None
    template_id: int | None
    final_text: str | None
    final_image_config: dict | None = None
    status: str
    created_at: datetime

    @field_validator("final_image_config", mode="before")
    @classmethod
    def _parse_image_config(cls, v):
        return _parse_json_text(v, None)


class PostDraftCreate(BaseModel):
    source: str = "manual"
    suggested_opinion_text: str | None = None
    image_source_url: str | None = None
    template_id: int | None = None
    final_text: str | None = None


class PostDraftUpdate(BaseModel):
    final_text: str | None = None
    template_id: int | None = None
    final_image_config: dict | None = None
    status: str | None = None


class InstagramMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    captured_at: datetime
    followers: int | None
    reach_30d: int | None
    engagement_rate: float | None
    top_post_ids: list[int] = []
    warning: str | None = None  # set on refresh when insights (reach/engagement) couldn't be fetched

    @field_validator("top_post_ids", mode="before")
    @classmethod
    def _parse_top_posts(cls, v):
        return _parse_json_text(v, [])


class TwitterMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    captured_at: datetime
    followers: int | None
    impressions_30d: int | None
    engagement_rate: float | None


class TwitterSuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: int | None
    draft_text: str
    status: str


class KpiBaselineIn(BaseModel):
    label: str
    posts_per_week: float
    avg_engagement_rate: float | None = None


class KpiBaselineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    posts_per_week: float
    avg_engagement_rate: float | None
    created_at: datetime


# --- Video generation (prompt in -> mp4 out) ---


class SourceLink(BaseModel):
    title: str
    url: str


class ModelInfoOut(BaseModel):
    id: str
    label: str
    aspect_ratios: list[str]
    durations: list[int]
    resolutions: list[str]
    price_per_second_usd: dict[str, float] | None
    notes: str | None


class ProviderInfoOut(BaseModel):
    id: str
    label: str
    configured: bool
    missing_keys: list[str]
    default_model: str
    models: list[ModelInfoOut]


class PromptImproveRequest(BaseModel):
    idea: str
    research: bool = False
    aspect_ratio: str = "16:9"
    duration_seconds: int = 8


class PromptImproveOut(BaseModel):
    prompt: str
    sources: list[SourceLink]
    research_notes: str | None


class VideoGenerationCreate(BaseModel):
    prompt: str
    original_idea: str | None = None
    provider: str
    model: str
    aspect_ratio: str
    duration_seconds: int
    resolution: str
    research_sources: list[SourceLink] = []


class VideoGenerationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prompt: str
    original_idea: str | None
    provider: str
    model: str
    aspect_ratio: str
    duration_seconds: int
    resolution: str
    status: str
    error: str | None
    has_file: bool
    video_url: str | None
    research_sources: list[SourceLink] = []
    estimated_cost_usd: float | None
    instagram_media_id: str | None = None
    instagram_permalink: str | None = None
    instagram_status: str | None = None  # publishing, published, failed
    instagram_error: str | None = None
    instagram_warnings: list[str] = []  # soft warnings, only filled on the publish response
    created_at: datetime
    completed_at: datetime | None

    @field_validator("research_sources", mode="before")
    @classmethod
    def _parse_sources(cls, v):
        return _parse_json_text(v, [])

    @field_validator("created_at", "completed_at")
    @classmethod
    def _as_utc(cls, v: datetime | None):
        # SQLite returns naive UTC; make the ISO string say so, or browsers read it as local time.
        return v.replace(tzinfo=timezone.utc) if v is not None and v.tzinfo is None else v


# --- Video Studio engines (docs/10_Video_Studio_Engines.md) ---


class StudioScene(BaseModel):
    index: int
    text: str
    visual: str = ""
    duration_s: float = 0.0


class StudioPlanOut(BaseModel):
    summary: str = ""
    script: str | None = None
    scenes: list[StudioScene] | None = None
    notes: str | None = None


class StudioPreviewOut(BaseModel):
    name: str
    label: str
    kind: str  # image, audio, video


class StudioProjectOut(BaseModel):
    id: int
    engine: str
    recipe: str | None
    title: str
    status: str
    stage: str | None
    progress: int
    error: str | None
    params: dict[str, Any]
    plan: StudioPlanOut | None
    previews: list[StudioPreviewOut]
    has_file: bool
    video_url: str | None
    estimated_cost_usd: float | None
    estimated_cost_low_usd: float | None = None
    estimated_cost_high_usd: float | None = None
    instagram_media_id: str | None = None
    instagram_permalink: str | None = None
    instagram_status: str | None = None  # publishing, published, failed
    instagram_error: str | None = None
    instagram_warnings: list[str] = []  # soft warnings, only filled on the publish response
    created_at: datetime
    completed_at: datetime | None

    @field_validator("created_at", "completed_at")
    @classmethod
    def _as_utc(cls, v: datetime | None):
        return v.replace(tzinfo=timezone.utc) if v is not None and v.tzinfo is None else v



class InstagramPublishRequest(BaseModel):
    caption: str = ""
    share_to_feed: bool = True


# --- Notes (Posts editor + mobile Notes page) ---------------------------------------------------------------------

NOTE_MAX_BULLETS = 300
NOTE_MAX_TEXT = 2000


def _as_utc(dt: datetime) -> datetime:
    """SQLite hands datetimes back naive; they are all UTC, so say so (browsers read a bare ISO string as local time)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class NoteBullet(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=NOTE_MAX_TEXT)


class NoteCreate(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    workspace: str = "madridonomy"
    title: str = Field(default="", max_length=200)
    bullets: list[NoteBullet] = Field(default_factory=list, max_length=NOTE_MAX_BULLETS)


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    bullets: list[NoteBullet] | None = Field(default=None, max_length=NOTE_MAX_BULLETS)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: str
    workspace: str
    title: str
    bullets: list[NoteBullet] = []
    created_at: datetime
    updated_at: datetime

    @field_validator("bullets", mode="before")
    @classmethod
    def _parse_bullets(cls, v):
        return _parse_json_text(v, [])

    @field_serializer("created_at", "updated_at")
    def _utc(self, v: datetime) -> str:
        return _as_utc(v).isoformat()


# --- Send to phone ------------------------------------------------------------------------------------------------

HANDOFF_MAX_SLIDES = 40


class HandoffCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    slide_urls: list[str] = Field(min_length=1, max_length=HANDOFF_MAX_SLIDES)
    caption: str = Field(default="", max_length=5000)

    @field_validator("slide_urls")
    @classmethod
    def _https_only(cls, urls: list[str]) -> list[str]:
        for u in urls:
            if not u.startswith("https://") or len(u) > 2048:
                raise ValueError("Slide URLs must be https:// links (the hosted images).")
        return urls


class HandoffOut(BaseModel):
    id: str
    title: str
    slide_urls: list[str]
    caption: str
    created_at: datetime
    expires_at: datetime

    @field_serializer("created_at", "expires_at")
    def _utc(self, v: datetime) -> str:
        return _as_utc(v).isoformat()

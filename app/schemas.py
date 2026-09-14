import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _parse_json_text(v: Any, default: Any):
    if isinstance(v, str):
        if not v:
            return default
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return default
    return v


class TopicCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    rationale: str | None
    suitable_for: str
    status: str
    created_at: datetime


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

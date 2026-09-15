from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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


class TopicCandidate(Base):
    __tablename__ = "topic_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)
    suitable_for: Mapped[str] = mapped_column(String, nullable=False)  # article, video, both
    status: Mapped[str] = mapped_column(String, default="new")  # new, selected, discarded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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

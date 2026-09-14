import json
import re

from sqlalchemy.orm import Session

from app.models import Article, ScrapedItem, TopicCandidate, TopicSourceLink
from app.services.gemini_client import generate_json, generate_text

# Style guide is an open item in 02_Article_Pipeline.md ("few-shot voice examples
# not yet compiled") — this is a minimal placeholder tone instruction; replace
# with real few-shot examples from past Madridonomy posts once compiled.
STYLE_GUIDE = (
    "Write in a confident, knowledgeable football-fan voice — direct opinions, "
    "no hedging filler, short punchy paragraphs, occasional rhetorical question. "
    "Avoid generic AI-article tells: no 'in conclusion', no 'furthermore', no "
    "listicle-style summarizing at the end unless the topic calls for it."
)


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:80]


def _source_context(db: Session, topic: TopicCandidate) -> str:
    links = db.query(TopicSourceLink).filter(TopicSourceLink.topic_id == topic.id).all()
    item_ids = [link.scraped_item_id for link in links]
    if not item_ids:
        return ""
    items = db.query(ScrapedItem).filter(ScrapedItem.id.in_(item_ids)).all()
    return "\n".join(f"- {item.raw_text[:300]}" for item in items)


def generate_article(db: Session, topic_id: int) -> Article:
    topic = db.query(TopicCandidate).filter(TopicCandidate.id == topic_id).first()
    if not topic:
        raise ValueError(f"Topic {topic_id} not found")

    context = _source_context(db, topic)

    draft_prompt = f"""Write a complete, publish-ready football article on this topic:

Title/angle: {topic.title}
Rationale: {topic.rationale}

Supporting context from scraped sources:
{context or "(no additional source context available)"}

Write the full article body only (no title line, no meta text). Aim for 500-800 words."""
    body = generate_text(draft_prompt)

    humanize_prompt = f"""Rewrite the following football article to sound more human and \
less like generic AI writing, following this style guide:

{STYLE_GUIDE}

Vary sentence structure, remove repetitive phrasing patterns, keep the factual content \
and length roughly the same. Return only the rewritten body text.

Original:
{body}"""
    humanized_body = generate_text(humanize_prompt)

    seo_prompt = f"""Given this football article, generate SEO metadata as JSON with exactly \
these keys: title (a punchy SEO title tag, under 60 chars), meta_description (under 155 chars), \
tags (a list of 5-8 relevant keyword tags).

Article:
{humanized_body[:3000]}"""
    seo = generate_json(seo_prompt)

    article = Article(
        topic_id=topic.id,
        title=seo.get("title") or topic.title,
        body=humanized_body,
        meta_description=seo.get("meta_description", ""),
        slug=_slugify(seo.get("title") or topic.title),
        tags=json.dumps(seo.get("tags", [])),
        status="draft",
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return article

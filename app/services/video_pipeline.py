from sqlalchemy.orm import Session

from app.models import Script, TopicCandidate, VideoTopic
from app.services.gemini_client import generate_json


def generate_video_titles(db: Session, topic_id: int) -> list[VideoTopic]:
    topic = db.query(TopicCandidate).filter(TopicCandidate.id == topic_id).first()
    if not topic:
        raise ValueError(f"Topic {topic_id} not found")
    if topic.suitable_for not in ("video", "both"):
        raise ValueError(f"Topic {topic_id} is not marked suitable for video")

    prompt = f"""You are generating candidate VIDEO titles for a football content creator, \
based on this topic (which was originally identified for general content):

Topic: {topic.title}
Rationale: {topic.rationale}

Generate 4-6 distinct candidate video titles. These must read as VIDEO titles, not article \
headlines — more hook-driven, punchier, built for retention in the first few seconds. Some \
should be framed for short-form vertical video (Shorts/Reels — punchy, curiosity-gap, under \
~8 words) and some for long-form video (YouTube-style, can be more descriptive).

Return a JSON array. Each item must have exactly these keys:
- "title": the candidate video title (string)
- "format": either "long" or "short"
- "suggestion_score": a float between 0 and 1 representing your own best guess at how well \
this title/angle would engage this creator's football audience. There is no real performance \
history to base this on yet, so treat this purely as a self-judged heuristic estimate, not a \
measured figure."""

    # NOTE: suggestion_score here is Gemini self-judging likely engagement — a placeholder
    # heuristic. Per docs/03_Video_Pipeline.md's own open item ("Suggestion-scoring heuristic
    # — currently undefined beyond 'some signal', needs an actual method once Dashboard data
    # exists"), this should be replaced with a real method once Dashboard performance data
    # (docs/05_Dashboard_Analytics.md) is available to ground it in actual post performance.
    candidates = generate_json(prompt)
    if not isinstance(candidates, list):
        candidates = []

    video_topics: list[VideoTopic] = []
    for candidate in candidates:
        title = candidate.get("title")
        if not title:
            continue
        fmt = candidate.get("format") if candidate.get("format") in ("long", "short") else "long"
        score = candidate.get("suggestion_score")
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None

        video_topic = VideoTopic(
            topic_id=topic.id,
            title=title,
            suggestion_score=score,
            format=fmt,
        )
        db.add(video_topic)
        video_topics.append(video_topic)

    db.commit()
    for vt in video_topics:
        db.refresh(vt)
    return video_topics


def generate_scripts(db: Session, video_topic_id: int) -> list[Script]:
    video_topic = db.query(VideoTopic).filter(VideoTopic.id == video_topic_id).first()
    if not video_topic:
        raise ValueError(f"Video topic {video_topic_id} not found")

    prompt = f"""Write two scripts for a football content video with this title:

Title: {video_topic.title}

Return a JSON object with exactly two keys, "long" and "short", each a string containing the \
full spoken script (no scene directions, no timestamps, just the words to be spoken):
- "long": a long-form YouTube-style script, roughly 400-700 words, can develop the argument \
with more detail and examples.
- "short": a short-form Shorts/Reels script, roughly 60-120 words, fast hook in the first \
line, punchy delivery, single clear point."""

    result = generate_json(prompt)
    if not isinstance(result, dict):
        result = {}

    long_body = result.get("long") or ""
    short_body = result.get("short") or ""

    long_script = Script(
        video_topic_id=video_topic.id,
        variant="long",
        body=long_body,
        selected=False,
    )
    short_script = Script(
        video_topic_id=video_topic.id,
        variant="short",
        body=short_body,
        selected=False,
    )
    db.add(long_script)
    db.add(short_script)
    db.commit()
    db.refresh(long_script)
    db.refresh(short_script)
    return [long_script, short_script]

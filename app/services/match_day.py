"""Match-day opinion suggestions for the Posts Studio — distinct from the
general Discovery Engine (01_Content_Discovery_Scraping.md): event-triggered
by a match in progress rather than scheduled trend-scanning, and narrower
(real-time match commentary/reactions rather than broad topic discovery).
"""

from app.services.gemini_client import generate_json
from app.services.scraping.twitter import fetch_topic

OPINION_PROMPT_TEMPLATE = """You are a football content creator's assistant. Below are \
real-time tweets/reactions about an ongoing or just-finished match involving {team}. \
Generate 5-8 short, opinionated post angles (3-5 lines each, ready to drop into an \
Instagram carousel post) that a passionate fan account would post right now.

Respond as a JSON array of strings, each a ready-to-use opinion post text.

Tweets:
{tweets_block}
"""


def suggest_opinions(team: str = "Real Madrid") -> list[str]:
    result = fetch_topic(query=f'"{team}" -is:retweet lang:en', limit=40)
    if not result.ok or not result.items:
        return []

    tweets_block = "\n".join(f"- {item.raw_text}" for item in result.items)
    prompt = OPINION_PROMPT_TEMPLATE.format(team=team, tweets_block=tweets_block)
    return generate_json(prompt)

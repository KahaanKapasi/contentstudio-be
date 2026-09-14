"""X/Twitter account metrics + posting, for the Dashboard
(distinct from services/scraping/twitter.py, which is the Discovery-Engine
trend-search adapter). API tier/cost for combined analytics + posting access
is an explicitly open item in 05_Dashboard_Analytics.md.
"""

import tweepy

from app.config import settings


class TwitterNotConfigured(RuntimeError):
    pass


def _require_bearer():
    if not settings.x_bearer_token:
        raise TwitterNotConfigured("X_BEARER_TOKEN not set in .env")


def _require_oauth1():
    if not all(
        [settings.x_api_key, settings.x_api_secret, settings.x_access_token, settings.x_access_token_secret]
    ):
        raise TwitterNotConfigured("X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET not fully set in .env")


def get_account_metrics(username: str) -> dict:
    _require_bearer()
    client = tweepy.Client(bearer_token=settings.x_bearer_token)
    user = client.get_user(username=username, user_fields=["public_metrics"])
    return user.data.public_metrics if user.data else {}


def post_tweet(text: str) -> str:
    _require_oauth1()
    client = tweepy.Client(
        consumer_key=settings.x_api_key,
        consumer_secret=settings.x_api_secret,
        access_token=settings.x_access_token,
        access_token_secret=settings.x_access_token_secret,
    )
    response = client.create_tweet(text=text)
    return response.data["id"]

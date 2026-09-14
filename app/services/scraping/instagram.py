"""Instagram source adapter — stub.

01_Content_Discovery_Scraping.md notes Instagram has "no practical public API
for this use case" and scraping is "highest fragility — Instagram actively
changes markup/blocks scraping." Building real scraping logic blind (without a
live, logged-in session to test against) would likely break on first use.

This adapter is wired into the same SourceResult interface as the other
sources so the Discovery pipeline treats it uniformly, but it always reports
"not configured" until real scraping logic (Playwright + an authenticated
session) is added here.
"""

from app.services.scraping.base import SourceResult


def fetch_topic(query: str = "") -> SourceResult:
    return SourceResult(
        source_name="instagram",
        items=[],
        ok=False,
        error="Instagram scraping not implemented — needs Playwright + authenticated session (see module docstring)",
    )

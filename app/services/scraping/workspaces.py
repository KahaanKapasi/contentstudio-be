"""Discovery workspaces: which sources and which topic focus a scrape uses.

  madridonomy  Real Madrid only (sources_madridonomy.py)
  footonomy    football-wide    (rss.DEFAULT_FEEDS + sources_footonomy.py)
  general      ungated: the union of both, shown/scraped without a focus filter

Everything downstream (articles, video, studio, posts) starts from a Discovery topic, so the workspace is chosen
once, in Discovery. Source data lives in the two sources_*.py modules; this file only assembles them.
"""

from dataclasses import dataclass

from app.services.scraping import rss, sources_footonomy, sources_madridonomy

DEFAULT_WORKSPACE = "footonomy"  # what existing rows and callers that pass nothing belong to
SCRAPED_WORKSPACES = ("madridonomy", "footonomy")  # what the background scheduler scrapes
WORKSPACE_IDS = ("madridonomy", "footonomy", "general")

GENERAL_FOCUS = (
    "a general football content creator. Pick the strongest football stories of the moment, from any club or league, "
    "with no brand restriction."
)


@dataclass(frozen=True)
class Workspace:
    id: str
    label: str
    description: str
    feeds: tuple[tuple[str, str], ...]
    x_query: str
    topic_focus: str


def _dedupe(feeds) -> tuple[tuple[str, str], ...]:
    seen_names, seen_urls, out = set(), set(), []
    for name, url in feeds:
        if name in seen_names or url in seen_urls:
            continue
        seen_names.add(name)
        seen_urls.add(url)
        out.append((name, url))
    return tuple(out)


def get(workspace_id: str | None) -> Workspace:
    ws = workspace_id or DEFAULT_WORKSPACE
    if ws == "madridonomy":
        m = sources_madridonomy
        return Workspace(ws, m.LABEL, m.DESCRIPTION, _dedupe(m.FEEDS), m.X_QUERY, m.TOPIC_FOCUS)
    f = sources_footonomy
    football_feeds = _dedupe((*rss.DEFAULT_FEEDS, *f.EXTRA_FEEDS))
    if ws == "footonomy":
        return Workspace(ws, f.LABEL, f.DESCRIPTION, football_feeds, f.X_QUERY, f.TOPIC_FOCUS)
    if ws == "general":
        feeds = _dedupe((*football_feeds, *sources_madridonomy.FEEDS))
        return Workspace(ws, "General", "Everything: Footonomy and Madridonomy sources together, no focus filter.", feeds, f.X_QUERY, GENERAL_FOCUS)
    raise ValueError(f"Unknown workspace '{workspace_id}'. Use one of: {', '.join(WORKSPACE_IDS)}.")


def describe() -> list[dict]:
    return [
        {"id": w.id, "label": w.label, "description": w.description, "feed_count": len(w.feeds), "x_query": w.x_query}
        for w in (get(i) for i in WORKSPACE_IDS)
    ]

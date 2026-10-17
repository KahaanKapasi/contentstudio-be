"""Live-check a Discovery workspace's sources (network!). Usage: python scripts/check_sources.py madridonomy|footonomy|general

Prints one line per RSS feed (OK / EMPTY / FAIL, entry count, newest title) and validates the X query. Exit code 1 if
any feed returns nothing, so an agent can iterate until every source is live. Never calls the X API (that costs money).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scraping import rss, workspaces  # noqa: E402


def main(workspace_id: str) -> int:
    ws = workspaces.get(workspace_id)
    print(f"{ws.label}: {len(ws.feeds)} feeds")
    bad = 0
    for name, url in ws.feeds:
        result = rss.fetch_feed(name, url, limit=5)
        n = len(result.items)
        if result.ok and n:
            title = (result.items[0].raw_text or "").split("\n")[0][:70]
            print(f"  OK    {name:28} {n:2} entries  newest: {title}")
        else:
            bad += 1
            print(f"  {'EMPTY' if result.ok else 'FAIL '} {name:28} {url}  {result.error or ''}"[:200])
    print(f"X query ({len(ws.x_query)} chars): {ws.x_query}")
    if len(ws.x_query) > 480:
        print("  X query is too long (limit 480)")
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "footonomy"))

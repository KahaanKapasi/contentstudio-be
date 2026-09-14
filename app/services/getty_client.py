"""Getty Images sourcing — internal-use accepted risk (per docs/CLAUDE.md:
"Getty image downloading is accepted as an internal-use risk").

FINDING (verified 2026-09-14): Getty Images runs bot-detection ("User
validation — please click below to validate that you are a real user") that
blocks a plain headless Playwright session outright — the search page never
even renders results, just the validation challenge. Automating past that
challenge is bot-detection evasion, which is off-limits regardless of this
being an internal/personal-use tool.

Given that, this module does NOT attempt automated search/scraping. The
practical path for Getty images stays manual: browse gettyimages.com
yourself, download the image, then upload it through the Posts screen's
normal image-upload input like any other source image. `search_preview_images`
below reflects this — it always raises GettyBlocked rather than pretending to
work, so the failure is visible immediately instead of silently returning
empty results.
"""


class GettyBlocked(RuntimeError):
    pass


class GettyNotConfigured(RuntimeError):
    pass


async def search_preview_images(query: str, limit: int = 12) -> list[dict]:
    raise GettyBlocked(
        "Getty Images blocks automated/headless access with a bot-detection challenge — "
        "verified directly against the live site. Download images manually from "
        "gettyimages.com and upload them via the normal image picker instead."
    )

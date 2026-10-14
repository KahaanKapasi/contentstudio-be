"""Smoke test: Higgsfield Seedance 2.5 text-to-video via the official SDK (higgsfield-client).

Run (BILLABLE - one 5 s 720p generation):
    cd backend && source venv/bin/activate && python examples/higgsfield_seedance/main.py

Credentials: put  HF_KEY=<key-id>:<key-secret>  in examples/higgsfield_seedance/.env.local (git-ignored).
The key is only ever loaded into this process's environment; it is never printed or logged.
"""

import os
import sys
import time
from pathlib import Path

import higgsfield_client
from dotenv import load_dotenv
from higgsfield_client import Cancelled, Completed, Failed, InProgress, NSFW, Queued
from higgsfield_client.exceptions import CredentialsMissedError

HERE = Path(__file__).resolve().parent
MODEL = "bytedance/seedance-2.5/text-to-video"
ARGUMENTS = {
    "prompt": "A cinematic scene at sunset",
    "duration": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
}
MAX_WAIT_S = 20 * 60  # the SDK polls forever; give up (and say so) rather than hang


def credentials_ok() -> bool:
    """True when HF_KEY looks like 'key-id:key-secret'. Never echoes the value."""
    key = os.environ.get("HF_KEY", "")
    key_id, _, secret = key.partition(":")
    return bool(key_id.strip() and secret.strip())


def video_url_from(result) -> str | None:
    """Seedance returns the file in the `video` field (object with `url`, or a bare URL string)."""
    video = result.get("video") if isinstance(result, dict) else None
    if isinstance(video, dict):
        video = video.get("url")
    return video if isinstance(video, str) and video.startswith("http") else None


def main() -> int:
    load_dotenv(HERE / ".env.local")
    if not credentials_ok():
        print(f"HF_KEY is missing or not in key-id:key-secret format.\nEnter it locally in {HERE / '.env.local'} (never in chat), then re-run.", file=sys.stderr)
        return 2

    started = time.monotonic()
    state: dict = {"request_id": None, "status": None}

    def on_enqueue(request_id: str) -> None:
        state["request_id"] = request_id
        print(f"Queued request {request_id}")

    def on_queue_update(status) -> None:
        state["status"] = status
        if isinstance(status, Queued):
            print("  status: queued")
        elif isinstance(status, InProgress):
            print("  status: in progress")
        if time.monotonic() - started > MAX_WAIT_S:
            raise TimeoutError(f"Gave up after {MAX_WAIT_S // 60} minutes; request {state['request_id']} may still be running (and billed).")

    print(f"Submitting {MODEL} {ARGUMENTS}")
    try:
        result = higgsfield_client.subscribe(MODEL, arguments=ARGUMENTS, on_enqueue=on_enqueue, on_queue_update=on_queue_update)
    except CredentialsMissedError as exc:
        print(f"Credentials rejected by SDK: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # network / HTTP errors, timeout above. Message never contains the key.
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # subscribe() does NOT raise for failed / canceled / moderated jobs: it returns whatever the API gave back.
    final = state["status"]
    if isinstance(final, NSFW):
        print("MODERATED: the request was flagged as NSFW by Higgsfield. No video was produced.", file=sys.stderr)
        return 3
    if isinstance(final, Failed):
        print(f"FAILED: Higgsfield reported the generation failed. Details: {result}", file=sys.stderr)
        return 1
    if isinstance(final, Cancelled):
        print("CANCELED: the request was canceled before it ran. No video was produced.", file=sys.stderr)
        return 1
    if not isinstance(final, Completed):
        print(f"UNKNOWN final status ({type(final).__name__}); not treating this as success. Result: {result}", file=sys.stderr)
        return 1

    url = video_url_from(result)
    if not url:
        print(f"Status was Completed but the result has no video URL. Result: {result}", file=sys.stderr)
        return 1

    print(f"SUCCESS in {time.monotonic() - started:.0f}s\nVideo URL: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

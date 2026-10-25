"""Send to phone: hand a finished carousel to the phone without Telegram.

The editor uploads each slide through /api/posts/upload-to-host, then creates a handoff here (password gate applies).
The phone opens /send/<id> in the web app, which reads the handoff through the PUBLIC route below.

Access choice: the read route is deliberately NOT behind the access password. The phone has never entered it, and the
link is meant to be opened by tapping a QR code. The id is a 128-bit random token (the capability), so it cannot be
guessed; it only exposes the hosted slide URLs and the caption of that one carousel (images that are about to be
posted publicly anyway); and the row stops resolving after 7 days. Creating and deleting handoffs stays gated.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PhoneHandoff, utcnow
from app.schemas import HandoffCreate, HandoffOut

HANDOFF_TTL = timedelta(days=7)

router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])  # behind require_access (see main.py)
public_router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])  # read by link


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _out(row: PhoneHandoff) -> HandoffOut:
    return HandoffOut(
        id=row.id,
        title=row.title,
        slide_urls=json.loads(row.slide_urls),
        caption=row.caption,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def purge_expired(db: Session) -> int:
    n = db.query(PhoneHandoff).filter(PhoneHandoff.expires_at < utcnow()).delete()
    db.commit()
    return n


@router.post("", response_model=HandoffOut)
def create_handoff(payload: HandoffCreate, db: Session = Depends(get_db)):
    purge_expired(db)
    now = utcnow()
    row = PhoneHandoff(
        id=secrets.token_urlsafe(16),  # 128 bits
        title=payload.title.strip(),
        slide_urls=json.dumps(payload.slide_urls),
        caption=payload.caption,
        created_at=now,
        expires_at=now + HANDOFF_TTL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.delete("/{handoff_id}")
def delete_handoff(handoff_id: str, db: Session = Depends(get_db)):
    row = db.get(PhoneHandoff, handoff_id)
    if not row:
        raise HTTPException(status_code=404, detail="Handoff not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@public_router.get("/{handoff_id}", response_model=HandoffOut)
def read_handoff(handoff_id: str, response: Response, db: Session = Depends(get_db)):
    row = db.get(PhoneHandoff, handoff_id) if 8 <= len(handoff_id) <= 64 else None
    if not row or _aware(row.expires_at) <= utcnow():
        raise HTTPException(status_code=404, detail="This link has expired or does not exist.")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return _out(row)

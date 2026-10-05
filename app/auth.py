import hmac

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import settings


def require_access(x_access_password: str | None = Header(default=None)) -> None:
    """Optional single-user password gate (CLAUDE.md: no multi-user auth, at most
    a simple local password gate). Disabled when LOCAL_ACCESS_PASSWORD is unset,
    so local dev and tests need no header."""
    expected = settings.local_access_password
    if not expected:
        return
    supplied = x_access_password or ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing access password")


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def auth_status():
    return {"required": bool(settings.local_access_password)}


@router.get("/check", dependencies=[Depends(require_access)])
def auth_check():
    return {"ok": True}

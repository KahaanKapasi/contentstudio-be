from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.costs import estimate as est
from app.services.costs import ledger, prices

router = APIRouter(prefix="/api/costs", tags=["costs"])


class EstimateRequest(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)


@router.post("/estimate")
def estimate_cost(payload: EstimateRequest):
    try:
        return est.estimate(payload.action, payload.params)
    except est.UnknownAction as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
def spend_summary(days: int = 30, db: Session = Depends(get_db)):
    return ledger.summary(db, days)


@router.get("/prices")
def price_table():
    return {"as_of": date.today().isoformat(), "prices": prices.table()}

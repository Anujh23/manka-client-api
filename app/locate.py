"""Public side of the Maps API link (/locate). No login: the secret token in the link is the permission.

GET  /locate/{token}  → who is asking and the link's status, for the page to show.
POST /locate/{token}  → the phone's GPS fix, accepted once while the link is pending.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from .db import DB, utcnow
from .models import Client, LocationRequest
from .products.maps import status

router = APIRouter(prefix="/locate")


class FixIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(gt=0, le=100_000)


def _request(db: DB, token: str) -> LocationRequest:
    r = db.scalar(select(LocationRequest).where(LocationRequest.token == token))
    if r is None:
        raise HTTPException(404, "This link isn't valid.")
    return r


@router.get("/{token}")
def show(token: str, db: DB) -> dict:
    r = _request(db, token)
    return {"status": status(r), "requester": db.get(Client, r.client_id).name}


@router.post("/{token}", status_code=204)
def submit(token: str, body: FixIn, request: Request, db: DB) -> None:
    r = _request(db, token)
    state = status(r)
    if state == "done":
        raise HTTPException(409, "Location already shared.")
    if state == "expired":
        raise HTTPException(410, "This link has expired.")
    # One atomic UPDATE: if two submits race, only the first one lands.
    saved = db.execute(
        update(LocationRequest)
        .where(LocationRequest.id == r.id, LocationRequest.located_at.is_(None))
        .values(
            lat=round(body.lat, 7), lng=round(body.lng, 7), accuracy_m=round(body.accuracy_m, 1), located_at=utcnow(),
            ip=request.client.host if request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        )
    ).rowcount
    db.commit()
    if not saved:
        raise HTTPException(409, "Location already shared.")

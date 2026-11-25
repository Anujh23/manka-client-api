"""Maps API: get someone's GPS coordinates with their consent.

POST /v1/maps/link {reference} makes a one-time link (valid LINK_HOURS). The client sends it to the person; they open
it (website /locate/?t=…, no branding) and tap Share location, which posts the phone's GPS fix to /locate/{token}
(app/locate.py). The client reads the result with GET /v1/maps/link/{id} (api.py). Uses only the phone's GPS: no map
tiles or geocoding services, so nothing third-party to license.
"""
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import Client, LocationRequest
from ..security import new_token
from . import Product

LINK_HOURS = 24


class LinkIn(BaseModel):
    reference: str = Field(min_length=1, max_length=64)


def link_url(token: str) -> str:
    return f"{get_settings().site_url.rstrip('/')}/locate/?t={token}"


def _ts(dt):
    return dt.isoformat(timespec="seconds") + "Z" if dt else None  # stored as naive UTC


def status(r: LocationRequest) -> str:
    if r.located_at:
        return "done"
    return "expired" if utcnow() > r.expires_at else "pending"


def view(r: LocationRequest) -> dict:
    """The request as clients see it (API and dashboard)."""
    return {
        "request": r.public_id,
        "reference": r.reference,
        "status": status(r),
        "url": link_url(r.token) if status(r) == "pending" else None,
        "expires_at": _ts(r.expires_at),
        "lat": r.lat,
        "lng": r.lng,
        "accuracy_m": r.accuracy_m,
        "located_at": _ts(r.located_at),
    }


def create(db: Session, body: LinkIn, client: Client) -> dict:
    r = LocationRequest(
        public_id=f"loc_{new_token()[:12]}", token=new_token(), client_id=client.id,
        reference=body.reference.strip(), expires_at=utcnow() + timedelta(hours=LINK_HOURS),
    )
    db.add(r)
    db.commit()
    return view(r)


PRODUCT = Product(
    name="maps", title="Maps API", method="POST", path="/maps/link", price_paise=100,
    input=LinkIn, run=create,
)

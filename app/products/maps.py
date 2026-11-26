"""Maps API: get someone's GPS coordinates with their consent.

POST /v1/maps/link {reference} makes a one-time link (valid LINK_HOURS). The client sends it to the person; they open
it (website /locate/?t=…, no branding) and tap Share location, which posts the phone's GPS fix to /locate/{token}
(app/locate.py). The client reads the result with GET /v1/maps/link/{id} (api.py). Once located, every request has
a map image URL (app/staticmap.py, OpenStreetMap), included in the price.

The Maps JavaScript API (app/static/maps.js, served at /v1/maps.js) bills one more product from a browser: maps_locate
(each MapsAPI.locate, stored like a shared link, reference "Browser"). MapsAPI.map is free: it doesn't call the API.
"""
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import staticmap
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


def image_url(public_id: str) -> str:
    """The map image of a located request (app/staticmap.py, served by api.py)."""
    return f"{get_settings().api_base}/v1/maps/image/{public_id}.png"


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
        "image": image_url(r.public_id) if r.located_at else None,
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
    name="maps", title="Maps API", method="POST", path="/maps/link", price_paise=500,
    input=LinkIn, run=create,
)


class FixIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(gt=0, le=100_000)


def browser_locate(db: Session, body: FixIn, client: Client) -> dict:
    now = utcnow()
    r = LocationRequest(
        public_id=f"loc_{new_token()[:12]}", token=new_token(), client_id=client.id, reference="Browser",
        expires_at=now, lat=round(body.lat, 7), lng=round(body.lng, 7), accuracy_m=round(body.accuracy_m, 1), located_at=now,
    )
    db.add(r)
    db.commit()
    staticmap.warm(r.lat, r.lng, r.accuracy_m)  # the page asks for the image next
    v = view(r)
    return {k: v[k] for k in ("request", "lat", "lng", "accuracy_m", "located_at", "image")}


LOCATE = Product(
    name="maps_locate", title="Browser location", method="POST", path="/maps/locate", price_paise=500,
    input=FixIn, run=browser_locate,
)

"""Map images: a PNG of the OpenStreetMap map around a located point, with a pin and the accuracy circle.

Like a static map API: clients get a plain image URL (GET /v1/maps/image/{request}.png, api.py) to put in an <img> or
draw onto a photo. Built here from OSM France tiles, a volunteer server: heavy use may get blocked, then move to a
paid tile host.

Fast: when a location is saved, warm() starts rendering its image in the background, so it's ready by the time the
page asks for it. All tiles of an image are fetched at once, tiles and finished images are cached in memory, and a
request for an image that's still rendering waits for that render instead of starting another. OSM's terms need an identifying User-Agent and the
"© OpenStreetMap" credit: by default next to the image on the client's page, as the docs require, or on the
image (credit=True).
"""
import io
import math
import threading
import urllib.request
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .config import get_settings

TILE_URL = "https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png"
TILE = 256
WIDTH, HEIGHT, ZOOM = 640, 400, 17
SCALE = 4  # the pin and circle are drawn 4x larger, then shrunk: smooth edges
BLUE = (37, 99, 235)
KEEP = 64  # finished images kept in memory, about 13 MB
_tiles = ThreadPoolExecutor(16, thread_name_prefix="tile")  # every tile of an image at once
_renders = ThreadPoolExecutor(4, thread_name_prefix="map")
_lock = threading.Lock()
_images: "OrderedDict[tuple, Future]" = OrderedDict()  # rendered or rendering, newest last
CREDIT = "© OpenStreetMap"  # licence condition (ODbL + tile server terms): must stay on the image, legible


@lru_cache(maxsize=1024)  # about 20 MB; failed fetches raise, so they aren't cached
def tile(z: int, x: int, y: int) -> bytes:
    url = TILE_URL.format(s="abc"[(x + y) % 3], z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": f"maps-image/1.0 (+{get_settings().site_url})"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def _fetch(xy: tuple[int, int]) -> bytes | None:
    try:
        return tile(ZOOM, xy[0] % 2**ZOOM, xy[1])
    except Exception:
        return None


def _pin(d: ImageDraw.ImageDraw, x: float, y: float, s: int) -> None:
    """Map pin with its tip at (x, y): white outline, blue body, white dot. Sizes are in final pixels times s."""
    c = y - 24 * s  # circle centre; the tip is 2 radii below it
    for r, color in ((14 * s, "white"), (12 * s, BLUE)):
        k = math.sqrt(3) / 2 * r  # where the sides touch the circle
        d.polygon([(x, c + 2 * r), (x - k, c + r / 2), (x + k, c + r / 2)], fill=color)
        d.ellipse((x - r, c - r, x + r, c + r), fill=color)
    r = 4.5 * s
    d.ellipse((x - r, c - r, x + r, c + r), fill="white")


def render(lat: float, lng: float, accuracy_m: float, credit: bool) -> tuple[bytes, bool]:
    """The PNG, and whether every tile loaded (if not, those parts are blank and the image shouldn't be cached)."""
    n = 2**ZOOM
    s = math.sin(math.radians(lat))
    px = (lng + 180) / 360 * n * TILE  # Web Mercator pixel, as the tiles use
    py = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n * TILE
    left, top = int(px) - WIDTH // 2, int(py) - HEIGHT // 2

    grid = [(x, y) for x in range(left // TILE, (left + WIDTH - 1) // TILE + 1)
            for y in range(top // TILE, (top + HEIGHT - 1) // TILE + 1) if 0 <= y < n]
    tiles = list(_tiles.map(_fetch, grid))
    img = Image.new("RGB", (WIDTH, HEIGHT), (242, 239, 233))
    for (x, y), data in zip(grid, tiles):
        if data:
            img.paste(Image.open(io.BytesIO(data)).convert("RGB"), (x * TILE - left, y * TILE - top))

    cx, cy = px - left, py - top
    radius = accuracy_m * n * TILE / (40_075_016.686 * math.cos(math.radians(lat)))  # metres → pixels
    over = Image.new("RGBA", (WIDTH * SCALE, HEIGHT * SCALE))
    d = ImageDraw.Draw(over)
    if radius >= 8:  # smaller hides under the pin
        r = min(radius, WIDTH) * SCALE
        d.ellipse((cx * SCALE - r, cy * SCALE - r, cx * SCALE + r, cy * SCALE + r), fill=(*BLUE, 40), outline=(*BLUE, 160), width=2 * SCALE)
    _pin(d, cx * SCALE, cy * SCALE, SCALE)
    over = over.convert("RGBa").resize((WIDTH, HEIGHT), Image.LANCZOS).convert("RGBA")  # premultiplied: no dark fringe
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")

    if credit:
        d = ImageDraw.Draw(img, "RGBA")
        font = ImageFont.load_default(size=9)
        l, t, r, b = d.textbbox((0, 0), CREDIT, font=font)
        w, h = r - l + 6, b - t + 4
        d.rectangle((WIDTH - w, HEIGHT - h, WIDTH, HEIGHT), fill=(255, 255, 255, 150))
        d.text((WIDTH - w + 3 - l, HEIGHT - h + 2 - t), CREDIT, font=font, fill=(80, 80, 80))

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue(), all(tiles)


def image(lat: float, lng: float, accuracy_m: float, credit: bool) -> tuple[bytes, bool]:
    """The image: from memory, or waits for the render already running (warm), or renders it now."""
    return _job(lat, lng, accuracy_m, credit).result()


def warm(lat: float, lng: float, accuracy_m: float) -> None:
    """Start rendering a just-saved location's default image in the background."""
    _job(lat, lng, accuracy_m, False)


def _job(*key) -> Future:
    with _lock:
        job = _images.get(key)
        if job is None or (job.done() and (job.exception() or not job.result()[1])):  # new, or failed: render again
            job = _images[key] = _renders.submit(render, *key)
        _images.move_to_end(key)
        while len(_images) > KEEP:
            _images.popitem(last=False)
        return job

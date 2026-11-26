import os
import tempfile

# Settings are read when app.* is first imported, so point them at a throwaway DB before any test module loads.
os.environ.update(
    DATABASE_URL=f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'test.db')}",
    ALLOWED_ORIGINS="http://site.test",
    SITE_URL="http://site.test",
    ALLOWED_HOSTS="",
    BREVO_API_KEY="",
    SMTP_HOST="",
)


import io  # noqa: E402

import pytest  # noqa: E402

TILE_COLOUR = (200, 220, 240)


@pytest.fixture(autouse=True)
def offline_map_tiles(monkeypatch):
    """Map images use a plain fake tile (no network in tests), and no image carries over between tests."""
    from PIL import Image

    from app import staticmap

    out = io.BytesIO()
    Image.new("RGB", (256, 256), TILE_COLOUR).save(out, "PNG")
    monkeypatch.setattr(staticmap, "tile", lambda z, x, y: out.getvalue())
    staticmap._images.clear()

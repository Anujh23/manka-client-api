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

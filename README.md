# Client API

The backend for client login on the website (`/clientlogin/`). FastAPI + Postgres. Runs on Render (free) with Neon Postgres, or on an Oracle Cloud Always Free VM behind Caddy (both set up below).

```
browser ── mankacommerceandconsultancy.co.in/clientlogin/   (static site, GitHub Pages)
   │  fetch(..., credentials: 'include')
   ▼
api.mankacommerceandconsultancy.co.in   (Render: FastAPI → Neon Postgres, or Oracle VM: Caddy → FastAPI → Postgres)
```

The site and the API sit on the same registrable domain, so the `session` cookie is first-party: `HttpOnly`, `Secure`, `SameSite=Lax`.

## Endpoints

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/auth/login` | `{email, password}` | 200 `{name, email}` + session cookie · 401 wrong · 429 locked 15 min |
| POST | `/auth/logout` | | 204 |
| GET | `/auth/me` | | 200 `{name, email, is_admin}` · 401 |
| POST | `/auth/forgot` | `{email}` | always 204; emails a 1-hour reset link if the account exists |
| POST | `/auth/reset` | `{token, password}` | 204 (logs out every browser) · 400 expired · 422 password < 10 chars |
| GET | `/account` | | 200 `{name, email, balance_paise, prices, key, entries}` (dashboard, session cookie) |
| POST | `/account/keys` | | 200 with the full `key`, **shown once**. One key per client: this revokes the previous one (Regenerate) |
| POST | `/account/try/{product}` | product input | Same result and charge as `/v1`, from the dashboard |
| GET | `/v1/ping` | `Authorization: Bearer <key>` | 200 `{id, ok, client}` · 401 bad key (not charged) |
| GET | `/health` | | `{ok: true}` |

### Admin (/admin, admins only)

An admin is a normal client account marked with `python -m app.cli admin EMAIL`. Admins log in at `/clientlogin/`, and their dashboard shows an **Admin** link to `/admin/`.

| Method | Path | Body | Does |
|---|---|---|---|
| GET | `/admin/clients` | | All clients with balance and admin/disabled flags |
| POST | `/admin/clients` | `{name, email, password}` | Create a client (password must be 10+ chars) |
| POST | `/admin/clients/{id}` | `{name, email}` | Edit |
| POST | `/admin/clients/{id}/password` | `{password}` | Set a password and log the client out everywhere |
| POST | `/admin/clients/{id}/disable` / `enable` | | **"Delete" = disable**: login and API keys stop, history is kept, reversible. An admin can't disable themselves. |
| POST | `/admin/clients/{id}/wallet` | `{action: credit\|debit, rupees, note}` | Recharge or adjustment, written to the ledger as "note (by admin@…)". Never goes below ₹0; max ₹10 lakh per entry. |

### Product APIs (/v1, Bearer key)

| Product | Route | Price |
|---|---|---|
| PAN check | `POST /v1/pan/check` `{pan}` | ₹0.50 |
| GSTIN check | `POST /v1/gstin/check` `{gstin}` | ₹1 |
| IFSC lookup | `GET /v1/ifsc/{ifsc}` | ₹0.50 |
| Maps API | `POST /v1/maps/link` `{reference}` → one-time link (24 h); `GET /v1/maps/link/{request}` → `status`, `lat`, `lng`, `accuracy_m`, `image` (free) | ₹5 per link |
| Maps JavaScript | `<script src="/v1/maps.js?key=KEY">` then `MapsAPI.locate()` (`POST /v1/maps/locate {lat, lng, accuracy_m}`) → coordinates + `image` | ₹5 per locate |
| BSA | `POST /v1/bsa/analyze` multipart `file` (PDF, max 10 MB) + optional `password` → full report (verdict, reconciliation, salary, loans, bounces, flags, transactions; schema in `app/bsa/REPORT_SCHEMA.md`). Only a successful analysis is charged; statements are never stored (temp file, deleted after). Errors (422, refunded or free): `password_required`, `aa_statement`, `unsupported_file`, `analysis_timeout`, `invalid_request` | ₹25 per statement |
| Map image | `GET /v1/maps/image/{request}.png`: PNG map (640×400, pin, accuracy circle) of any located request; no key, for `<img>`. No map credit on it by default: the client must show "© OpenStreetMap" next to the map. `?credit=1` adds a small label instead | Included |

- One file per product in `app/products/`. Add it to `PRODUCTS` in `app/products/__init__.py` and it gets a `/v1` route, a dashboard Try and a price. Client docs: website repo `src/data/api-content.ts` (page `/docs/`).
- Success is the result plus `id` (`call_<n>`). Errors: `{"error": {"code", "message"}, "id"?}` with codes `invalid_key` 401, `insufficient_balance` 402, `not_found` 404, `invalid_request` 422, `server_error` 500.
- **Only a 200 is charged.** Bad input (422) is checked before charging; not found (404) and failures (500) are refunded. A structurally invalid PAN/GSTIN is a 200 with `format_valid: false` (the check ran).
- Maps API: the person opens the link on the website (`/locate/?t=…`, unbranded), taps Share location, and the phone's GPS fix is posted once to `POST /locate/{token}` (`app/locate.py`). The location comes from the phone's GPS. `GET /account/locations` lists requests for the dashboard.
- BSA load limit (`app/products/bsa.py` `slot()`, settings `BSA_MAX_PARALLEL` 2 / `BSA_QUEUE_MAX` 8 / `BSA_QUEUE_WAIT` 60 s): at most 2 analyses run at once (a big statement needs ~100 MB; Render free has 512 MB), up to 8 more wait for a slot, anything else gets a free `429 busy` + `Retry-After: 30` before any charge. Raise the numbers on a bigger server.
- Map images (`app/staticmap.py`): built on the server from OpenStreetMap tiles (OSM France) with Pillow. Rendering starts in the background the moment a location is saved, tiles are fetched in parallel, and tiles plus the last 64 images stay in memory: about 2 s for a new area, about 0.3 s nearby or repeated. Volunteer servers: if volume grows, move to a paid tile host.
- Maps JavaScript (`app/static/maps.js`): GPS in the visitor's browser, Nominatim address. /v1 allows any origin (CORS `*`, no cookies), but a browser call (it carries Origin) only works from the client's listed websites (`POST /account/websites`, table `websites`); others get 403 `origin_not_allowed`, uncharged. Calls without Origin (curl, servers) are unaffected.
- BSA (`app/bsa/`): a PDF-only copy of the BSA project's engine (copied 2026-11-26; no spreadsheets, no pandas), run in a child process with a 120 s limit (`BSA_ANALYZE_TIMEOUT`). Don't edit its logic here: to refresh, re-copy `bsa/engine` from the BSA project, keep the PDF-only edit in `pipeline.py` and the `app.bsa` paths in `isolated.py`, and re-run the regression check against the BSA samples. `Rejected` (`app/products/__init__.py`) is how a product refuses input after charging: refunded, with its own status and code.
- PAN and GSTIN are format/check-digit checks only, never "verified". IFSC data is the open Razorpay IFSC dataset (MIT): download `IFSC.csv` from https://github.com/razorpay/ifsc/releases and run `python -m app.cli load-ifsc IFSC.csv v2.0.62 2026-09-01` (tag and date of that release), about monthly.

### Wallet and pay-per-call

- Money is integer **paise**. `wallet.py` is the only code that changes balances, and every change writes a ledger row.
- Prices live on each product in `app/products/` (paise per successful call). Each client has **one API key** (`mcc_…`). Every call, by key or the dashboard's Try, is paid from the prepaid balance (starts at ₹0, nothing free); too little balance gets 402 `insufficient_balance`.
- A call is debited before the product runs (atomic, so it can't overdraw); if balance is short you get **402**, and if the product fails it's **refunded**.
- Recharge is manual for now: the client sends the amount from the dashboard (WhatsApp/email), pays, and you run `python -m app.cli credit client@company.com 1000 "UPI UTR 412345678901"`. A payment gateway can call `wallet.credit()` later.

Security: Argon2id hashes, only SHA-256 of session/reset tokens is stored, lockout after 5 failures per email (or 30 per IP) in 15 min, and an Origin check plus CORS limited to the site.

## Local dev

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows; bin/ on Linux
.venv/Scripts/python -m app.cli add you@example.com "Your Name"
.venv/Scripts/uvicorn app.main:app --reload --port 8000
.venv/Scripts/python -m pytest -q
```

Uses SQLite (`dev.db`) by default. Without SMTP, reset links are printed to the log. The site's dev server (localhost:4321) calls `http://localhost:8000` automatically.

## Clients

There's no public sign-up. You create every account:

```bash
docker compose exec api python -m app.cli add client@company.com "Company Name"
docker compose exec api python -m app.cli password client@company.com
docker compose exec api python -m app.cli disable client@company.com
docker compose exec api python -m app.cli list
docker compose exec api python -m app.cli credit client@company.com 1000 "UPI UTR 412345678901"
docker compose exec api python -m app.cli balance client@company.com
docker compose exec api python -m app.cli admin you@company.com          # allow /admin
```

## Deploy on Render + Neon (free, no card)

Render runs the API from the `Dockerfile` (free web service `manka-client-api`, Singapore). Postgres is on Neon (free, permanent).

1. **Neon** (neon.tech, sign in with GitHub): New project → region **AWS Asia Pacific (Singapore)** → copy the **pooled** connection string.
2. **Brevo**: SMTP & API → API Keys → generate a key. Render's free tier blocks SMTP ports, so mail goes over Brevo's HTTPS API. `contact@mankacommerceandconsultancy.co.in` must be a verified sender.
3. **Render** (render.com, sign in with GitHub): New → **Blueprint** → pick this repo. `render.yaml` sets everything; paste `DATABASE_URL` (Neon) and `BREVO_API_KEY` when asked. Check `https://manka-client-api.onrender.com/health` → `{"ok":true}`.
4. **Domain**: service → Settings → Custom Domains → add `api.mankacommerceandconsultancy.co.in`. In Domain India DNS, **add** `CNAME api → manka-client-api.onrender.com`. Render issues HTTPS. After that, only `/health` answers on the onrender.com address (`ALLOWED_HOSTS`).
5. **Keep it awake**: free services sleep after 15 idle minutes (about 1 min to wake). A free cron-job.org job hitting `https://api.mankacommerceandconsultancy.co.in/health` every 10 minutes keeps it warm. 750 free hours a month covers 24x7.
6. **Clients**: Render's free tier has no shell, so run the CLI from your laptop against Neon. Put `DATABASE_URL=<neon url>` in a local `.env` (git-ignored), then `.venv/Scripts/python -m app.cli add client@company.com "Company Name"`.

## Deploy on Oracle Cloud (Always Free)

1. **VM**: Compute → Instances → Create. Image: Ubuntu 24.04. Shape: `VM.Standard.A1.Flex` (Ampere, 1 OCPU / 6 GB is plenty). If A1 says "out of capacity", use `VM.Standard.E2.1.Micro` (AMD, also free). Add your SSH key. Note the public IP.
2. **Ports**: Instance → Subnet → Security List → Add Ingress Rules: source `0.0.0.0/0`, TCP, ports `80` and `443`.
3. **DNS** (Domain India panel, only *add* a record): `A` record `api` → the VM's public IP.
4. **Server**:
   ```bash
   ssh ubuntu@<ip>
   git clone <this repo> manka-client-api && cd manka-client-api
   bash deploy/setup-vm.sh          # docker, firewall, swap, nightly backup
   exit                             # log back in so the docker group applies
   ssh ubuntu@<ip> && cd manka-client-api
   cp .env.example .env && nano .env   # POSTGRES_PASSWORD (openssl rand -hex 24), Brevo SMTP login
   docker compose up -d --build
   curl https://api.mankacommerceandconsultancy.co.in/health
   ```
5. **First client**: `docker compose exec api python -m app.cli add ...`

Update: `git pull && docker compose up -d --build`. Logs: `docker compose logs -f api`.
Backups: `~/backups/manka-YYYY-MM-DD.sql.gz` nightly, 14 days kept. Copy them off the VM now and then.
Restore: `gunzip -c file.sql.gz | docker compose exec -T db psql -U manka manka`.

Note: Oracle may reclaim an Always Free VM that stays idle (very low CPU) for 7 days. Upgrading the tenancy to Pay As You Go keeps the free resources free and stops reclamation.

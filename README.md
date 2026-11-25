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
| GET | `/auth/me` | | 200 `{name, email}` · 401 |
| POST | `/auth/forgot` | `{email}` | always 204; emails a 1-hour reset link if the account exists |
| POST | `/auth/reset` | `{token, password}` | 204 (logs out every browser) · 400 expired · 422 password < 10 chars |
| GET | `/account` | | 200 `{name, email, balance_paise, test_balance_paise, keys, entries}` (dashboard, session cookie) |
| POST | `/account/keys` | `{mode: "test"\|"live"}` | 200 with the full `key`, **shown once** |
| POST | `/account/keys/{id}/revoke` | | 204 |
| GET | `/v1/ping` | `Authorization: Bearer <key>` | 200 `{ok, mode, client}` · 401 bad key |
| GET | `/health` | | `{ok: true}` |

### Wallet and pay-per-call

- Money is integer **paise**. `wallet.py` is the only code that changes balances, and every change writes a ledger row.
- Prices live in `products.py` (paise per successful call). **Live keys** pay from the wallet. **Test keys** pay the same price from a one-time **₹100 test credit** per client (`TEST_CREDIT_PAISE`), then get 402 "Test credit used up". A product route charges with `Depends(charged("<name>"))` from `api.py`.
- A live call is debited before the product runs (atomic, so it can't overdraw); if balance is short you get **402**, and if the product fails it's **refunded**.
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
```

## Deploy on Render + Neon (free, no card)

Render runs the API from the `Dockerfile` (free web service, Singapore). Postgres is on Neon (free, permanent).

1. **Neon** (neon.tech, sign in with GitHub): New project → region **AWS Asia Pacific (Singapore)** → copy the **pooled** connection string.
2. **Brevo**: SMTP & API → API Keys → generate a key. Render's free tier blocks SMTP ports, so mail goes over Brevo's HTTPS API. `contact@mankacommerceandconsultancy.co.in` must be a verified sender.
3. **Render** (render.com, sign in with GitHub): New → **Blueprint** → pick this repo. `render.yaml` sets everything; paste `DATABASE_URL` (Neon) and `BREVO_API_KEY` when asked. Check `https://mankaconsulting.onrender.com/health` → `{"ok":true}`.
4. **Domain**: service → Settings → Custom Domains → add `api.mankacommerceandconsultancy.co.in`. In Domain India DNS, **add** `CNAME api → mankaconsulting.onrender.com`. Render issues HTTPS. After that, only `/health` answers on the onrender.com address (`ALLOWED_HOSTS`).
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

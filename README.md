# Listables (Graduation Project)

This repo contains your existing HTML/CSS storefront plus a simple FastAPI backend to make it a working e-commerce app (products, cart, checkout, orders).

## Setup (Windows / PowerShell)

### 1) Create a `.env`

Copy `.env.example` to `.env` in the project root and change the values:

- `SECRET_KEY`: set anything random
- `DATABASE_URL`: points to Postgres

### 2) Database choice

**Option A (recommended for graduation / easiest): SQLite**

- Leave `DATABASE_URL="sqlite:///./listables.db"` in your `.env`
- No Docker needed

**Option B (recommended stack): Postgres via Docker**

Set `DATABASE_URL` to Postgres, then start Docker Desktop and run:

```powershell
docker compose up -d
```

If you got an error about Docker daemon not running, start **Docker Desktop** first, then re-run the command.

### 3) Install backend dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r backend\requirements.txt
```

### 4) Run the server

```powershell
.\.venv\Scripts\python -m uvicorn backend.app.main:app --reload --port 8000
```

Open:

- Home: `http://localhost:8000/index.html`
- Cart: `http://localhost:8000/cart.html`
- Checkout: `http://localhost:8000/checkout.html`

## Notes

- The app auto-creates tables on startup.
- It seeds a few demo products on first run.
- Stripe is optional: set `STRIPE_ENABLED=true` and `STRIPE_SECRET_KEY=...` in `.env` to enable card payments.


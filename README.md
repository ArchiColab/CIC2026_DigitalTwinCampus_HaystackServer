# Campus Digital Twin — Web Deployment

A browser-based digital twin of the Myllypuro campus floor. Users sign in with their own **Empathic Building** account, and the app overlays live sensor data (temperature, CO₂, occupancy, humidity) directly onto the 3D IFC model.

Built as part of the **Digital Twins in Construction** course at Metropolia University of Applied Sciences, Computing in Construction programme, 2026.

**Team:** Duy-Hien Ha · Chau Nguyen · Mohsen Parsaei

---

## How it works

```
Browser (IFC viewer + login)
        ↓  POST /api/login  (EB email + password)
FastAPI ──► EBAuthManager.force_login()
        ↓  httponly session cookie (8 hours)
Redis   ──► stores session + per-user sensor cache (3 min TTL)
        ↓  GET /api/points  (cookie auth)
FastAPI ──► cache hit → return JSON instantly
            cache miss → fetch from EB API → cache → return
        ↓
Three.js + web-ifc renders IFC model with sensor heatmap
```

Sensor data is **cached per user for 3 minutes**. This means a class of 30 students logging in simultaneously will each make at most one EB API call on first load, then read from cache — no rate-limiting risk.

---

## Project structure

```
campus-dt/
├── main.py                      ← FastAPI app (auth, sessions, /api/points)
├── Procfile                     ← Railway start command
├── requirements.txt
├── .env.example                 ← copy to .env for local dev
├── .gitignore
│
├── services/                    ← unchanged from original Blender project
│   ├── auth.py                  ← EBAuthManager (login, token refresh)
│   ├── eb_api.py                ← EBApiClient (REST wrapper)
│   └── haystack_converter.py   ← raw EB sensor → Hayson format
│
├── data/
│   └── space_mapping.json       ← IFC space metadata (generate with notebook)
│
└── static/
    └── index.html               ← full IFC viewer + login screen (single file)
```

> **Note:** The IFC model file (`ARK_MET_F1.ifc`) is **not** committed to Git due to its size.  
> Upload it manually after deployment (see step 5 below), or use Git LFS.

---

## Local development

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- The `ARK_MET_F1.ifc` file
- `data/space_mapping.json` (generate with `20260315_IfcOpenShell.ipynb`)

Docker Compose starts both the FastAPI app and Redis together — you don't need
to install Python, Redis, or any dependencies on your machine directly.

### 1. Clone the repo

```bash
git clone https://github.com/your-org/campus-dt.git
cd campus-dt
```

### 2. Generate space mapping (if not already done)

```bash
# Place ARK_MET_F1.ifc in the project root, then run:
jupyter notebook 20260315_IfcOpenShell.ipynb
# This creates data/space_mapping.json
```

### 3. Start everything with Docker Compose

```bash
docker compose up --build
```

This starts two containers on a shared network:

| Container | Role | Internal address |
|-----------|------|-----------------|
| `app` | FastAPI server | http://localhost:8000 |
| `redis` | Session & cache store | `redis:6379` (internal only) |

`REDIS_URL=redis://redis:6379` is set automatically in `docker-compose.yml` —
no `.env` file needed for local development.

Open [http://localhost:8000](http://localhost:8000) — you'll see the login screen.  
Sign in with your Empathic Building credentials, then upload your IFC file.

### Stopping

```bash
docker compose down
```

### Rebuilding after code changes

The `app` container mounts your source code as a volume, so most changes
are reflected immediately without a rebuild. If you change `requirements.txt`
or the `Dockerfile`, rebuild with:

```bash
docker compose up --build
```

---

## Deploying to Railway

### Step 1 — Create Railway project

1. Go to [railway.app](https://railway.app) and create a new project
2. Click **Add Service → GitHub Repo** and connect this repository

### Step 2 — Add Redis plugin

In your Railway project dashboard:

1. Click **+ New** → **Database** → **Add Redis**
2. Railway automatically injects `REDIS_URL` into your service — no manual config needed

### Step 3 — Set environment variables

In your Railway service → **Variables** tab, add:

| Variable | Value |
|----------|-------|
| `REDIS_URL` | *(auto-filled by Redis plugin — do not change)* |

That's it. No EB credentials go in Railway — each user provides their own at login.

### Step 4 — Deploy

Railway deploys automatically when you push to your connected branch.  
To trigger a manual deploy: **Railway dashboard → Deploy → Deploy Now**

### Step 5 — Check the deployment

Once deployed, Railway gives you a URL like `https://campus-dt.up.railway.app`.

Visit `/api/status` after logging in to confirm Redis and the EB API are connected:

```json
{
  "email": "you@metropolia.fi",
  "cache_exists": true,
  "cache_expires_in": "178s",
  "last_updated": "2026-04-20T09:14:33+00:00"
}
```

---

## API reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `POST` | `/api/login` | — | Authenticate with EB credentials, sets session cookie |
| `POST` | `/api/logout` | cookie | Clear session and Redis cache entry |
| `GET` | `/api/me` | cookie | Returns `{ email }` — used on page load to skip login if session exists |
| `GET` | `/api/points` | cookie | Returns Hayson sensor array. Header `X-Cache-Updated` shows data freshness |
| `GET` | `/api/status` | cookie | Debug: cache TTL, last update time |

### `/api/login` request body

```json
{
  "email": "you@metropolia.fi",
  "password": "your-eb-password"
}
```

### `/api/points` response

Returns the same Hayson JSON format as `data/haystack_latest.json` from the Blender workflow — the frontend `processSensorData()` function is identical for both sources.

---

## Adding the IFC model to static files

The IFC file is too large to commit to Git. Instead, place it in `static/` so it can be served by FastAPI's `StaticFiles` mount:

```bash
# After deploying, use Railway's file system or rsync:
cp ARK_MET_F1.ifc static/

# Or use the Railway CLI:
railway run cp ARK_MET_F1.ifc static/
```

Users then drag-and-drop the file from their local machine — the viewer loads it entirely in the browser using `web-ifc` (no server-side IFC processing at runtime).

---

## Sensor data flow details

### Cache behaviour

- Each user has their own Redis cache key: `cache:{email}`
- TTL: **3 minutes** (set by `USER_CACHE_TTL` in `main.py`)
- On cache hit: data returned instantly, no EB API call
- On cache miss: live fetch from EB, result cached, returned
- The `X-Cache-Updated` response header tells the browser when the data was fetched
- The browser auto-refreshes every 3 minutes via `setInterval` in `index.html`

### Session behaviour

- Sessions stored in Redis under `session:{token}`
- TTL: **8 hours** (set by `SESSION_TTL` in `main.py`)
- The `httponly` + `samesite=strict` + `secure` cookie flags prevent XSS and CSRF
- On logout, both the session and the user's sensor cache are deleted

### Changing cache or session duration

Edit these constants at the top of `main.py`:

```python
SESSION_TTL    = 60 * 60 * 8   # 8 hours
USER_CACHE_TTL = 60 * 3        # 3 minutes
```

---

## Blender workflow (still works unchanged)

The web deployment does not replace the local Blender + Bonsai workflow — it runs in parallel.

```bash
# Run the data collector (writes haystack_latest.json for Blender)
python main_collector.py

# Then in Blender, run scripts in order:
#   make_all_transparent.py
#   campus_dt_visualizer.py
#   dt_click_inspect.py
```

The `services/` folder is shared between both workflows.

---

## Troubleshooting

**Login says "Invalid EB credentials"**  
→ Double-check your Empathic Building email and password. The app forwards credentials directly to the EB API — if your credentials work on the EB platform, they'll work here.

**Sensor panel shows "Loading…" forever**  
→ Check the browser console for errors. Visit `/api/status` to confirm your session and Redis are working. If the EB API is down, the fetch will timeout after 30s.

**`REDIS_URL environment variable is not set`**  
→ You haven't added the Redis plugin in Railway. Go to your project → **+ New** → **Database** → **Add Redis**.

**IFC model loads but no sensor colors appear**  
→ The `data/space_mapping.json` may be missing or outdated. Re-run `20260315_IfcOpenShell.ipynb` to regenerate it from your IFC file.

**Railway build fails on `ifcopenshell`**  
→ Add a `nixpacks.toml` to specify the Python version:
```toml
[phases.setup]
nixPkgs = ["python311", "python311Packages.pip"]
```

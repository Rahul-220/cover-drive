# Ask‑IPL / CoverDrive

Natural language → SQL for IPL stats. ETL converts Cricsheet JSON to Parquet, DuckDB powers queries, FastAPI serves an API and a tiny React (UMD) UI.

![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)

Replace OWNER/REPO with your repo path after pushing.

**Features**
- FastAPI backend with `/nlq` endpoint (NL → validated DuckDB SELECT).
- Supports two data sources:
  - Hive‑partitioned Parquet (`data/parquet/{matches,deliveries}/season=YYYY`).
  - Single DuckDB snapshot file (`data/ipl.duckdb`) with `deliveries` and `matches` tables.
- React UMD single‑page UI served by FastAPI.
- Dockerfile + GitHub Actions CI + Render deployment spec.

**Repo Structure**
- `etl/`: Build Parquet from Cricsheet JSON (`filtered/` → `parquet/`).
- `backend/`: FastAPI app, mounts frontend and executes SQL.
- `frontend/`: Static `index.html` + `CoverDrive.js` (global React/DOM).
- `scripts/`: Helpers (run SQL against Parquet, Gemini prompt->SQL utilities).
- `data/`: Local, ignored. Parquet or DuckDB snapshot lives here.

---

## Prerequisites
- Python 3.11
- One of:
  - Parquet in `data/parquet/` (see ETL below), or
  - DuckDB snapshot at `data/ipl.duckdb` (see Option B below)
- For NLQ (Gemini): set `GEMINI_API_KEY` in environment (never commit secrets).

## Run Locally (no Docker)
1) Create and activate venv, install deps

```
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

2) Either generate Parquet or point to a DuckDB snapshot

```
# Parquet path
python etl/parquet_etl.py

# OR: use a DuckDB snapshot
python backend/build_snapshot.py --db data/ipl.duckdb  # builds from existing Parquet if present
```

3) Start API + UI

```
# Using Parquet fallback
uvicorn backend.app:app --reload --port 8000

# Using DuckDB snapshot
set DUCKDB_PATH=data/ipl.duckdb && uvicorn backend.app:app --reload --port 8000  # PowerShell
# or (bash): DUCKDB_PATH=data/ipl.duckdb uvicorn backend.app:app --reload --port 8000
```

Notes:
- `/nlq` uses Gemini at request time; ensure `GEMINI_API_KEY` is set if you exercise that endpoint.
- Without Parquet or a snapshot, queries return empty results but the app boots.

## Data snapshot (Option B)
Build once locally, host the file, and let Render download it on startup.

1) Build ipl.duckdb locally from Parquet

```
python backend/build_snapshot.py --db data/ipl.duckdb
```

2) Host the file (examples)
- GitHub Releases: upload `ipl.duckdb` and copy the asset URL.
- S3/Cloud storage: upload and use a presigned/public URL.

3) Render download + startup
- Set `DUCKDB_URL` in Render (public URL to `ipl.duckdb`).
- `render.yaml` starts with a small downloader that saves to `data/ipl.duckdb` if missing, exports `DUCKDB_PATH`, and runs uvicorn.
- To update data later: rebuild locally, upload new snapshot, update the hosted file/URL, and redeploy or restart.

## Run via Docker

Build and run locally:

```
docker build -t coverdrive:local .
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY=your_key \
  -e DUCKDB_PATH=/app/data/ipl.duckdb \
  -v %cd%/data:/app/data \  # Windows PowerShell (use $(pwd) on bash)
  coverdrive:local
# open http://localhost:8000
```

Mounting `./data` allows the container to see your local `ipl.duckdb` or Parquet.

## CI (GitHub Actions)
- Workflow: `.github/workflows/ci.yml`
  - Job 1: Python checks (install, compile key files, import app, DuckDB sanity).
  - Job 2: Docker build (no push). Add deploy afterwards as needed.

Badge will reflect CI status after pushing and fixing the badge URL.

## Deploy: Render (recommended)
This repo includes `render.yaml`. Steps:

1) Push to GitHub (see commands below).
2) In Render dashboard: New → Blueprint → select your repo.
3) Confirm service:
   - Type: Web Service
   - Runtime: Python
   - Build: `pip install -r requirements.txt`
   - Start: downloads `ipl.duckdb` to `data/ipl.duckdb` if missing, then starts uvicorn.
4) Environment variables:
   - `GEMINI_API_KEY`: for NLQ endpoint
   - `DUCKDB_URL`: public URL to snapshot file (optional for local dev)
5) Deploy. Render auto‑rebuilds on pushes to default branch.

`render.yaml` excerpt:

```
services:
  - type: web
    name: coverdrive
    runtime: python
    disk:
      name: coverdrive-data
      mountPath: /opt/render/project/src/data
      sizeGB: 5
    buildCommand: pip install -r requirements.txt
    startCommand: |
      bash -lc "set -euo pipefail
      export DUCKDB_PATH=data/ipl.duckdb
      python - <<'PY'
      import os, urllib.request, pathlib
      url = os.environ.get('DUCKDB_URL')
      p = pathlib.Path('data/ipl.duckdb')
      p.parent.mkdir(parents=True, exist_ok=True)
      if url and not p.exists():
          print('Downloading DuckDB snapshot...', flush=True)
          with urllib.request.urlopen(url) as r, open(p, 'wb') as f:
              f.write(r.read())
          print('Download complete.', flush=True)
      PY
      exec uvicorn backend.app:app --host 0.0.0.0 --port $PORT"
    envVars:
      - key: GEMINI_API_KEY
        sync: false
      - key: DUCKDB_URL
        sync: false
```

## Alternates (brief)

### Fly.io
Sample `fly.toml` (not added to repo by default):

```
# fly.toml
app = "coverdrive"
primary_region = "iad"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = true
  auto_start_machines = true
  min_machines_running = 0

[[vm]]
  size = "shared-cpu-1x"
```

Deploy:

```
fly launch --no-deploy
fly secrets set GEMINI_API_KEY=your_key
fly deploy
```

Optional GH Actions job snippet:

```
  fly-deploy:
    needs: docker
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: superfly/flyctl-actions/setup-flyctl@v1
      - run: fly deploy --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
```

### Railway

```
railway up  # or connect GitHub repo in dashboard
railway variables set GEMINI_API_KEY=your_key
```

Define service start command in Railway to:

```
uvicorn backend.app:app --host 0.0.0.0 --port $PORT
```

### AWS ECS (Fargate)
1) Build and push to ECR:

```
AWS_ACCOUNT_ID=123456789012
REGION=us-east-1
REPO=coverdrive

aws ecr create-repository --repository-name $REPO || true
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

docker build -t $REPO:latest .
docker tag $REPO:latest $AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
```

2) Update your ECS task definition container to use that image and port 8000. Add `GEMINI_API_KEY` as a task env var. Then update service:

```
aws ecs update-service --cluster your-cluster --service your-service --force-new-deployment
```

---

## ETL Details
- Input: `data/filtered/<season>/*.json` (Cricsheet unzipped per season).
- Run: `python etl/parquet_etl.py` → writes `data/parquet/matches` and `data/parquet/deliveries`, Hive‑partitioned by `season`.

## Security
- Do not commit `.env` or secrets. CI and Docker do not require the key unless you exercise `/nlq`.
- `.gitignore` excludes `data/` and common artifacts.

---

## GitHub: Initialize, Commit, Push

```
git init
git add .
git commit -m "chore: bootstrap CI, Docker, deploy"
git branch -M main
git remote add origin git@github.com:YOUR_USER/YOUR_REPO.git
git push -u origin main
```

## Connect to Render
1) Create a new Web Service (Blueprint) from your GitHub repo.
2) Confirm build/start commands or rely on `render.yaml`.
3) Add env vars `GEMINI_API_KEY` and (optionally) `DUCKDB_URL` in Render → Environment.
4) Deploy.


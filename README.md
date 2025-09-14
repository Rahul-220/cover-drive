# Ask‑IPL / CoverDrive

Natural language → SQL for IPL stats. ETL converts Cricsheet JSON to Parquet, DuckDB powers queries, FastAPI serves an API and a tiny React (UMD) UI.

![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)

Replace OWNER/REPO with your repo path after pushing.

**Features**
- FastAPI backend with `/nlq` endpoint (NL → validated DuckDB SELECT).
- DuckDB views over Hive‑partitioned Parquet (`data/parquet/{matches,deliveries}/season=YYYY`).
- React UMD single‑page UI served by FastAPI.
- Dockerfile + GitHub Actions CI + Render deployment spec.

**Repo Structure**
- `etl/`: Build Parquet from Cricsheet JSON (`filtered/` → `parquet/`).
- `backend/`: FastAPI app, mounts frontend and executes SQL.
- `frontend/`: Static `index.html` + `CoverDrive.js` (global React/DOM).
- `scripts/`: Helpers (run SQL against Parquet, Gemini prompt->SQL utilities).
- `data/`: Local, ignored. Place Parquet here for queries.

---

## Prerequisites
- Python 3.11
- Parquet produced in `data/parquet/` (see ETL below)
- For NLQ (Gemini): set `GEMINI_API_KEY` in environment (never commit secrets). The app imports the Gemini helper but only calls it on `/nlq`.

## Run Locally (no Docker)
1) Create and activate venv, install deps

```
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

2) Generate Parquet (if you have Cricsheet JSON)

```
python etl/parquet_etl.py
```

3) Start API + UI

```
uvicorn backend.app:app --reload --port 8000
# open http://localhost:8000
```

Notes:
- `/nlq` uses Gemini at request time; ensure `GEMINI_API_KEY` is set if you exercise that endpoint.
- Without Parquet present, most queries will return empty/err.

## Run via Docker

Build and run locally:

```
docker build -t coverdrive:local .
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY=your_key \
  -v %cd%/data:/app/data \  # Windows PowerShell (use $(pwd) on bash)
  coverdrive:local
# open http://localhost:8000
```

Mounting `./data` allows the container to see your local Parquet.

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
   - Start: `uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-10000}`
4) Environment variables: add `GEMINI_API_KEY` (leave out of git).
5) Deploy. Render auto‑rebuilds on pushes to default branch.

`render.yaml` excerpt:

```
services:
  - type: web
    name: coverdrive
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: bash -lc 'uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-10000}'
    envVars:
      - key: GEMINI_API_KEY
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
3) Add env var `GEMINI_API_KEY` in Render → Environment.
4) Deploy.

# Ask‑IPL / CoverDrive

Natural language → SQL for IPL stats. ETL converts Cricsheet JSON to Parquet, DuckDB powers queries, FastAPI serves an API and a tiny React (UMD) UI.



Replace OWNER/REPO with your repo path after pushing.

**Features**
- FastAPI backend with `/nlq` endpoint (NL → validated DuckDB SELECT).
- Supports two data sources:
  - Hive‑partitioned Parquet (`data/parquet/{matches,deliveries}/season=YYYY`).
  - Single DuckDB snapshot file (`data/ipl.duckdb`) with `deliveries` and `matches` tables.
- React UMD single‑page UI served by FastAPI.
- Dockerfile

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

## Run Locally (no Docker)
1) Create and activate venv, install deps

```
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

2) Start API + UI

```
# Using Parquet fallback
uvicorn backend.app:app --reload --port 8000
```

3) Build and run locally:

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


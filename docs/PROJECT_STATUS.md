# Project Status (Ask-IPL / CoverDrive)

Date: 2026-02-01

## What’s built so far (high-level)
- Natural-language cricket questions are turned into SQL and run on IPL data.
- Data can be read from either Parquet files (season-partitioned) or a single DuckDB file.
- FastAPI serves the API and the static UI.
- DuckDB is used in-process to execute SQL over views `deliveries_q` and `matches_q`.
- A clarification loop exists for ambiguous entities (e.g., multiple players with same surname).

## Core flow (simple)
1) User asks a question in English.
2) The system rewrites the question to canonical names (teams/venues/players) or asks for clarification.
3) Gemini generates a DuckDB SELECT query.
4) SQL is normalized and safety-checked.
5) DuckDB runs the query over the data views.
6) Results (and optional SQL) are returned to the UI.

## Data storage options
- Parquet: `data/parquet/{matches,deliveries}/season=YYYY/*.parquet`
- DuckDB snapshot: `data/ipl.duckdb` (or any path via `DUCKDB_PATH`)
- Views hide the storage source so SQL always targets `deliveries_q` / `matches_q`.

## Key features added recently
- Alias loading from Excel: teams, cities, venues + player nicknames.
- Canonical catalogs for exact matching (players/teams/cities/venues).
- Ambiguity detection with clarification prompt (UI + API).
- DEV mode in UI to show generated SQL.

## Uncommitted work present
- Modified: `backend/app.py`, `frontend/CoverDrive.js`, `requirements.txt`
- New (untracked): `backend/alias_loader.py`, `backend/entity_resolver.py`

## Important risk before committing
- `backend/alias_loader.py` imports `pandas`, but `requirements.txt` does not include it (currently commented out).
- This will break app startup in a clean environment unless `pandas` is added.

## Open questions
- Which data source is the default for production (Parquet vs DuckDB snapshot)?
- Do we want to ship ETL dependencies (pandas/pyarrow) in prod, or keep them separate?
- Should the “ask” entity resolver flow be used by the UI, or is `/nlq` the only public path?

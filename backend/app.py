from pathlib import Path
from typing import List, Any
import logging
import os
import re

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------- LLM -> SQL ----------------
# Use your working Gemini function (SDK-only) as-is
from scripts.test_gemini_sql import generate_sql

# ---------------- Paths ----------------
ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"

PARQUET_DIR = ROOT / "data" / "parquet"
DELIVERIES_DIR = PARQUET_DIR / "deliveries"
MATCHES_DIR = PARQUET_DIR / "matches"
DELIVERIES_GLOB = (DELIVERIES_DIR / "season=*" / "**" / "*.parquet").as_posix()
MATCHES_GLOB    = (MATCHES_DIR    / "season=*" / "**" / "*.parquet").as_posix()

# Optional path to a DuckDB snapshot file (e.g., data/ipl.duckdb)
DUCKDB_PATH = os.getenv("DUCKDB_PATH")

# ---------------- DuckDB ----------------
con = duckdb.connect(DUCKDB_PATH) if DUCKDB_PATH else duckdb.connect()
# If you want explicit threads, set an INT (some builds reject 'auto')
threads = max(1, (os.cpu_count() or 4))
con.execute(f"PRAGMA threads={threads}")

def _has_any_parquet(base_dir: Path) -> bool:
    try:
        return any(base_dir.rglob("*.parquet"))
    except Exception:
        return False


def init_views() -> None:
    """
    Create views and expose typed helpers the LLM can safely use.
    """
    # deliveries_src / matches_src: prefer snapshot tables if available
    used_snapshot_sources = False
    if DUCKDB_PATH:
        try:
            con.execute("CREATE OR REPLACE VIEW deliveries_src AS SELECT * FROM deliveries")
            con.execute("CREATE OR REPLACE VIEW matches_src AS SELECT * FROM matches")
            used_snapshot_sources = True
        except Exception:
            used_snapshot_sources = False

    if not used_snapshot_sources:
        # Source view: deliveries_src (may be empty on fresh deploys)
        if _has_any_parquet(DELIVERIES_DIR):
            con.execute(f"""
                CREATE OR REPLACE VIEW deliveries_src AS
                SELECT * FROM read_parquet('{DELIVERIES_GLOB}');
            """)
        else:
            # Create an empty schema-compatible placeholder
            con.execute(
                """
                CREATE OR REPLACE VIEW deliveries_src AS
                SELECT
                    CAST(NULL AS VARCHAR) AS match_id,
                    CAST(NULL AS VARCHAR) AS season,
                    CAST(NULL AS INTEGER) AS inning,
                    CAST(NULL AS INTEGER) AS over,
                    CAST(NULL AS INTEGER) AS ball_in_over,
                    CAST(NULL AS VARCHAR) AS batting_team,
                    CAST(NULL AS VARCHAR) AS batter,
                    CAST(NULL AS VARCHAR) AS non_striker,
                    CAST(NULL AS VARCHAR) AS bowler,
                    CAST(NULL AS INTEGER) AS runs_batter,
                    CAST(NULL AS INTEGER) AS runs_total,
                    CAST(NULL AS INTEGER) AS extras_total,
                    CAST(NULL AS INTEGER) AS wides,
                    CAST(NULL AS INTEGER) AS noballs,
                    CAST(NULL AS INTEGER) AS legbyes,
                    CAST(NULL AS INTEGER) AS byes,
                    CAST(NULL AS INTEGER) AS penalty,
                    CAST(NULL AS VARCHAR) AS dismissal_kind,
                    CAST(NULL AS VARCHAR) AS player_out,
                    CAST(NULL AS VARCHAR) AS fielder,
                    CAST(NULL AS VARCHAR) AS venue,
                    CAST(NULL AS VARCHAR) AS city,
                    CAST(NULL AS VARCHAR) AS date
                WHERE 1=0;
                """
            )

    # Public view adds typed helper column
    con.execute(
        """
        CREATE OR REPLACE VIEW deliveries AS
        SELECT
            d.*,
            COALESCE(
                TRY_CAST(d.season AS INTEGER),
                TRY_CAST(NULLIF(regexp_extract(CAST(d.season AS VARCHAR), '(20[0-3][0-9])', 1), '') AS INTEGER)
            ) AS season_year
        FROM deliveries_src d;
        """
    )

    if not used_snapshot_sources:
        # matches: typed timestamp/date + year to guide the LLM
        # Source view: matches_src (may be empty on fresh deploys)
        if _has_any_parquet(MATCHES_DIR):
            con.execute(f"""
                CREATE OR REPLACE VIEW matches_src AS
                SELECT * FROM read_parquet('{MATCHES_GLOB}');
            """)
        else:
            con.execute(
                """
                CREATE OR REPLACE VIEW matches_src AS
                SELECT
                    CAST(NULL AS VARCHAR) AS match_id,
                    CAST(NULL AS VARCHAR) AS season,
                    CAST(NULL AS VARCHAR) AS date,
                    CAST(NULL AS VARCHAR) AS competition,
                    CAST(NULL AS VARCHAR) AS venue,
                    CAST(NULL AS VARCHAR) AS city,
                    CAST(NULL AS VARCHAR) AS team1,
                    CAST(NULL AS VARCHAR) AS team2,
                    CAST(NULL AS VARCHAR) AS winner,
                    CAST(NULL AS VARCHAR) AS toss_winner,
                    CAST(NULL AS VARCHAR) AS toss_decision
                WHERE 1=0;
                """
            )

    # Public view adds typed helper columns
    con.execute(
        """
        CREATE OR REPLACE VIEW matches AS
        SELECT
            m.*,
            COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE))                                        AS match_ts,
            CAST(COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE)) AS DATE)                           AS match_date,
            CAST(EXTRACT(YEAR FROM COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE))) AS INTEGER)     AS match_year
        FROM matches_src m;
        """
    )

# ---------------- SQL extraction & normalization ----------------
SQL_BLOCK_RE = re.compile(
    r"(?:```sql\s*)(?P<sql>.*?)(?:```)|"        # ```sql ... ```
    r"(?:```\s*)(?P<sql2>.*?)(?:```)|"          # ``` ... ```
    r"(?P<inline>SELECT\b[\s\S]+)",             # first SELECT onward
    flags=re.IGNORECASE | re.DOTALL,
)

def extract_sql(text: str) -> str:
    """
    Pull a runnable SQL statement out of an LLM response.
    Priority: ```sql ...```, then ```...```, then first 'SELECT...'.
    Ensures the final statement starts with SELECT.
    """
    m = SQL_BLOCK_RE.search(text or "")
    if not m:
        raise ValueError("SQL not found in LLM output")

    sql = (m.group("sql") or m.group("sql2") or m.group("inline") or "").strip()

    # If multiple statements, keep the last non-empty segment
    if ";" in sql:
        parts = [p.strip() for p in sql.split(";") if p.strip()]
        sql = parts[-1] if parts else sql

    # Strip leading comments and verify it starts with SELECT
    sql_nocomments = re.sub(r"(?is)^\s*(--.*?$|/\*.*?\*/)+", "", sql).strip()
    if not re.match(r"(?is)^select\b", sql_nocomments):
        raise ValueError("Output does not start with SELECT or SQL not found.")
    return sql_nocomments

def safe_generate_sql(nlq: str) -> str:
    """
    Call your LLM function and extract a runnable SQL SELECT.
    """
    raw = generate_sql(nlq)
    return extract_sql(raw)

def normalize_sql(sql: str) -> str:
    """
    Make LLM SQL more DuckDB-friendly:
      - Convert strftime/date_part/YEAR/EXTRACT(YEAR ...) variants to EXTRACT(YEAR FROM SAFE(...))
      - SAFE(col) = COALESCE(try_cast(col AS TIMESTAMP), try_cast(col AS DATE))
      - Normalize season filters like season='IPL-2023' -> season=2023, and IN(...) lists
      - Convert EXTRACT(YEAR ... ) = 'YYYY' -> = YYYY (numeric)
    """
    s = sql

    def safe_date(expr: str) -> str:
        expr = expr.strip()
        return f"COALESCE(try_cast({expr} AS TIMESTAMP), try_cast({expr} AS DATE))"

    # ---- season normalizations (handles qualified names too) ----
    def extract_year(txt: str) -> str | None:
        m = re.search(r"(20[0-3][0-9])", txt or "")
        return m.group(1) if m else None

    # season = '...'
    def season_eq_fix(m: re.Match) -> str:
        qualifier = m.group(1) or ""         # e.g. 'matches.' or ''
        lit = m.group(2) if m.group(2) is not None else m.group(3)
        y = extract_year(lit)
        col = f"{qualifier}season".strip()
        if y:
            return f"{col} = {y}"
        if re.fullmatch(r"\d{4}", lit or ""):
            return f"{col} = {lit}"
        return m.group(0)

    s = re.sub(
        r"""(?ix)
            \b([a-z_][a-z0-9_]*\.)?season\s*=\s*
            (?:
              '([^']*)' | "([^"]*)"
            )
        """,
        season_eq_fix,
        s,
    )

    # season IN ('...','...')
    def season_in_fix(m: re.Match) -> str:
        qualifier = m.group(1) or ""
        inner = m.group(2)
        col = f"{qualifier}season".strip()
        items = [x.strip() for x in inner.split(",")]
        years: list[str] = []
        for it in items:
            v = it.strip().strip("'\"")
            y = extract_year(v)
            if y:
                years.append(y)
            elif re.fullmatch(r"\d{4}", v):
                years.append(v)
        return f"{col} IN ({', '.join(years)})" if years else m.group(0)

    s = re.sub(
        r"""(?ix)\b([a-z_][a-z0-9_]*\.)?season\s+IN\s*\(\s*([^)]+?)\s*\)""",
        season_in_fix,
        s,
    )

    # ---- year extraction → EXTRACT(YEAR FROM SAFE(...)) ----
    # YEAR(col) -> EXTRACT(YEAR FROM SAFE(col))
    s = re.sub(
        r"""(?i)\byear\s*\(\s*([^)]+?)\s*\)""",
        lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})",
        s,
    )
    # date_part('year', col) or date_part('y', col)
    s = re.sub(
        r"""(?i)date_part\(\s*['"]y(?:ear)?['"]\s*,\s*([^)]+?)\s*\)""",
        lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})",
        s,
    )
    # strftime('%Y', col)
    s = re.sub(
        r"""(?i)strftime\(\s*['"]%Y['"]\s*,\s*([^)]+?)\s*\)""",
        lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})",
        s,
    )
    # strftime(col, '%Y')
    s = re.sub(
        r"""(?i)strftime\(\s*([^)]+?)\s*,\s*['"]%Y['"]\s*\)""",
        lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})",
        s,
    )
    # wrap any existing EXTRACT(YEAR FROM x) to ensure SAFE(x)
    s = re.sub(
        r"""(?i)EXTRACT\s*\(\s*YEAR\s+FROM\s+([^)]+?)\s*\)""",
        lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})",
        s,
    )
    # EXTRACT(...)= 'YYYY'  -> numeric compare
    s = re.sub(
        r"""(?i)(EXTRACT\s*\(\s*YEAR\s+FROM\s+[^\)]+\))\s*=\s*'(\d{4})'""",
        lambda m: f"{m.group(1)} = {m.group(2)}",
        s,
    )

    # tiny cleanup: stray semicolons before LIMIT/OFFSET
    s = re.sub(r";\s*(limit|offset)\b", r" \1", s, flags=re.IGNORECASE)

    return s

# ---------------- FastAPI ----------------
app = FastAPI(title="CoverDrive")

# Serve /static/* from the frontend directory
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=False), name="static")

# Serve the main page at /
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")

# Optional favicon (avoids noisy 404s in logs)
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    path = FRONTEND_DIR / "favicon.ico"
    return FileResponse(path) if path.exists() else ("", 204)

@app.on_event("startup")
def _on_startup():
    logging.basicConfig(level=logging.INFO)
    # Fail fast if the frontend files aren’t where we expect
    assert (FRONTEND_DIR / "index.html").exists(), f"Missing {FRONTEND_DIR / 'index.html'}"
    init_views()

# ---------------- Models ----------------
class NLQRequest(BaseModel):
    question: str = Field(..., description="Natural language question")
    max_rows: int = Field(200, ge=1, le=5000)  # frontend doesn't send; default applies

class SQLResponse(BaseModel):
    sql: str
    columns: List[str]
    rows: List[List[Any]]

# ---------------- Routes ----------------
@app.post("/nlq", response_model=SQLResponse)
def nlq(req: NLQRequest):
    # 1) NL -> SQL (robust extraction)
    try:
        raw_sql = safe_generate_sql(req.question)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    # 2) Normalize
    sql = normalize_sql(raw_sql)

    # 3) Execute
    try:
        cur = con.execute(sql)
        rows = cur.fetchmany(req.max_rows)
        cols = [d[0] for d in cur.description] if cur.description else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query execution error: {e}")

    # 4) Return JSON (with the final, normalized SQL we ran)
    return SQLResponse(sql=sql, columns=cols, rows=[list(r) for r in rows])

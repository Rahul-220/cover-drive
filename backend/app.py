from pathlib import Path
from typing import List, Any, Optional, Union
import logging, os, re, time, uuid

import duckdb
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------- LLM -> SQL ----------------
from scripts.test_gemini_sql import (
    generate_sql, build_prompt, call_gemini, _extract_sdk, clean_and_validate_sql_from_text
)

# ---------------- Paths ----------------
ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
PARQUET_DIR = ROOT / "data" / "parquet"
DELIVERIES_DIR = PARQUET_DIR / "deliveries"
MATCHES_DIR = PARQUET_DIR / "matches"
DELIVERIES_GLOB = (DELIVERIES_DIR / "season=*" / "**" / "*.parquet").as_posix()
MATCHES_GLOB    = (MATCHES_DIR    / "season=*" / "**" / "*.parquet").as_posix()
DUCKDB_PATH = os.getenv("DUCKDB_PATH")

# ---------------- DuckDB ----------------
con = duckdb.connect(DUCKDB_PATH) if DUCKDB_PATH else duckdb.connect()
threads = max(1, (os.cpu_count() or 4))
con.execute(f"PRAGMA threads={threads}")

def _has_any_parquet(base_dir: Path) -> bool:
    try: return any(base_dir.rglob("*.parquet"))
    except Exception: return False

def init_views() -> None:
    used_snapshot_sources = False
    if DUCKDB_PATH:
        try:
            con.execute("CREATE OR REPLACE VIEW deliveries_src AS SELECT * FROM deliveries")
            con.execute("CREATE OR REPLACE VIEW matches_src AS SELECT * FROM matches")
            used_snapshot_sources = True
        except Exception:
            used_snapshot_sources = False

    if not used_snapshot_sources:
        if _has_any_parquet(DELIVERIES_DIR):
            con.execute(f"CREATE OR REPLACE VIEW deliveries_src AS SELECT * FROM read_parquet('{DELIVERIES_GLOB}');")
        else:
            con.execute("""
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
            """)

    deliveries_view_sql = """
        CREATE OR REPLACE VIEW {name} AS
        SELECT
            d.*,
            COALESCE(
                TRY_CAST(d.season AS INTEGER),
                TRY_CAST(NULLIF(regexp_extract(CAST(d.season AS VARCHAR), '(20[0-3][0-9])', 1), '') AS INTEGER)
            ) AS season_year
        FROM deliveries_src d;
    """
    try:
        con.execute(deliveries_view_sql.format(name="deliveries"))
    except Exception as e:
        logging.info("deliveries table exists; creating deliveries_typed instead: %s", e)
        con.execute(deliveries_view_sql.format(name="deliveries_typed"))

    con.execute("""
        CREATE OR REPLACE VIEW deliveries_q AS
        SELECT
            d.*,
            COALESCE(
                TRY_CAST(d.season AS INTEGER),
                TRY_CAST(NULLIF(regexp_extract(CAST(d.season AS VARCHAR), '(20[0-3][0-9])', 1), '') AS INTEGER)
            ) AS season_year
        FROM deliveries_src d;
    """)

    if not used_snapshot_sources:
        if _has_any_parquet(MATCHES_DIR):
            con.execute(f"CREATE OR REPLACE VIEW matches_src AS SELECT * FROM read_parquet('{MATCHES_GLOB}');")
        else:
            con.execute("""
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
            """)

    matches_view_sql = """
        CREATE OR REPLACE VIEW {name} AS
        SELECT
            m.*,
            COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE))                                        AS match_ts,
            CAST(COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE)) AS DATE)                           AS match_date,
            CAST(EXTRACT(YEAR FROM COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE))) AS INTEGER)     AS match_year
        FROM matches_src m;
    """
    try:
        con.execute(matches_view_sql.format(name="matches"))
    except Exception as e:
        logging.info("matches table exists; creating matches_typed instead: %s", e)
        con.execute(matches_view_sql.format(name="matches_typed"))

    con.execute("""
        CREATE OR REPLACE VIEW matches_q AS
        SELECT
            m.*,
            COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE))                                        AS match_ts,
            CAST(COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE)) AS DATE)                           AS match_date,
            CAST(EXTRACT(YEAR FROM COALESCE(TRY_CAST(m.date AS TIMESTAMP), TRY_CAST(m.date AS DATE))) AS INTEGER)     AS match_year
        FROM matches_src m;
    """)

# ---------------- SQL extraction & normalization ----------------
SQL_BLOCK_RE = re.compile(
    r"(?:```sql\s*)(?P<sql>.*?)(?:```)|"
    r"(?:```\s*)(?P<sql2>.*?)(?:```)|"
    r"(?P<inline>(?:WITH|SELECT)\b[\s\S]+)",
    flags=re.IGNORECASE | re.DOTALL,
)

def extract_sql(text: str) -> str:
    m = SQL_BLOCK_RE.search(text or "")
    if not m: raise ValueError("SQL not found in LLM output")
    sql = (m.group("sql") or m.group("sql2") or m.group("inline") or "").strip()
    if ";" in sql:
        parts = [p.strip() for p in sql.split(";") if p.strip()]
        sql = parts[-1] if parts else sql
    sql_nocomments = re.sub(r"(?is)^\s*(--.*?$|/\*.*?\*/)+", "", sql).strip()
    if not re.match(r"(?is)^(select|with)\b", sql_nocomments):
        raise ValueError("Output does not start with SELECT or SQL not found.")
    return sql_nocomments

def safe_generate_sql(nlq: str) -> str:
    try:
        raw = generate_sql(nlq); return extract_sql(raw)
    except Exception:
        text = _extract_sdk(call_gemini(build_prompt(nlq)))
        try: return clean_and_validate_sql_from_text(text)
        except Exception: return extract_sql(text)

def normalize_sql(sql: str) -> str:
    s = sql
    def safe_date(expr: str) -> str:
        expr = expr.strip()
        return f"COALESCE(try_cast({expr} AS TIMESTAMP), try_cast({expr} AS DATE))"
    def extract_year(txt: str) -> Optional[str]:
        m = re.search(r"(20[0-3][0-9])", txt or "")
        return m.group(1) if m else None
    def season_eq_fix(m: re.Match) -> str:
        qualifier = m.group(1) or ""
        lit = m.group(2) if m.group(2) is not None else m.group(3)
        y = extract_year(lit); col = f"{qualifier}season".strip()
        if y: return f"{col} = {y}"
        if re.fullmatch(r"\d{4}", lit or ""): return f"{col} = {lit}"
        return m.group(0)
    s = re.sub(r"""(?ix)\b([a-z_][a-z0-9_]*\.)?season\s*=\s*(?:'([^']*)'|"([^"]*)")""", season_eq_fix, s)
    def season_in_fix(m: re.Match) -> str:
        qualifier = m.group(1) or ""; inner = m.group(2); col = f"{qualifier}season".strip()
        items = [x.strip() for x in inner.split(",")]; years: List[str] = []
        for it in items:
            v = it.strip().strip("'\""); y = extract_year(v)
            if y: years.append(y)
            elif re.fullmatch(r"\d{4}", v): years.append(v)
        return f"{col} IN ({', '.join(years)})" if years else m.group(0)
    s = re.sub(r"""(?ix)\b([a-z_][a-z0-9_]*\.)?season\s+IN\s*\(\s*([^)]+?)\s*\)""", season_in_fix, s)
    s = re.sub(r"""(?i)\byear\s*\(\s*([^)]+?)\s*\)""", lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})", s)
    s = re.sub(r"""(?i)date_part\(\s*['"]y(?:ear)?['"]\s*,\s*([^)]+?)\s*\)""", lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})", s)
    s = re.sub(r"""(?i)strftime\(\s*['"]%Y['"]\s*,\s*([^)]+?)\s*\)""", lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})", s)
    s = re.sub(r"""(?i)strftime\(\s*([^)]+?)\s*,\s*['"]%Y['"]\s*\)""", lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})", s)
    s = re.sub(r"""(?i)EXTRACT\s*\(\s*YEAR\s+FROM\s+([^)]+?)\s*\)""", lambda m: f"EXTRACT(YEAR FROM {safe_date(m.group(1))})", s)
    s = re.sub(r"""(?i)(EXTRACT\s*\(\s*YEAR\s+FROM\s+[^\)]+\))\s*=\s*'(\d{4})'""", lambda m: f"{m.group(1)} = {m.group(2)}", s)
    s = re.sub(r";\s*(limit|offset)\b", r" \1", s, flags=re.IGNORECASE)
    s = re.sub(r"(?i)\b(FROM|JOIN)\s+([a-z_][a-z0-9_\.]*?)\s+AS(\s+)(?=(WHERE|ON|JOIN|GROUP|ORDER|LIMIT|OFFSET|$))", r"\1 \2 ", s)
    return s

# ---------------- FastAPI ----------------
app = FastAPI(title="CoverDrive")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=False), name="static")

@app.get("/healthz", include_in_schema=False)
def healthz(): return {"status": "ok"}

@app.get("/", include_in_schema=False)
def index(): return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store"})

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    path = FRONTEND_DIR / "favicon.ico"
    return FileResponse(path) if path.exists() else ("", 204)

@app.on_event("startup")
def _on_startup():
    logging.basicConfig(level=logging.INFO)
    assert (FRONTEND_DIR / "index.html").exists(), f"Missing {FRONTEND_DIR / 'index.html'}"
    init_views()

# ---------------- Models ----------------
class NLQRequest(BaseModel):
    question: str = Field(..., description="Natural language question")
    max_rows: int = Field(200, ge=1, le=5000)

class NLQSuccess(BaseModel):
    status: str = "ok"
    columns: List[str]
    rows: List[List[Any]]
    meta: dict = Field(default_factory=dict)
    sql: Optional[str] = None           # only when X-Debug: 1
    notice: Optional[str] = None        # human-friendly info (e.g., no results)

class NLQError(BaseModel):
    status: str = "error"
    code: str
    message: str
    correlation_id: Optional[str] = None

# ---------------- Routes ----------------
@app.post("/nlq", response_model=Union[NLQSuccess, NLQError])
def nlq(req: NLQRequest, request: Request):
    start = time.time()
    corr_id = str(uuid.uuid4())
    debug = request.headers.get("X-Debug", "0") in ("1", "true", "True")

    # 1) NL -> SQL
    try:
        raw_sql = safe_generate_sql(req.question)
    except Exception:
        return NLQError(
            code="NLQ_UNSUPPORTED",
            message="We’re working on this type of query. Try rephrasing or narrowing it.",
            correlation_id=corr_id,
        )

    # 2) Normalize
    sql = normalize_sql(raw_sql)

    # 3) Execute
    try:
        cur = con.execute(sql)
        rows = cur.fetchmany(req.max_rows)
        cols = [d[0] for d in cur.description] if cur.description else []
    except Exception as e:
        msg = str(e)
        code = "SQL_INVALID"
        if "Binder Error" in msg or "Catalog" in msg:
            code = "SCHEMA_MISMATCH"
        elif "Interrupted" in msg or "timeout" in msg.lower():
            code = "QUERY_TIMEOUT"
        return NLQError(
            code=code,
            message="We’re working on this type of query. Try rephrasing or narrowing it.",
            correlation_id=corr_id,
        )

    latency = int((time.time() - start) * 1000)
    payload = NLQSuccess(
        columns=cols,
        rows=[list(r) for r in rows],
        meta={"t_latency_ms": latency, "correlation_id": corr_id, "no_results": len(rows) == 0},
    )
    if len(rows) == 0:
        payload.notice = "No results found for that query. Try broadening the season, team, or player spelling."
    if debug:
        payload.sql = sql
    return payload

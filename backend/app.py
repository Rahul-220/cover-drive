from pathlib import Path
from typing import List, Any, Optional, Union, Tuple, Literal, Dict
import logging, os, re, time, uuid, json
import boto3
from urllib.parse import urlparse

import duckdb
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.alias_loader import load_aliases_from_excel  # your loader module
from backend.entity_resolver import resolve_entities 
from rapidfuzz import process, fuzz

from mangum import Mangum

# ---------------- LLM -> SQL ----------------
from scripts.test_gemini_sql import (
    generate_sql_or_clarify, generate_sql, build_prompt, call_gemini, _extract_sdk, clean_and_validate_sql_from_text
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

TMP_DUCKDB_LOCAL = "/tmp/ipl.duckdb"
def download_if_s3(path: str) -> str:
    if not path.lower().startswith("s3://"): 
        return path
    parsed = urlparse(path)
    bucket, key = parsed.netloc, parsed.path.lstrip("/")
    s3 = boto3.client("s3")
    s3.download_file(bucket, key, TMP_DUCKDB_LOCAL)
    return TMP_DUCKDB_LOCAL

# ---------------- DuckDB ----------------
resolved_path = download_if_s3(DUCKDB_PATH) if DUCKDB_PATH else None
con = duckdb.connect(resolved_path) if resolved_path else duckdb.connect()
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

# catalogs:
def init_catalogs() -> None:
    """
    Create small catalogs (players/teams/cities/venues/seasons/constants) with strong normalization.
    """
    # Shared normalization macro
    con.execute("""
        CREATE OR REPLACE MACRO normc(s) AS
          lower(
            regexp_replace(
              regexp_replace(coalesce(cast(s as varchar), ''), '[^a-z0-9 ]', '', 'g'),
              '\\s+', ' ', 'g'
            )
          );
    """)

    # Players (all roles)
    con.execute("""
        CREATE OR REPLACE VIEW players_catalog AS
        WITH all_names AS (
            SELECT batter AS name FROM deliveries_q WHERE batter IS NOT NULL
            UNION ALL SELECT bowler FROM deliveries_q WHERE bowler IS NOT NULL
            UNION ALL SELECT player_out FROM deliveries_q WHERE player_out IS NOT NULL
            UNION ALL SELECT fielder FROM deliveries_q WHERE fielder IS NOT NULL
        )
        SELECT
            name,
            normc(name) AS norm,
            COUNT(*)    AS hits
        FROM all_names
        GROUP BY 1,2
        ORDER BY hits DESC;
    """)

    # Teams
    con.execute("""
        CREATE OR REPLACE VIEW teams_catalog AS
        WITH t AS (
            SELECT team1 AS team FROM matches_q WHERE team1 IS NOT NULL
            UNION ALL SELECT team2 FROM matches_q WHERE team2 IS NOT NULL
        )
        SELECT
            team,
            normc(team) AS norm,
            COUNT(*)    AS games
        FROM t
        GROUP BY 1,2
        ORDER BY games DESC;
    """)

    # Cities
    con.execute("""
        CREATE OR REPLACE VIEW cities_catalog AS
        SELECT
            city,
            normc(city) AS norm,
            COUNT(*)    AS games
        FROM matches_q
        WHERE city IS NOT NULL
        GROUP BY 1,2
        ORDER BY games DESC;
    """)

    # Venues
    con.execute("""
        CREATE OR REPLACE VIEW venues_catalog AS
        SELECT
            venue,
            normc(venue) AS norm,
            COUNT(*)     AS games
        FROM matches_q
        WHERE venue IS NOT NULL
        GROUP BY 1,2
        ORDER BY games DESC;
    """)

    # Seasons & constants
    con.execute("""
        CREATE OR REPLACE VIEW seasons_catalog AS
        SELECT DISTINCT match_year AS season
        FROM matches_q
        WHERE match_year IS NOT NULL
        ORDER BY season;
    """)
    con.execute("""
        CREATE OR REPLACE VIEW constants AS
        SELECT (SELECT max(match_year) FROM matches_q) AS latest_season;
    """)

    logging.info("Catalog views ready (strong normalization)")


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

# new code

def _extract_year_from_text(txt: str) -> Optional[int]:
    m = re.search(r"(20[0-3][0-9])", txt or "")
    return int(m.group(1)) if m else None


def _candidate_list(entity: str, mention: str, season: Optional[int], limit: int = 8) -> List[str]:
    term = (mention or "").strip()
    if not term:
        return []
    like = f"%{term.lower()}%"
    try:
        if entity == "player":
            if season:
                rows = con.execute(
                    """
                    WITH all_names AS (
                      SELECT batter AS name, season_year AS yr FROM deliveries_q WHERE batter IS NOT NULL
                      UNION ALL SELECT bowler AS name, season_year AS yr FROM deliveries_q WHERE bowler IS NOT NULL
                      UNION ALL SELECT player_out AS name, season_year AS yr FROM deliveries_q WHERE player_out IS NOT NULL
                      UNION ALL SELECT fielder AS name, season_year AS yr FROM deliveries_q WHERE fielder IS NOT NULL
                    )
                    SELECT name, COUNT(*) AS hits
                    FROM all_names
                    WHERE yr = ? AND lower(name) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [season, like, limit],
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    WITH all_names AS (
                      SELECT batter AS name FROM deliveries_q WHERE batter IS NOT NULL
                      UNION ALL SELECT bowler AS name FROM deliveries_q WHERE bowler IS NOT NULL
                      UNION ALL SELECT player_out AS name FROM deliveries_q WHERE player_out IS NOT NULL
                      UNION ALL SELECT fielder AS name FROM deliveries_q WHERE fielder IS NOT NULL
                    )
                    SELECT name, COUNT(*) AS hits
                    FROM all_names
                    WHERE lower(name) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [like, limit],
                ).fetchall()
        elif entity == "team":
            if season:
                rows = con.execute(
                    """
                    WITH t AS (
                      SELECT team1 AS name, match_year AS yr FROM matches_q WHERE team1 IS NOT NULL
                      UNION ALL SELECT team2 AS name, match_year AS yr FROM matches_q WHERE team2 IS NOT NULL
                    )
                    SELECT name, COUNT(*) AS hits
                    FROM t
                    WHERE yr = ? AND lower(name) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [season, like, limit],
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    WITH t AS (
                      SELECT team1 AS name FROM matches_q WHERE team1 IS NOT NULL
                      UNION ALL SELECT team2 AS name FROM matches_q WHERE team2 IS NOT NULL
                    )
                    SELECT name, COUNT(*) AS hits
                    FROM t
                    WHERE lower(name) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [like, limit],
                ).fetchall()
        elif entity == "city":
            if season:
                rows = con.execute(
                    """
                    SELECT city AS name, COUNT(*) AS hits
                    FROM matches_q
                    WHERE city IS NOT NULL AND match_year = ? AND lower(city) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [season, like, limit],
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT city AS name, COUNT(*) AS hits
                    FROM matches_q
                    WHERE city IS NOT NULL AND lower(city) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [like, limit],
                ).fetchall()
        elif entity == "venue":
            if season:
                rows = con.execute(
                    """
                    SELECT venue AS name, COUNT(*) AS hits
                    FROM matches_q
                    WHERE venue IS NOT NULL AND match_year = ? AND lower(venue) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [season, like, limit],
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT venue AS name, COUNT(*) AS hits
                    FROM matches_q
                    WHERE venue IS NOT NULL AND lower(venue) LIKE ?
                    GROUP BY name
                    ORDER BY hits DESC, name
                    LIMIT ?
                    """,
                    [like, limit],
                ).fetchall()
        else:
            return []
    except Exception:
        return []

    return [r[0] for r in rows if r and r[0]]

# ---------- Dataset lists (cached) ----------
# _venue_cache: dict[tuple[Optional[int]], List[str]] = {}
# _city_cache: dict[tuple[Optional[int]], List[str]] = {}
# _team_cache: dict[tuple[Optional[int]], List[str]] = {}

_NORM_NONALNUM = re.compile(r"[^a-z0-9 ]")

# def _normc_py(s: Optional[str]) -> str:
#     s = (s or "").lower()
#     s = _NORM_NONALNUM.sub("", s)
#     return " ".join(s.split())

# exact helpers
def _catalog_exact_player(name: str) -> Optional[str]:
    try:
        row = con.execute("SELECT name FROM players_catalog WHERE norm = normc(?) LIMIT 1", [name]).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _catalog_exact_team(name: str) -> Optional[str]:
    try:
        row = con.execute("SELECT team FROM teams_catalog WHERE norm = normc(?) LIMIT 1", [name]).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _catalog_exact_city(name: str) -> Optional[str]:
    try:
        row = con.execute("SELECT city FROM cities_catalog WHERE norm = normc(?) LIMIT 1", [name]).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _catalog_exact_venue(name: str) -> Optional[str]:
    try:
        row = con.execute("SELECT venue FROM venues_catalog WHERE norm = normc(?) LIMIT 1", [name]).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    
# new helpers
# ---------- Candidate collectors (lists) ----------

def _surname_of(full_name: str) -> str:
    parts = [p for p in (full_name or "").split() if p]
    return parts[-1] if parts else ""

def player_candidates_from_phrase(phrase: str, year: Optional[int], limit: int = 5) -> List[dict]:
    phrase = _normalize_ws_case(phrase or "")
    out: List[dict] = []

    # 1) catalog exact full name
    hit = _catalog_exact_player(phrase)
    if hit:
        out.append({"value": hit, "display": hit, "source": "catalog_exact", "rank": 1})

    # 2) excel alias exact
    if not out:
        hit = _player_exact_from_alias(phrase)
        if hit:
            out.append({"value": hit, "display": hit, "source": "alias_exact", "rank": 1})

    # 3) initials+surname (e.g., "KL Rahul", "V Kohli")
    if not out:
        parts = phrase.split()
        if len(parts) == 2 and parts[0].isalpha() and 1 <= len(parts[0]) <= 3:
            try:
                rows = con.execute("""
                    WITH derived AS (
                      SELECT
                        name AS full_name,
                        CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',1) ELSE name END AS first_name,
                        CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',-1) ELSE name END AS last_name
                      FROM players_catalog
                    )
                    SELECT full_name
                    FROM derived
                    WHERE lower(last_name) = lower(?)
                      AND ( lower(first_name) = lower(?) OR substr(lower(first_name),1,1) = lower(substr(?,1,1)) )
                """, [parts[1], parts[0], parts[0]]).fetchall()
                for i, r in enumerate(rows[:limit], start=1):
                    out.append({"value": r[0], "display": r[0], "source": "initials_surname", "rank": i})
            except Exception:
                pass

    # 4) surname-only: collect all players with same last name (e.g., "Pandya")
    if not out and phrase and " " not in phrase:
        try:
            rows = con.execute("""
                WITH derived AS (
                  SELECT
                    name AS full_name,
                    CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',-1) ELSE name END AS last_name
                  FROM players_catalog
                )
                SELECT DISTINCT full_name
                FROM derived
                WHERE lower(last_name) = lower(?)
                ORDER BY full_name
                LIMIT ?
            """, [phrase, limit]).fetchall()
            for i, r in enumerate(rows, start=1):
                out.append({"value": r[0], "display": r[0], "source": "surname", "rank": i})
        except Exception:
            pass

    return out[:limit]


def player_candidates_from_token(token: str, year: Optional[int], limit: int = 5) -> List[dict]:
    token = _normalize_ws_case(token or "")
    out: List[dict] = []

    # 1) catalog exact full name
    hit = _catalog_exact_player(token)
    if hit:
        return [{"value": hit, "display": hit, "source": "catalog_exact", "rank": 1}]

    # 2) excel alias exact
    hit = _player_exact_from_alias(token)
    if hit:
        return [{"value": hit, "display": hit, "source": "alias_exact", "rank": 1}]

    # 3) initials-only (<=3 letters) -> all matching initials
    if token.isalpha() and 1 <= len(token) <= 3:
        try:
            rows = con.execute("SELECT DISTINCT name FROM players_catalog").fetchall()
            names = [r[0] for r in rows if r and r[0]]
            initials = token.upper()
            cands = [n for n in names if _initials_of(n) == initials]
            for i, n in enumerate(cands[:limit], start=1):
                out.append({"value": n, "display": n, "source": "initials_only", "rank": i})
        except Exception:
            pass

    # 4) surname-only collector (same as phrase when single token)
    if not out and token and " " not in token:
        try:
            rows = con.execute("""
                WITH derived AS (
                  SELECT
                    name AS full_name,
                    CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',-1) ELSE name END AS last_name
                  FROM players_catalog
                )
                SELECT DISTINCT full_name
                FROM derived
                WHERE lower(last_name) = lower(?)
                ORDER BY full_name
                LIMIT ?
            """, [token, limit]).fetchall()
            for i, r in enumerate(rows, start=1):
                out.append({"value": r[0], "display": r[0], "source": "surname", "rank": i})
        except Exception:
            pass

    return out[:limit]


def team_candidates_from_phrase(phrase: str, year: Optional[int], limit: int = 5) -> List[dict]:
    phrase = _normalize_ws_case(phrase or "")
    out: List[dict] = []

    # Excel exact alias
    row = con.execute("""
        SELECT canonical_label
        FROM alias_teams
        WHERE lower(alias_label)=lower(?)
          AND ( ? IS NULL OR (COALESCE(valid_from, ?) <= ? AND COALESCE(valid_to, ?) >= ?) )
        LIMIT 1
    """, [phrase, year, year, year, year, year]).fetchone()
    if row:
        out.append({"value": row[0], "display": row[0], "source": "alias_exact", "rank": 1})

    # Catalog matches (exact / prefix / contains)
    if len(out) < limit:
        rows = con.execute("""
            SELECT team
            FROM teams_catalog
            ORDER BY
              (norm = normc(?)) DESC,
              (norm LIKE normc(?) || '%') DESC,
              (instr(norm, normc(?)) > 0) DESC,
              games DESC,
              length(team) ASC
            LIMIT ?
        """, [phrase, phrase, phrase, limit]).fetchall()
        for r in rows:
            if not any(c["value"] == r[0] for c in out):
                out.append({"value": r[0], "display": r[0], "source": "catalog", "rank": len(out)+1})
    return out[:limit]


def venue_candidates_from_phrase(phrase: str, year: Optional[int], limit: int = 5) -> List[dict]:
    phrase = _normalize_ws_case(phrase or "")
    out: List[dict] = []

    # Excel exact alias (snap to dataset form)
    row = con.execute("""
        SELECT canonical_label
        FROM alias_venues
        WHERE lower(alias_label)=lower(?)
          AND ( ? IS NULL OR (COALESCE(valid_from, ?) <= ? AND COALESCE(valid_to, ?) >= ?) )
        LIMIT 1
    """, [phrase, year, year, year, year, year]).fetchone()
    if row:
        snapped = _snap_venue_to_dataset(row[0], year) or row[0]
        out.append({"value": snapped, "display": snapped, "source": "alias_exact", "rank": 1})

    # Catalog matches (exact / prefix / contains)
    if len(out) < limit:
        rows = con.execute("""
            SELECT venue
            FROM venues_catalog
            ORDER BY
              (norm = normc(?)) DESC,
              (norm LIKE normc(?) || '%') DESC,
              (instr(norm, normc(?)) > 0) DESC,
              games DESC,
              length(venue) ASC
            LIMIT ?
        """, [phrase, phrase, phrase, limit]).fetchall()
        for r in rows:
            if not any(c["value"] == r[0] for c in out):
                out.append({"value": r[0], "display": r[0], "source": "catalog", "rank": len(out)+1})
    return out[:limit]


def city_candidates_from_phrase(phrase: str, year: Optional[int], limit: int = 5) -> List[dict]:
    phrase = _normalize_ws_case(phrase or "")
    rows = con.execute("""
        SELECT city
        FROM cities_catalog
        ORDER BY
          (norm = normc(?)) DESC,
          (norm LIKE normc(?) || '%') DESC,
          (instr(norm, normc(?)) > 0) DESC,
          games DESC,
          length(city) ASC
        LIMIT ?
    """, [phrase, phrase, phrase, limit]).fetchall()
    return [{"value": r[0], "display": r[0], "source": "catalog", "rank": i+1} for i, r in enumerate(rows)]

# ---------- Ambiguity helpers ----------

def needs_clarification(cands: List[dict], kind: str, raw: str) -> bool:
    if len(cands) < 2:
        return False
    raw_clean = (raw or "").strip()
    if kind == "player":
        # If the raw looks like a surname-only OR initials-only, and multiple players match â†’ clarify
        if " " not in raw_clean:
            return True
        parts = raw_clean.split()
        if len(parts) == 2 and parts[0].isalpha() and 1 <= len(parts[0]) <= 3:
            # initials+surname form but multiple candidates â†’ clarify
            return True
        # fallback: multiple near-equal candidates
        return True
    else:
        # For team/venue/city: only clarify if we didn't get an exact catalog/alias match and have >1 plausible
        exact_present = any(c.get("source") in ("catalog_exact", "alias_exact") and c["rank"] == 1 for c in cands)
        return (not exact_present) and len(cands) >= 2

def make_clarify_response(original_q: str, kind: str, raw: str, cands: List[dict]) -> dict:
    nice = {
        "player": f"Which {raw} did you mean?",
        "team":   f"Which team did you mean?",
        "venue":  f"Which venue did you mean?",
        "city":   f"Which city did you mean?",
    }.get(kind, "Please choose one")
    return {
        "status": "clarify",
        "dimension": kind,
        "question": nice,
        "options": [{"label": c["display"], "value": c["value"]} for c in cands[:5]],
        "original_question": original_q,
        "phrase": raw,
    }

# ---------- Wrappers that can yield a clarify payload ----------

def resolve_phrase_with_candidates(kind: Literal["player","team","venue","city"], phrase: str, year: Optional[int], original_q: str) -> Tuple[Optional[str], Optional[dict]]:
    if not phrase:
        return None, None
    if kind == "player":
        cands = player_candidates_from_phrase(phrase, year)
    elif kind == "team":
        cands = team_candidates_from_phrase(phrase, year)
    elif kind == "venue":
        cands = venue_candidates_from_phrase(phrase, year)
    else:
        cands = city_candidates_from_phrase(phrase, year)

    if not cands:
        return None, None
    if needs_clarification(cands, kind, phrase):
        return None, make_clarify_response(original_q, kind, phrase, cands)
    return cands[0]["value"], None


def resolve_token_with_candidates(kind: Literal["player","team","venue","city"], token: str, year: Optional[int], original_q: str) -> Tuple[Optional[str], Optional[dict]]:
    if not token:
        return None, None
    if kind == "player":
        cands = player_candidates_from_token(token, year)
    elif kind == "team":
        cands = team_candidates_from_phrase(token, year)  # token path reuses phrase logic for simplicity
    elif kind == "venue":
        cands = venue_candidates_from_phrase(token, year)
    else:
        cands = city_candidates_from_phrase(token, year)

    if not cands:
        return None, None
    if needs_clarification(cands, kind, token):
        return None, make_clarify_response(original_q, kind, token, cands)
    return cands[0]["value"], None


# excel helpers
def _player_exact_from_alias(token: str) -> Optional[str]:
    try:
        row = con.execute("SELECT full_name FROM player_alias WHERE lower(alias)=lower(?) LIMIT 1", [token]).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _initials_of(name: str) -> str:
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return ""
    # initials from first N-1 tokens; last token is often surname and not initial
    if len(parts) == 1:
        return parts[0][:1].upper()
    return "".join(p[0].upper() for p in parts[:-1])


def _normalize_ws_case(s: str) -> str:
    return " ".join((s or "").split())

def _resolve_player_phrase(phrase: str, year: Optional[int]) -> Optional[str]:
    if not phrase:
        return None
    phrase = _normalize_ws_case(phrase)

    # 1) catalog exact full name
    hit = _catalog_exact_player(phrase)
    if hit:
        logging.info("[resolve-player-phrase] catalog exact %r -> %r", phrase, hit)
        return hit

    # 2) excel exact alias
    hit = _player_exact_from_alias(phrase)
    if hit:
        logging.info("[resolve-player-phrase] excel alias %r -> %r", phrase, hit)
        return hit

    # 3) initials + surname (strict, from catalog-derived first/last)
    parts = phrase.split()
    if len(parts) == 2 and parts[0].isalpha() and 1 <= len(parts[0]) <= 3:
        try:
            row = con.execute("""
                WITH derived AS (
                  SELECT
                    name AS full_name,
                    CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',1) ELSE name END AS first_name,
                    CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',-1) ELSE name END AS last_name
                  FROM players_catalog
                )
                SELECT full_name
                FROM derived
                WHERE lower(last_name) = lower(?)
                  AND ( lower(first_name) = lower(?) OR substr(lower(first_name),1,1) = lower(substr(?,1,1)) )
                LIMIT 1
            """, [parts[1], parts[0], parts[0]]).fetchone()
            if row:
                logging.info("[resolve-player-phrase] initials+surname %r -> %r", phrase, row[0])
                return row[0]
        except Exception:
            pass

    logging.info("[resolve-player-phrase] no match %r", phrase)
    return None

def _resolve_player_token(token: str, year: Optional[int]) -> Optional[str]:
    if not token:
        return None

    # 1) catalog exact
    hit = _catalog_exact_player(token)
    if hit:
        logging.info("[resolve-player-token] catalog exact %r -> %r", token, hit)
        return hit

    # 2) excel alias exact
    hit = _player_exact_from_alias(token)
    if hit:
        logging.info("[resolve-player-token] excel alias %r -> %r", token, hit)
        return hit

    # 3) initials-only (<=3 letters) â†’ pick unique initials in catalog only; no fuzzy
    if token.isalpha() and 1 <= len(token) <= 3:
        try:
            rows = con.execute("SELECT DISTINCT name FROM players_catalog").fetchall()
            names = [r[0] for r in rows if r and r[0]]
            initials = token.upper()
            cands = [n for n in names if _initials_of(n) == initials]
            if len(cands) == 1:
                logging.info("[resolve-player-token] initials-only unique %r -> %r", token, cands[0])
                return cands[0]
        except Exception:
            pass

    logging.info("[resolve-player-token] no match %r", token)
    return None

def _extract_year_from_sql(sql: str) -> Optional[int]:
    """
    Try to infer a year from common patterns in the SQL the LLM emits.
    Looks for match_year = 20xx or IN (...) first; else returns None.
    """
    # match_year = 2023
    m = re.search(r"(?i)\bmatch_year\s*=\s*(20[0-3]\d)\b", sql or "")
    if m: 
        return int(m.group(1))
    # match_year IN (2022, 2023, ...)
    m = re.search(r"(?i)\bmatch_year\s+IN\s*\(([^)]+)\)", sql or "")
    if m:
        years = re.findall(r"(20[0-3]\d)", m.group(1))
        if years:
            return int(years[0])
    return None

# regexes for venue predicates
_VENUE_EQ_RE = re.compile(r"(?i)(\bvenue\s*=\s*)'([^']+)'")
_VENUE_IN_RE = re.compile(r"(?i)(\bvenue\s+IN\s*\()([^)]+)(\))")
_VENUE_ANY_RE = re.compile(r"(?i)\bvenue\s*(=|in\s*\()")
_CITY_EQ_AND_RE = re.compile(r"(?i)\s+AND\s+city\s*=\s*'[^']*'")
_CITY_IN_AND_RE = re.compile(r"(?i)\s+AND\s+city\s+IN\s*\([^)]+\)")
_CITY_EQ_START_RE = re.compile(r"(?i)\bWHERE\s+city\s*=\s*'[^']*'\s+AND\s+")
_CITY_IN_START_RE = re.compile(r"(?i)\bWHERE\s+city\s+IN\s*\([^)]+\)\s+AND\s+")

def _drop_city_when_venue_locked(sql: str) -> str:
    if not sql or not _VENUE_ANY_RE.search(sql):
        return sql  # only act when venue predicate present

    out = sql
    # remove "AND city = '...'"
    out = _CITY_EQ_AND_RE.sub("", out)
    # remove "AND city IN (...)"
    out = _CITY_IN_AND_RE.sub("", out)
    # handle if city-term is the first thing after WHERE: "WHERE city=... AND ..."
    out = _CITY_EQ_START_RE.sub("WHERE ", out)
    out = _CITY_IN_START_RE.sub("WHERE ", out)
    return out


def _snap_venue_literals_in_sql(sql: str, year_hint: Optional[int]) -> str:
    # Keep the same function, it will call the no-fuzzy _snap_venue_to_dataset()
    # so you donâ€™t need to change callers.
    if not sql:
        return sql
    year = year_hint or _extract_year_from_sql(sql)

    def _eq_sub(m: re.Match) -> str:
        prefix, lit = m.group(1), m.group(2)
        snapped = _snap_venue_to_dataset(lit, year) or lit
        return f"{prefix}'{snapped}'"

    def _in_sub(m: re.Match) -> str:
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        parts = re.findall(r"'([^']*)'", body)
        snapped_parts = [f"'{_snap_venue_to_dataset(p, year) or p}'" for p in parts]
        return f"{prefix}{', '.join(snapped_parts)}{suffix}"

    out = _VENUE_EQ_RE.sub(_eq_sub, sql)
    out2 = _VENUE_IN_RE.sub(_in_sub, out)
    if out2 != sql:
        logging.info("[alias] post-sql venue snap (no fuzzy). before=%r after=%r", sql, out2)
    return out2

def _snap_venue_to_dataset(name: str, year: Optional[int]) -> Optional[str]:
    # No fuzzy: just normalized equality against catalog
    # return _catalog_exact_venue(name)
    try:
        row = con.execute(
            """
            SELECT venue
            FROM venues_catalog
            ORDER BY
              -- exact normalized equality first
              (norm = normc(?)) DESC,
              -- 'Wankhede Stadium, Mumbai' starts with 'wankhede stadium'
              (norm LIKE normc(?) || '%') DESC,
              -- anywhere containment as a backstop
              (instr(norm, normc(?)) > 0) DESC,
              -- prefer more frequent venues if still tied
              games DESC,
              -- and shorter names
              length(venue) ASC
            LIMIT 1
            """,
            [name, name, name],
        ).fetchone()
        snapped = row[0] if row else None
        if snapped and snapped != name:
            logging.info("[alias] snap venue %r -> %r", name, snapped)
        return snapped
    except Exception as e:
        logging.warning("[alias] snap venue error for %r: %s", name, e)
        return None

def _resolve_team_alias(token: str, year: Optional[int]) -> Optional[str]:
    # Step 1: DuckDB catalog exact
    hit = _catalog_exact_team(token)
    if hit:
        logging.info("[resolve-team] catalog exact %r -> %r", token, hit)
        return hit

    # Step 2: Excel exact
    row = con.execute("""
        SELECT canonical_label
        FROM alias_teams
        WHERE lower(alias_label) = lower(?)
          AND ( ? IS NULL OR (COALESCE(valid_from, ?) <= ? AND COALESCE(valid_to, ?) >= ?) )
        LIMIT 1
    """, [token, year, year, year, year, year]).fetchone()
    if row:
        logging.info("[resolve-team] excel exact %r -> %r", token, row[0])
        return row[0]

    logging.info("[resolve-team] no match %r", token)
    return None


def _resolve_city_alias(token: str, year: Optional[int]) -> Optional[str]:
    hit = _catalog_exact_city(token)
    if hit:
        logging.info("[resolve-city] catalog exact %r -> %r", token, hit)
        return hit

    row = con.execute("""
        SELECT canonical_label
        FROM alias_cities
        WHERE lower(alias_label) = lower(?)
          AND ( ? IS NULL OR (COALESCE(valid_from, ?) <= ? AND COALESCE(valid_to, ?) >= ?) )
        LIMIT 1
    """, [token, year, year, year, year, year]).fetchone()
    if row:
        logging.info("[resolve-city] excel exact %r -> %r", token, row[0])
        return row[0]

    logging.info("[resolve-city] no match %r", token)
    return None


def _resolve_venue_alias(token: str, year: Optional[int]) -> Optional[str]:
    # Step 1: catalog exact; this returns dataset wording (e.g., "Wankhede Stadium, Mumbai")
    hit = _catalog_exact_venue(token)
    if hit:
        logging.info("[resolve-venue] catalog exact %r -> %r", token, hit)
        return hit

    # Step 2: Excel exact â†’ snap to dataset wording by catalog equality (no fuzzy)
    row = con.execute("""
        SELECT canonical_label
        FROM alias_venues
        WHERE lower(alias_label) = lower(?)
          AND ( ? IS NULL OR (COALESCE(valid_from, ?) <= ? AND COALESCE(valid_to, ?) >= ?) )
        LIMIT 1
    """, [token, year, year, year, year, year]).fetchone()
    if row:
        canon = row[0]
        # Try to snap by normalized equality to dataset
        snap = _catalog_exact_venue(canon)  # uses normc
        ret = snap or canon
        logging.info("[resolve-venue] excel exact %r -> %r (snapped=%r)", token, ret, bool(snap))
        return ret

    logging.info("[resolve-venue] no match %r", token)
    return None



# ---------- Context-aware question rewrite stays the same ----------
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\.\-']*")
_PREP_LOC_RE = re.compile(
    r"\b(?P<prefix>in|at)\s+(?P<phrase>[A-Za-z][A-Za-z\.\-']*(?:\s+[A-Za-z][A-Za-z\.\-']*){0,3})\b",
    flags=re.IGNORECASE,
)
_PREP_TEAM_RE = re.compile(
    r"\b(?P<prefix>vs\.?|versus|against)\s+(?P<phrase>[A-Za-z][A-Za-z\.\-']*(?:\s+[A-Za-z][A-Za-z\.\-']*){0,3})\b",
    flags=re.IGNORECASE,
)
# Phrases like: "for Royal Challengers Bangalore", "by Mumbai Indians", "of CSK"
_PREP_TEAM_FOR_RE = re.compile(
    r"\b(?P<prefix>for|by|of)\s+"
    r"(?P<phrase>"
        r"[A-Za-z][A-Za-z\.\-']*"                               # first token
        r"(?:\s+(?!in\b|at\b|on\b|with\b|against\b|vs\b|vs\.|\d)"  # block stopwords/years as next token
        r"[A-Za-z][A-Za-z\.\-']*"
        r"){0,3}"                                                # up to 4 tokens total
    r")",
    flags=re.IGNORECASE,
)

# helper temp:
def _debug_team_for_matches(text: str):
    for m in _PREP_TEAM_FOR_RE.finditer(text):
        logging.info(
            "[team_for:match] span=%s prefix=%r phrase_raw=%r tail=%r",
            m.span(),
            m.group("prefix"),
            m.group("phrase"),
            text[m.end():m.end()+12],  # shows what comes immediately after (e.g., " in 2022")
        )


# Common function words we should never resolve
_STOPWORDS = {
    "a","an","the","in","at","on","by","of","for","to","from","and","or","vs","vs.","versus","against",
    "with","without","between","over","under","than","as","is","are","was","were","be","been","being",
    "match","matches","run","runs","wicket","wickets","ball","balls","score","scored","scores","total",
    "how","many","who","played","play","won","wins","lost","losses","season","year",
    # cricket metrics / nouns that should NOT be entity-mapped
    "strike","rate","strikerate","sr","average","avg","economy","econ","ecorate","boundary","boundaries",
    "fours","sixes","hundreds","fifties","fifty","hundred","highest","lowest","most","least","top","best"
}

# Legit short team abbreviations to allow through even if short
_TEAM_ABBR = {
    "MI","RCB","CSK","DC","DD","KKR","SRH","GT","LSG","RR","PBKS","KXIP","GL","RPS"
}

def _trim_trailing_stopwords(phrase: str) -> str:
    """Remove trailing stopwords accidentally captured in a location phrase."""
    toks = phrase.split()
    while toks and toks[-1].lower() in _STOPWORDS:
        toks.pop()
    return " ".join(toks)

def _trim_trailing_noise(phrase: str) -> str:
    """Trim trailing stopwords and trailing years like 'in 2023' / '2023'."""
    # remove trailing "in 20xx"
    m = re.search(r"^(.*?)(?:\s+in\s+(20[0-3]\d))?$", phrase.strip(), flags=re.IGNORECASE)
    core = m.group(1).strip() if m else phrase.strip()
    # drop trailing stopwords
    toks = core.split()
    while toks and toks[-1].lower() in _STOPWORDS:
        toks.pop()
    # drop lone trailing year tokens
    while toks and re.fullmatch(r"20[0-3]\d", toks[-1]):
        toks.pop()
    return " ".join(toks)


# def _pre_resolve_text(question: str) -> str:
#     """
#     Context-aware alias replacement BEFORE LLM:
#       - 'at <PHRASE>'   -> resolve VENUE first (no city bias), trimming trailing stopwords
#       - 'in <PHRASE>'   -> resolve CITY first (then venue as fallback), trimming trailing stopwords
#       - 'vs/against â€¦'  -> resolve TEAM
#       - single-token pass: player -> city -> venue (only if none already fixed) -> team
#     Appends a hint if a venue was resolved: do NOT add city predicates.
#     """
#     try:
#         if not question:
#             return question

#         year = _extract_year_from_text(question)
#         out = question

#         resolved_venues: list[str] = []
#         steps: list[str] = []
#         replacements_log: list[tuple[str,str,str]] = []

#         logging.info("[alias] input='%s' year=%s", question, year)
#         logging.info("[alias] input=%r year=%s", question, year)
#         _debug_team_for_matches(out)

#         # --- 1) Location phrases (in/at) ---
#         def _loc_sub(m: re.Match) -> str:
#             prefix = m.group("prefix").lower()
#             phrase_raw = m.group("phrase")
#             phrase = _trim_trailing_stopwords(phrase_raw)  # <<< fix â€œChepauk inâ€ -> â€œChepaukâ€
#             canon: Optional[str] = None

#             steps.append(f"loc:{prefix} phrase='{phrase_raw}' -> trimmed='{phrase}'")

#             try:
#                 if prefix == "at":
#                     # Prefer venue for "at"
#                     canon = _resolve_venue_alias(phrase, year)
#                     if not canon and " " in phrase:
#                         canon = _resolve_venue_alias(phrase.split()[-1], year)
#                     # fallback to city if venue not found
#                     if not canon:
#                         canon = _resolve_city_alias(phrase, year)
#                         if not canon and " " in phrase:
#                             canon = _resolve_city_alias(phrase.split()[-1], year)
#                 else:
#                     # "in" -> prefer city
#                     canon = _resolve_city_alias(phrase, year)
#                     if not canon and " " in phrase:
#                         canon = _resolve_city_alias(phrase.split()[-1], year)
#                     # fallback to venue
#                     if not canon:
#                         canon = _resolve_venue_alias(phrase, year)
#                         if not canon and " " in phrase:
#                             canon = _resolve_venue_alias(phrase.split()[-1], year)
#             except Exception as e:
#                 logging.warning("[alias] loc:%s resolver error for '%s': %s", prefix, phrase, e)

#             if canon:
#                 # is this a venue canonical?
#                 is_venue = bool(con.execute(
#                     "SELECT 1 FROM alias_venues WHERE canonical_label = ? LIMIT 1", [canon]
#                 ).fetchone())
#                 if is_venue and canon not in resolved_venues:
#                     resolved_venues.append(canon)
#                     steps.append(f"loc:{prefix} -> venue='{canon}'")
#                     replacements_log.append((phrase_raw, canon, "loc:venue"))
#                 else:
#                     steps.append(f"loc:{prefix} -> city='{canon}'")
#                     replacements_log.append((phrase_raw, canon, "loc:city"))
#                 return f"{prefix} {canon}"

#             steps.append(f"loc:{prefix} -> no_match")
#             return f"{prefix} {phrase_raw}"

#         out = _PREP_LOC_RE.sub(_loc_sub, out)

#         # Protect tokens from already fixed venue(s)
#         protected_tokens = set()
#         for v in resolved_venues:
#             for t in re.findall(_WORD_RE, v):
#                 protected_tokens.add(t.lower())

#         # --- 2) Team phrases (vs/against) ---
#         def _team_sub(m: re.Match) -> str:
#             prefix = m.group("prefix")
#             phrase = m.group("phrase")
#             try:
#                 canon = _resolve_team_alias(phrase, year) or (_resolve_team_alias(phrase.split()[-1], year) if " " in phrase else None)
#             except Exception as e:
#                 logging.warning("[alias] team resolver error for '%s': %s", phrase, e)
#                 canon = None
#             if canon:
#                 steps.append(f"team:{prefix} -> '{canon}'")
#                 replacements_log.append((phrase, canon, "team"))
#                 return f"{prefix} {canon}"
#             steps.append(f"team:{prefix} -> no_match")
#             return f"{prefix} {phrase}"

#         out = _PREP_TEAM_RE.sub(_team_sub, out)

#             # --- 2b) Team phrases after "for/by/of": resolve TEAM (multi-word) ---
#         # --- 2b) Team phrases after "for/by/of": resolve TEAM (multi-word) ---
#         def _team_for_sub(m: re.Match) -> str:
#             prefix = m.group("prefix")
#             phrase_raw = m.group("phrase")
#             phrase = _trim_trailing_noise(_trim_trailing_stopwords(phrase_raw))

#             steps.append(f"team:{prefix} raw='{phrase_raw}' cleaned='{phrase}'")

#             # PLAYER first (full name -> alias -> initials+surname -> fuzzy(players))
#             try:
#                 p = _resolve_player_phrase(phrase, year)
#             except Exception as e:
#                 logging.warning("[alias] team(for/by/of) player-check error for '%s': %s", phrase, e)
#                 p = None
#             if p:
#                 steps.append(f"team:{prefix} -> actually player '{p}'")
#                 replacements_log.append((phrase_raw, p, "player"))
#                 for t in re.findall(_WORD_RE, p):
#                     protected_tokens.add(t.lower())
#                 return f"{prefix} {p}"

#             # TEAM next (Excel exact -> fallback last token)
#             try:
#                 canon = _resolve_team_alias(phrase, year) if phrase else None
#                 if not canon and phrase and " " in phrase:
#                     canon = _resolve_team_alias(phrase.split()[-1], year)
#             except Exception as e:
#                 logging.warning("[alias] team(for/by/of) resolver error for '%s': %s", phrase, e)
#                 canon = None

#             if canon:
#                 steps.append(f"team:{prefix} -> '{canon}'")
#                 replacements_log.append((phrase_raw, canon, "team"))
#                 for t in re.findall(_WORD_RE, canon):
#                     protected_tokens.add(t.lower())
#                 return f"{prefix} {canon}"

#             steps.append(f"team:{prefix} -> no_match (phrase='{phrase_raw}' cleaned='{phrase}')")
#             return f"{prefix} {phrase_raw}"
        
#         out = _PREP_TEAM_FOR_RE.sub(_team_for_sub, out)

#         # --- 3) Single-token pass with stopword/length guards ---
#         seen = set()
#         repl_pairs: list[tuple[str, str]] = []

#         for m in _WORD_RE.finditer(out):
#             tok = m.group(0)
#             low = tok.lower()
#             if low in seen or low in protected_tokens:
#                 continue
#             seen.add(low)

#             # Guard 1: skip stopwords entirely
#             if low in _STOPWORDS:
#                 steps.append(f"skip token(stopword) '{tok}'")
#                 continue

#             # Guard 2: skip very short tokens unless a known team abbr
#             if len(tok) < 3 and tok.upper() not in _TEAM_ABBR:
#                 try:
#                     p = _resolve_player_token(tok, year)   # uses initials/alias/fuzzy over players only
#                 except Exception as e:
#                     logging.warning("[alias] player(initials) error for '%s': %s", tok, e)
#                     p = None
#                 if p:
#                     repl_pairs.append((tok, p))
#                     replacements_log.append((tok, p, "player"))
#                     continue
#                 steps.append(f"skip token(short) '{tok}'")
#                 continue

#             # 3a) Player nickname (exact only)
#             try:
#                 player_hit = _resolve_player_token(tok, year)
#             except Exception as e:
#                 logging.warning("[alias] player_token resolver error for '%s': %s", tok, e)
#                 player_hit = None
#             if player_hit:
#                 repl_pairs.append((tok, player_hit))
#                 replacements_log.append((tok, player_hit, "player"))
#                 continue
            
#             # 3b) If token looks like a team code (ALL CAPS/known abbr), resolve TEAM **before** city
#             if tok.upper() in _TEAM_ABBR or tok.isupper():
#                 try:
#                     team = _resolve_team_alias(tok, year)
#                 except Exception as e:
#                     logging.warning("[alias] team resolver error for '%s': %s", tok, e)
#                     team = None
#                 if team:
#                     repl_pairs.append((tok, team))
#                     replacements_log.append((tok, team, "team"))
#                     continue
#             # fall through to city/venue if team didnâ€™t match

#             # 3c) City
#             try:
#                 city = _resolve_city_alias(tok, year)
#             except Exception as e:
#                 logging.warning("[alias] city resolver error for '%s': %s", tok, e)
#                 city = None
#             if city:
#                 repl_pairs.append((tok, city))
#                 replacements_log.append((tok, city, "city"))
#                 continue

#             # 3d) Venue (only if none fixed via phrases)
#             if not resolved_venues:
#                 try:
#                     venue = _resolve_venue_alias(tok, year)
#                 except Exception as e:
#                     logging.warning("[alias] venue resolver error for '%s': %s", tok, e)
#                     venue = None
#                 if venue:
#                     repl_pairs.append((tok, venue))
#                     replacements_log.append((tok, venue, "venue"))
#                     resolved_venues.append(venue)  # lock further venues
#                     for t in re.findall(_WORD_RE, venue):
#                         protected_tokens.add(t.lower())
#                     continue

#             # 3e) Team (normal order if not an ALL-CAPS code)
#             try:
#                 team = _resolve_team_alias(tok, year)
#             except Exception as e:
#                 logging.warning("[alias] team resolver error for '%s': %s", tok, e)
#                 team = None
#             if team:
#                 repl_pairs.append((tok, team))
#                 replacements_log.append((tok, team, "team"))
#                 continue

#         if repl_pairs:
#             repl_pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
#             for src, dst in repl_pairs:
#                 out = re.sub(rf"\b{re.escape(src)}\b", dst, out)

#         # --- 4) LLM hint when a venue is fixed ---
#         if resolved_venues:
#             hint = (
#                 " [Resolved venue: "
#                 + "; ".join(resolved_venues)
#                 + ". When a venue is specified, filter ONLY by matches_q.venue (exact equality); do NOT add city predicates.]"
#             )
#             out = out + hint

#         logging.info("[alias] resolved_venues=%s", resolved_venues)
#         logging.info("[alias] replacements=%s", replacements_log)
#         logging.info("[alias] steps=%s", steps)
#         logging.info("[alias] output='%s'", out)

#         return out

#     except Exception as e:
#         logging.exception("[alias] pre_resolve failed: %s", e)
#         return question

def _pre_resolve_or_clarify(question: str) -> dict:
    """
    Try to rewrite the user question by snapping aliases to canonical entities.
    If a PLAYER reference is ambiguous (e.g., 'Pandya' -> HH/KH), return a clarify payload.

    Returns:
      {"mode": "clarify", "payload": {...}}  OR  {"mode": "rewrite", "rewritten": "<text>"}
    """
    try:
        if not question:
            return {"mode": "rewrite", "rewritten": question}

        year = _extract_year_from_text(question)
        out = question

        resolved_venues: list[str] = []
        steps: list[str] = []
        replacements_log: list[tuple[str,str,str]] = []
        # clarify_box = {"payload": None}

        logging.info("[alias] input=%r year=%s", question, year)
        _debug_team_for_matches(out)
        
        # -------------------------------
        # Helpers for ambiguity (players)
        # -------------------------------
        def _ambiguous_players_for_phrase(phrase: str) -> list[str]:
            """Return candidate player full_names if 'phrase' is ambiguous; else []"""
            phrase = _normalize_ws_case(phrase)
            if not phrase:
                return []

            # exact full name in catalog -> not ambiguous
            hit = _catalog_exact_player(phrase)
            if hit:
                return []

            # exact alias -> not ambiguous
            hit = _player_exact_from_alias(phrase)
            if hit:
                return []

            # initials + surname: strictly query possible matches
            parts = phrase.split()
            if len(parts) == 2 and parts[0].isalpha() and 1 <= len(parts[0]) <= 3:
                rows = con.execute("""
                    WITH derived AS (
                      SELECT
                        name AS full_name,
                        CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',1) ELSE name END AS first_name,
                        CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',-1) ELSE name END AS last_name
                      FROM players_catalog
                    )
                    SELECT DISTINCT full_name
                    FROM derived
                    WHERE lower(last_name) = lower(?)
                      AND (
                           lower(first_name) = lower(?)
                        OR substr(lower(first_name),1,1) = lower(substr(?,1,1))
                      )
                """, [parts[1], parts[0], parts[0]]).fetchall()
                cands = [r[0] for r in rows if r and r[0]]
                # If more than one, it's ambiguous
                return cands if len(cands) > 1 else []

            # single surname like "Pandya": collect *all* catalog players whose last_name matches
            if " " not in phrase:
                rows = con.execute("""
                    WITH derived AS (
                      SELECT
                        name AS full_name,
                        CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',1) ELSE name END AS first_name,
                        CASE WHEN strpos(name,' ')>0 THEN split_part(name,' ',-1) ELSE name END AS last_name
                      FROM players_catalog
                    )
                    SELECT DISTINCT full_name
                    FROM derived
                    WHERE lower(last_name) = lower(?)
                """, [phrase]).fetchall()
                cands = [r[0] for r in rows if r and r[0]]
                return cands if len(cands) > 1 else []

            return []

        # ---------------------------------
        # 1) Location phrases (in / at)
        # ---------------------------------
        def _loc_sub(m: re.Match) -> str:
            prefix = m.group("prefix").lower()
            phrase_raw = m.group("phrase")
            phrase = _trim_trailing_stopwords(phrase_raw)
            canon: Optional[str] = None

            steps.append(f"loc:{prefix} phrase='{phrase_raw}' -> trimmed='{phrase}'")
            try:
                if prefix == "at":
                    # Prefer venue
                    canon = _resolve_venue_alias(phrase, year) or (_resolve_venue_alias(phrase.split()[-1], year) if " " in phrase else None)
                    if not canon:
                        canon = _resolve_city_alias(phrase, year) or (_resolve_city_alias(phrase.split()[-1], year) if " " in phrase else None)
                else:
                    # Prefer city
                    canon = _resolve_city_alias(phrase, year) or (_resolve_city_alias(phrase.split()[-1], year) if " " in phrase else None)
                    if not canon:
                        canon = _resolve_venue_alias(phrase, year) or (_resolve_venue_alias(phrase.split()[-1], year) if " " in phrase else None)
            except Exception as e:
                logging.warning("[alias] loc:%s resolver error for '%s': %s", prefix, phrase, e)

            if canon:
                is_venue = bool(con.execute(
                    "SELECT 1 FROM alias_venues WHERE canonical_label = ? LIMIT 1", [canon]
                ).fetchone())
                if is_venue and canon not in resolved_venues:
                    resolved_venues.append(canon)
                    steps.append(f"loc:{prefix} -> venue='{canon}'")
                    replacements_log.append((phrase_raw, canon, "loc:venue"))
                else:
                    steps.append(f"loc:{prefix} -> city='{canon}'")
                    replacements_log.append((phrase_raw, canon, "loc:city"))
                return f"{prefix} {canon}"

            steps.append(f"loc:{prefix} -> no_match")
            return f"{prefix} {phrase_raw}"

        out = _PREP_LOC_RE.sub(_loc_sub, out)

        # Protect tokens from already fixed venue(s)
        protected_tokens = set()
        for v in resolved_venues:
            for t in re.findall(_WORD_RE, v):
                protected_tokens.add(t.lower())

        # ---------------------------------
        # 2) Team phrases (vs/against)
        # ---------------------------------
        def _team_sub(m: re.Match) -> str:
            prefix = m.group("prefix")
            phrase = m.group("phrase")
            try:
                canon = _resolve_team_alias(phrase, year) or (_resolve_team_alias(phrase.split()[-1], year) if " " in phrase else None)
            except Exception as e:
                logging.warning("[alias] team resolver error for '%s': %s", phrase, e)
                canon = None
            if canon:
                steps.append(f"team:{prefix} -> '{canon}'")
                replacements_log.append((phrase, canon, "team"))
                return f"{prefix} {canon}"
            steps.append(f"team:{prefix} -> no_match")
            return f"{prefix} {phrase}"

        out = _PREP_TEAM_RE.sub(_team_sub, out)

        # ----------------------------------------------------
        # 2b) Team phrases after "for/by/of": PLAYER first!
        # ----------------------------------------------------
        def _team_for_sub(m: re.Match) -> str:
            prefix = m.group("prefix")
            phrase_raw = m.group("phrase")
            phrase = _trim_trailing_noise(_trim_trailing_stopwords(phrase_raw))
            steps.append(f"team:{prefix} raw='{phrase_raw}' cleaned='{phrase}'")

            # â–¶ï¸Ž Clarify if ambiguous player
            amb = _ambiguous_players_for_phrase(phrase)
            if amb:
                payload = {
                    "type": "player",
                    "phrase": phrase,
                    "choices": sorted(set(amb)),  # UI expects plain list
                }
                logging.info("[alias] clarify-player %r -> %s", phrase, payload["choices"])
                raise ClarifyNeeded(payload)  # marker we will detect below
                # raise ClarifyNeeded({"__CLARIFY__": payload})

            # PLAYER resolve (catalog/alias/initials+surname)
            try:
                p = _resolve_player_phrase(phrase, year)
            except Exception as e:
                logging.warning("[alias] team(for/by/of) player-check error for '%s': %s", phrase, e)
                p = None
            if p:
                steps.append(f"team:{prefix} -> actually player '{p}'")
                replacements_log.append((phrase_raw, p, "player"))
                for t in re.findall(_WORD_RE, p):
                    protected_tokens.add(t.lower())
                return f"{prefix} {p}"

            # TEAM if not a player
            try:
                canon = _resolve_team_alias(phrase, year) if phrase else None
                if not canon and phrase and " " in phrase:
                    canon = _resolve_team_alias(phrase.split()[-1], year)
            except Exception as e:
                logging.warning("[alias] team(for/by/of) resolver error for '%s': %s", phrase, e)
                canon = None

            if canon:
                steps.append(f"team:{prefix} -> '{canon}'")
                replacements_log.append((phrase_raw, canon, "team"))
                for t in re.findall(_WORD_RE, canon):
                    protected_tokens.add(t.lower())
                return f"{prefix} {canon}"

            steps.append(f"team:{prefix} -> no_match (phrase='{phrase_raw}' cleaned='{phrase}')")
            return f"{prefix} {phrase_raw}"
        
        try:
            out = _PREP_TEAM_FOR_RE.sub(_team_for_sub, out)
        except ClarifyNeeded as c:
            return {"mode": "clarify", "payload": c.payload}

        
        # ----------------------------------------------------
        # 3) Single-token pass (player â†’ city â†’ venue â†’ team)
        # ----------------------------------------------------
        seen = set()
        repl_pairs: list[tuple[str, str]] = []

        for m in _WORD_RE.finditer(out):
            tok = m.group(0)
            low = tok.lower()
            if low in seen or low in protected_tokens:
                continue
            seen.add(low)

            # stopwords
            if low in _STOPWORDS:
                steps.append(f"skip token(stopword) '{tok}'")
                continue

            # very short tokens â†’ try player initials (no fuzzy)
            if len(tok) < 3 and tok.upper() not in _TEAM_ABBR:
                try:
                    p = _resolve_player_token(tok, year)
                except Exception as e:
                    logging.warning("[alias] player(initials) error for '%s': %s", tok, e)
                    p = None
                if p:
                    repl_pairs.append((tok, p))
                    replacements_log.append((tok, p, "player"))
                    continue
                steps.append(f"skip token(short) '{tok}'")
                continue

            # Player by token (catalog/alias/initials-only)
            try:
                player_hit = _resolve_player_token(tok, year)
            except Exception as e:
                logging.warning("[alias] player_token resolver error for '%s': %s", tok, e)
                player_hit = None
            if player_hit:
                repl_pairs.append((tok, player_hit))
                replacements_log.append((tok, player_hit, "player"))
                continue

            # Team code first if ALL CAPS / known abbr
            if tok.upper() in _TEAM_ABBR or tok.isupper():
                try:
                    team = _resolve_team_alias(tok, year)
                except Exception as e:
                    logging.warning("[alias] team resolver error for '%s': %s", tok, e)
                    team = None
                if team:
                    repl_pairs.append((tok, team))
                    replacements_log.append((tok, team, "team"))
                    continue

            # City
            try:
                city = _resolve_city_alias(tok, year)
            except Exception as e:
                logging.warning("[alias] city resolver error for '%s': %s", tok, e)
                city = None
            if city:
                repl_pairs.append((tok, city))
                replacements_log.append((tok, city, "city"))
                continue

            # Venue (only if none fixed via phrases)
            if not resolved_venues:
                try:
                    venue = _resolve_venue_alias(tok, year)
                except Exception as e:
                    logging.warning("[alias] venue resolver error for '%s': %s", tok, e)
                    venue = None
                if venue:
                    repl_pairs.append((tok, venue))
                    replacements_log.append((tok, venue, "venue"))
                    resolved_venues.append(venue)
                    for t in re.findall(_WORD_RE, venue):
                        protected_tokens.add(t.lower())
                    continue

            # Team
            try:
                team = _resolve_team_alias(tok, year)
            except Exception as e:
                logging.warning("[alias] team resolver error for '%s': %s", tok, e)
                team = None
            if team:
                repl_pairs.append((tok, team))
                replacements_log.append((tok, team, "team"))
                continue

        if repl_pairs:
            repl_pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
            for src, dst in repl_pairs:
                out = re.sub(rf"\b{re.escape(src)}\b", dst, out)

        # Venue hint
        if resolved_venues:
            hint = (
                " [Resolved venue: "
                + "; ".join(resolved_venues)
                + ". When a venue is specified, filter ONLY by matches_q.venue (exact equality); do NOT add city predicates.]"
            )
            out = out + hint

        logging.info("[alias] resolved_venues=%s", resolved_venues)
        logging.info("[alias] replacements=%s", replacements_log)
        logging.info("[alias] steps=%s", steps)
        logging.info("[alias] output=%r", out)

        return {"mode": "rewrite", "rewritten": out}

    except Exception as e:
        logging.exception("[alias] pre_resolve_or_clarify failed: %s", e)
        return {"mode": "rewrite", "rewritten": question}


# Back-compat shim: keep old name if other code still calls it
def _pre_resolve_text(question: str) -> str:
    return _pre_resolve_or_clarify(question)
    # return res.get("rewritten", question)


# ---------------- FastAPI ----------------
app = FastAPI(title="CoverDrive")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],)


if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=False), name="static")
else:
    print(f"[init] Skipping static mount, no folder found at {FRONTEND_DIR}")

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
    if FRONTEND_DIR.is_dir():
        assert (FRONTEND_DIR / "index.html").exists(), f"Missing {FRONTEND_DIR / 'index.html'}"
    init_views()

    # --- NEW: load alias sheets (teams/city/venue + player nicknames) ---
    # Priority: env var -> data/teams_alias.xlsx -> /mnt/data/teams_alias.xlsx (for local uploads)
    aliases_path = os.getenv("ALIASES_XLS_PATH") or "data/teams_alias.xlsx"
    if not Path(aliases_path).exists() and Path("/mnt/data/teams_alias.xlsx").exists():
        aliases_path = "/mnt/data/teams_alias.xlsx"
    try:
        load_aliases_from_excel(con, Path(aliases_path))
        logging.info(f"[alias] Loaded alias workbook from {aliases_path}")
    except Exception as e:
        logging.warning(f"[alias] Skipping alias load: {e}")
    
    # Build catalogs for exact, year-aware matching (Step-1)
    init_catalogs()
    logging.info("[startup] catalogs ready")

# ---------------- Models ----------------
class NLQRequest(BaseModel):
    question: str = Field(..., description="Natural language question")
    max_rows: int = Field(200, ge=1, le=5000)
    selections: Optional[dict[str, str]] = None   # e.g. {"player": "KH Pandya"}
    mention: Optional[str] = None 

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

class AskReq(BaseModel):
    question: str
    selections: Optional[dict] = None

class AskResp(BaseModel):
    status: str                       # "clarify" | "ok"
    question: Optional[str] = None
    options: Optional[list] = None    # [{kind, value}, ...]
    context: Optional[dict] = None    # resolved entities to feed LLM
    message: Optional[str] = None

# near your other pydantic models
# class NLQClarify(BaseModel):
#     status: str = "clarify"
#     dimension: str
#     question: str
#     options: List[Dict[str, str]]  # [{"label": "...", "value": "..."}]
#     original_question: str
#     phrase: str

class ClarifyNeeded(Exception):
    def __init__(self, payload):
        self.payload = payload

# ---------------- Models ----------------
class NLQClarify(BaseModel):
    status: str = "needs_clarification"
    entity: str
    mention: str
    options: List[str]
    correlation_id: Optional[str] = None



# ---------------- Routes ----------------
@app.post("/nlq", response_model=Union[NLQSuccess, NLQError, NLQClarify])
def nlq(req: NLQRequest, request: Request):
    start = time.time()
    corr_id = str(uuid.uuid4())
    debug = request.headers.get("X-Debug", "0") in ("1", "true", "True")

    q = (req.question or "").strip()

    if req.selections:
        kind, chosen = next(iter(req.selections.items()))
        mention = (req.mention or "").strip()
        if mention:
            try:
                q = re.sub(rf"\b{re.escape(mention)}\b", chosen, q, count=1)
            except Exception:
                q = q.replace(mention, chosen, 1)
        else:
            q = f"{q} [Resolved {kind}: {chosen}]"

    # 1) NL -> (SQL or Clarify)
    try:
        llm_out = generate_sql_or_clarify(q)
    except Exception:
        return NLQError(
            code="NLQ_UNSUPPORTED",
            message="Weâ€™re working on this type of query. Try rephrasing or narrowing it.",
            correlation_id=corr_id,
        )

    if llm_out.get("type") == "clarify":
        entity = llm_out.get("entity", "")
        mention = llm_out.get("mention", "")
        season = llm_out.get("season") or _extract_year_from_text(q)
        options = _candidate_list(entity, mention, season)
        if not options:
            return NLQError(
                code="CLARIFY_FAILED",
                message="We couldnâ€™t find matching options. Try adding the season or more detail.",
                correlation_id=corr_id,
            )
        return NLQClarify(
            status="needs_clarification",
            entity=str(entity),
            mention=str(mention),
            options=[str(x) for x in options],
            correlation_id=corr_id,
        )

    # 2) Normalize + post-process
    sql = normalize_sql(llm_out.get("sql", ""))
    logging.info("[llm] SQL(norm)=%s", sql)
    year_hint = _extract_year_from_text(q)
    sql = _snap_venue_literals_in_sql(sql, year_hint)
    sql = _drop_city_when_venue_locked(sql)
    logging.info("[llm] SQL(final)=%s", sql)
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
            message="Weâ€™re working on this type of query. Try rephrasing or narrowing it.",
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


@app.post("/ask", response_model=AskResp)
def ask(req: AskReq):
    r = resolve_entities(con, req.question)

    # apply selections if the UI posts them back
    if req.selections:
        for kind, val in req.selections.items():
            if val:
                r["resolved"].setdefault(kind, [])
                if val not in r["resolved"][kind]:
                    r["resolved"][kind].append(val)
        # remove any clarify of kinds that are now resolved
        remain = []
        for c in r["clarify"]:
            if not r["resolved"].get(c["kind"]):
                remain.append(c)
        r["clarify"] = remain

    if r["clarify"]:
        q = r["clarify"][0]["label"]
        opts = [{"kind": r["clarify"][0]["kind"], "value": o} for o in r["clarify"][0]["options"]]
        return AskResp(
            status="clarify",
            question=q,
            options=opts,
            context={"resolved": r["resolved"], "unresolved": r["unresolved"]}
        )

    return AskResp(
        status="ok",
        context={
            "resolved_entities": r["resolved"],
            "unresolved_terms": r["unresolved"],
            "notes": [
                "Use canonical names in SQL filters.",
                "If a needed entity list is empty, ask for clarification."
            ],
        },
        message="Entities resolved."
    )


# ---------------- AWS Lambda handler ----------------
handler = Mangum(app)


from pathlib import Path
from typing import List, Any, Optional, Union
import logging, os, re, time, uuid
import json

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

# new: catalog views
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



# helper functions:
# ---------------- Candidate helpers (Step 2) ----------------

# ---------------- Candidate helpers (improved) ----------------
# ---------------- Candidate helpers (alias-free, fuzzy) ----------------
import difflib

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _initials(label: str) -> str:
    # e.g., "MS Dhoni" -> "msd"; "Virat Kohli" -> "vk"
    toks = [t for t in re.split(r"\s+", _norm(label)) if t]
    if not toks:
        return ""
    letters = []
    for t in toks:
        letters.append(t[0])
        if len(letters) >= 3:
            break
    return "".join(letters)

def _fuzzy_score(q: str, label: str) -> float:
    """
    Hybrid similarity score 0..100:
      - 100 for exact normalized match
      - 97 if nospace match (e.g., 'msdhoni')
      - 98 if initials match (e.g., 'msd' -> 'MS Dhoni')
      - 96 for substring match
      - else difflib ratio
    """
    qn = _norm(q)
    ln = _norm(label)
    if not qn or not ln:
        return 0.0

    if qn == ln:
        return 100.0

    nospace = ln.replace(" ", "")
    if qn == nospace:
        return 97.0

    init = _initials(label)
    if qn == init:
        return 98.0

    if qn in ln or ln in qn or qn in nospace or nospace in qn:
        return 96.0

    return round(difflib.SequenceMatcher(a=qn, b=ln).ratio() * 100, 1)

def _rank_and_trim(q: str, rows, label_idx: int, freq_idx: int, limit: int):
    # rows: iterable of (label, freq)
    scored = []
    for r in rows:
        label = str(r[label_idx]) if r[label_idx] is not None else ""
        freq  = int(r[freq_idx]) if r[freq_idx] is not None else 0
        score = _fuzzy_score(q, label)
        scored.append((score, freq, label))

    # Sort by (score desc, freq desc, label asc)
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    if not scored:
        return []

    top_score = scored[0][0]

    # Keep items close to the best or above a minimum floor
    floor = max(60.0, top_score - 8.0)
    filtered = [t for t in scored if t[0] >= floor]

    # If even the best is weak (<60), only show top 3 to avoid noise
    if top_score < 60.0:
        filtered = scored[:3]

    uniq, seen = [], set()
    for score, freq, label in filtered:
        if label not in seen:
            uniq.append({"label": label, "score": float(score), "freq": freq})
            seen.add(label)
        if len(uniq) >= limit:
            break
    return uniq

def _prefilter_like(table: str, norm_col: str, q: str, label_col: str, freq_col: str, limit_sql: int = 200):
    """
    Prefilter that tries BOTH directions:
      - catalog_norm LIKE %query_norm%               (old way)
      - query_norm LIKE %catalog_norm%               (new, critical for full-sentence questions)
    Also tries nospace variants so 'msd' matches 'ms dhoni'.
    """
    qn = _norm(q)
    like_q = f"%{qn}%"
    like_q_nospace = f"%{qn.replace(' ', '')}%"

    sql = f"""
        SELECT {label_col}, {freq_col}
        FROM {table}
        WHERE
            {norm_col} LIKE ?                         -- catalog contains query (rare for sentences)
         OR REPLACE({norm_col}, ' ', '') LIKE ?       -- nospace variant
         OR ? LIKE ('%' || {norm_col} || '%')         -- query contains catalog (common for sentences)
         OR REPLACE(?, ' ', '') LIKE
                ('%' || REPLACE({norm_col}, ' ', '') || '%')
        LIMIT {limit_sql}
    """
    # params order matches the 4 placeholders above
    return con.execute(sql, [like_q, like_q_nospace, qn, qn]).fetchall()

def _prefilter_fallback(table: str, label_col: str, freq_col: str, limit_sql: int = 1000):
    """
    Fallback: take most frequent labels; fuzzy ranking will re-order them.
    """
    sql = f"SELECT {label_col}, {freq_col} FROM {table} ORDER BY {freq_col} DESC LIMIT {limit_sql}"
    return con.execute(sql).fetchall()

def get_candidates(entity: str, mention: str, limit: int = 8):
    """
    entity ∈ {"player","team","city","venue","season"}
    Returns a list[{"label": "...", "score": 0..100, "freq": int}]
    """
    entity = (entity or "").lower().strip()
    q = mention or ""
    if not q:
        return []

    if entity == "player":
        rows = _prefilter_like("players_catalog", "norm", q, "name", "hits")
        if not rows:
            rows = _prefilter_fallback("players_catalog", "name", "hits")
        return _rank_and_trim(q, rows, label_idx=0, freq_idx=1, limit=limit)

    if entity == "team":
        rows = _prefilter_like("teams_catalog", "norm", q, "team", "games")
        if not rows:
            rows = _prefilter_fallback("teams_catalog", "team", "games")
        return _rank_and_trim(q, rows, label_idx=0, freq_idx=1, limit=limit)

    if entity == "city":
        rows = _prefilter_like("cities_catalog", "norm", q, "city", "games")
        if not rows:
            rows = _prefilter_fallback("cities_catalog", "city", "games")
        return _rank_and_trim(q, rows, label_idx=0, freq_idx=1, limit=limit)

    if entity == "venue":
        rows = _prefilter_like("venues_catalog", "norm", q, "venue", "games")
        if not rows:
            rows = _prefilter_fallback("venues_catalog", "venue", "games")
        return _rank_and_trim(q, rows, label_idx=0, freq_idx=1, limit=limit)

    if entity == "season":
        # Parse years, ranges, and phrases like "last 3 seasons"
        qn = _norm(q)
        latest = con.execute("SELECT latest_season FROM constants").fetchone()
        latest_season = int(latest[0]) if latest and latest[0] else None

        years = set()

        # explicit years
        for y in re.findall(r"\b(20[0-3]\d)\b", qn):
            years.add(int(y))

        # ranges like 2021-2023
        m = re.search(r"\b(20[0-3]\d)\s*[-–]\s*(20[0-3]\d)\b", qn)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b:
                for yy in range(a, b + 1):
                    years.add(yy)

        # phrases
        if latest_season:
            if "last season" in qn:
                years.add(latest_season)
            m2 = re.search(r"last\s+(\d+)\s+seasons", qn)
            if m2:
                n = int(m2.group(1))
                for i in range(n):
                    years.add(latest_season - i)
            if "this season" in qn or "current season" in qn:
                years.add(latest_season)

        if years:
            avail = {int(r[0]) for r in con.execute("SELECT season FROM seasons_catalog").fetchall()}
            years = [y for y in sorted(years, reverse=True) if y in avail]
            return [{"label": y, "score": 100.0, "freq": 0} for y in years][:limit]

        rows = con.execute("SELECT season FROM seasons_catalog ORDER BY season DESC LIMIT 8").fetchall()
        return [{"label": int(r[0]), "score": 50.0, "freq": 0} for r in rows]

    return []


# Step 3: extract JSON object from LLM text
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
def _extract_json_obj(text: str) -> Optional[dict]:
    """
    Try to parse a JSON object from LLM text. Supports fenced ```json blocks
    or raw JSON in the message. Returns dict or None.
    """
    if not text:
        return None
    # Prefer fenced block
    m = _JSON_BLOCK_RE.search(text)
    candidates = []
    if m:
        candidates.append(m.group(1))
    candidates.append(text)

    for cand in candidates:
        cand = cand.strip()
        # Heuristic: find first '{' ... last '}' to reduce prefix/suffix chatter
        start = cand.find("{")
        end = cand.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cand[start:end+1])
            except Exception:
                pass
    return None

# Step 4: Minimal schema hint + prompt template
SCHEMA_HINT = """
Tables and columns (DuckDB):

deliveries_q(
  match_id, season, season_year, inning, over, ball_in_over,
  batting_team, batter, non_striker, bowler,
  runs_batter, runs_total, extras_total, wides, noballs, legbyes, byes, penalty,
  dismissal_kind, player_out, fielder, venue, city, date
)

matches_q(
  match_id, season, date, competition, venue, city,
  team1, team2, winner, toss_winner, toss_decision,
  match_ts, match_date, match_year
)
"""

METRIC_GLOSSARY = """
Metric glossary:
- "total runs" by a batter: SUM(runs_batter)
- "sixes" (batter): COUNT(*) over deliveries where runs_batter = 6
- "fours"  (batter): COUNT(*) over deliveries where runs_batter = 4
- "team total runs" in a match: SUM(runs_total) GROUP BY match/team
- "strike rate" (batter): 100 * SUM(runs_batter) / COUNT(*) over legal balls (exclude wides)
- "economy" (bowler): SUM(runs_total) / (COUNT(*)/6) over legal balls (exclude wides, no-balls for ball count)
- "powerplay": overs 1–6 (use over BETWEEN 1 AND 6)
- "playoffs": competition ILIKE '%Qualifier%' OR '%Eliminator%' OR '%Final%'
"""

LLM_INSTRUCTIONS = """
You are a SQL planner for DuckDB over the given IPL schema.

You MUST return JSON in EXACTLY ONE of these shapes:

1) Confident:
{
  "status": "ok",
  "resolutions": {
    "players": {"<mention>": "<normalized full name>", ...},
    "teams":   {"<mention>": "<normalized team name>", ...},
    "cities":  {"<mention>": "<normalized city>", ...},
    "venues":  {"<mention>": "<normalized venue>", ...},
    "seasons": [<YYYY>, ...],
    "metrics": {"metric": "<one_of: total_runs, team_total_runs, strike_rate, economy, wickets, matches, ...>"}
  },
  "sql": "SELECT ... FROM ... WHERE ...;  -- single runnable SQL"
}

2) Needs clarification (for one unresolved mention):
{
  "status": "needs_clarification",
  "entity": "<player|team|city|venue|season|metric>",
  "mention": "<the original ambiguous string>",
  "options": ["candidate1","candidate2","candidate3"]
}

Rules:
- Use ONLY values from the provided candidate lists for players, teams, cities, and venues.
- Use seasons exactly as numbers (YYYY). Expand relative phrases like "last 3 seasons" to explicit years if candidates provided.
- Prefer matches_q.city for city filters; use deliveries_q only for per-ball metrics.
- If unsure which entity type a mention refers to, ask for clarification.
- If the user question includes a hint of the form:
-    (disambiguate: <entity> "<mention>" = "<choice>")
-   then treat that as the user's choice and resolve that mention accordingly in "resolutions" (do not ask again).
- Return a SINGLE SQL statement if status is "ok". No commentary outside the JSON.
"""

# ---------------- SQL extraction & normalization ----------------
SQL_BLOCK_RE = re.compile(
    r"(?:```sql\s*)(?P<sql>.*?)(?:```)|"
    r"(?:```\s*)(?P<sql2>.*?)(?:```)|"
    r"(?P<inline>(?:WITH|SELECT)\b[\s\S]+)",
    flags=re.IGNORECASE | re.DOTALL,
)

# scripts/test_gemini_sql.py
import json, logging

def _extract_sdk(resp) -> str:
    """
    Extract text from Gemini SDK responses. Logs shape + sizes so we can see
    why we sometimes get empty strings.
    """
    try:
        logging.info("[SDK] type=%s", type(resp).__name__)

        # Plain string
        if isinstance(resp, str):
            s = (resp or "").strip()
            logging.info("[SDK] string_len=%d", len(s))
            return s

        # Dict JSON
        if isinstance(resp, dict):
            logging.info("[SDK] dict_keys=%s", list(resp.keys())[:12])
            cands = resp.get("candidates") or []
            logging.info("[SDK] dict.candidates=%d", len(cands))
            texts = []
            for i, c in enumerate(cands):
                fr = c.get("finishReason") or c.get("finish_reason")
                logging.info("[SDK] cand[%d].finish=%s keys=%s", i, fr, list(c.keys())[:12])
                content = c.get("content") or {}
                parts = content.get("parts") or []
                logging.info("[SDK] cand[%d].parts=%d", i, len(parts))
                for j, p in enumerate(parts):
                    if isinstance(p, dict) and "text" in p:
                        t = p.get("text") or ""
                        logging.info("[SDK] cand[%d].part[%d].text_len=%d", i, j, len(t))
                        if t.strip():
                            texts.append(t)
                out = c.get("output")
                if isinstance(out, str) and out.strip():
                    logging.info("[SDK] cand[%d].output_len=%d", i, len(out))
                    texts.append(out)
            s = "\n".join(t for t in texts if t).strip()
            logging.info("[SDK] collected_len=%d", len(s))
            return s

        # SDK object (google-genai)
        if hasattr(resp, "candidates"):
            cands = getattr(resp, "candidates") or []
            logging.info("[SDK] obj.candidates=%d", len(cands))
            # Finish reason of first cand
            try:
                if cands:
                    fr = getattr(cands[0], "finish_reason", None) or getattr(cands[0], "finishReason", None)
                    logging.info("[SDK] obj.finish=%s", fr)
            except Exception:
                pass

            texts = []
            for i, c in enumerate(cands):
                try:
                    content = getattr(c, "content", None)
                    parts = getattr(content, "parts", None) or []
                    logging.info("[SDK] cand[%d].parts=%d", i, len(parts))
                    for j, p in enumerate(parts):
                        t = getattr(p, "text", None)
                        # Also log other part types (function/tool calls) if present
                        if t is None and hasattr(p, "function_call"):
                            logging.info("[SDK] cand[%d].part[%d].function_call", i, j)
                        if t is None and hasattr(p, "inline_data"):
                            logging.info("[SDK] cand[%d].part[%d].inline_data", i, j)
                        logging.info("[SDK] cand[%d].part[%d].text_len=%d", i, j, len(t or ""))
                        if isinstance(t, str) and t.strip():
                            texts.append(t)
                    out = getattr(c, "output", None)
                    if isinstance(out, str) and out.strip():
                        logging.info("[SDK] cand[%d].output_len=%d", i, len(out))
                        texts.append(out)
                except Exception as ie:
                    logging.error("[SDK] cand[%d] parse_error=%s", i, ie)

            s = "\n".join(t for t in texts if t).strip()
            logging.info("[SDK] collected_len=%d", len(s))
            if s:
                return s

            # As a last resort, try to_dict()/model_dump() to inspect raw payload
            for meth in ("to_dict", "model_dump", "__dict__"):
                try:
                    if hasattr(resp, meth):
                        obj = getattr(resp, meth)() if callable(getattr(resp, meth)) else getattr(resp, meth)
                        payload = json.dumps(obj, default=str)[:4000]
                        logging.info("[SDK] %s_len=%d preview=%s", meth, len(payload), payload[:300])
                        break
                except Exception:
                    pass

        # Unknown shape → short string fallback
        s = (str(resp) or "").strip()
        logging.warning("[SDK] unknown_shape str_len=%d", len(s))
        return s

    except Exception as e:
        logging.exception("[SDK] EXTRACT_FAIL: %s", e)
        return ""



def safe_generate_sql(nlq: str) -> str:
    try:
        raw = generate_sql(nlq); return clean_and_validate_sql_from_text(raw)
    except Exception:
        text = _extract_sdk(call_gemini(build_prompt(nlq)))
        try: return clean_and_validate_sql_from_text(text)
        except Exception: return clean_and_validate_sql_from_text(text)

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

    # --- NEW: fix wrong column on matches_q (season_year -> match_year)
    mq_aliases = set()
    for m in re.finditer(r"(?i)\bfrom\s+matches_q(?:\s+(?:as\s+)?([a-z_][a-z0-9_]*))?", s):
        mq_aliases.add((m.group(1) or "").lower() or None)
    for m in re.finditer(r"(?i)\bjoin\s+matches_q(?:\s+(?:as\s+)?([a-z_][a-z0-9_]*))?", s):
        mq_aliases.add((m.group(1) or "").lower() or None)

    # Qualified: m.season_year or matches_q.season_year -> .match_year
    for a in [a for a in mq_aliases if a]:
        s = re.sub(rf"(?i)\b{re.escape(a)}\s*\.\s*season_year\b", f"{a}.match_year", s)
    s = re.sub(r"(?i)\bmatches_q\s*\.\s*season_year\b", "matches_q.match_year", s)

    # Unqualified: if ONLY matches_q appears (no deliveries_q), flip bare season_year -> match_year
    if mq_aliases and not re.search(r"(?i)\bdeliveries_q\b", s):
        s = re.sub(r"(?i)\bseason_year\b", "match_year", s)

    # Cleanup: avoid "...; LIMIT" and stray "AS" without alias
    s = s.strip()
    s = re.sub(r";\s*(limit|offset)\b", r" \1", s, flags=re.IGNORECASE)
    s = re.sub(r"(?i)\b(FROM|JOIN)\s+([a-z_][a-z0-9_\.]*?)\s+AS(\s+)(?=(WHERE|ON|JOIN|GROUP|ORDER|LIMIT|OFFSET|$))",
               r"\1 \2 ", s)

    return s

# Step 5: Build candidate bundle and final prompt
def build_candidate_bundle(question: str, limit: int = 8) -> dict:
    bundle = {
        "players": get_candidates("player", question, limit),
        "teams":   get_candidates("team", question, limit),
        "cities":  get_candidates("city", question, limit),
        "venues":  get_candidates("venue", question, limit),
        "seasons": [c["label"] for c in get_candidates("season", question, limit=6)],
    }
    # Allow explicit years from the user even if catalogs are off
    explicit_years = {int(y) for y in re.findall(r"\b(20[0-3]\d)\b", question)}
    bundle["seasons"] = sorted(set(bundle["seasons"]) | explicit_years, reverse=True)[:8]
    return bundle


def generate_sql_with_candidates(question: str) -> dict:
    cands = build_candidate_bundle(question)

    # Trim candidate lists to keep prompt small
    players = [x["label"] for x in cands["players"]][:5]
    teams   = [x["label"] for x in cands["teams"]][:5]
    cities  = [x["label"] for x in cands["cities"]][:5]
    venues  = [x["label"] for x in cands["venues"]][:5]
    seasons = list(cands["seasons"])[:6]

    prompt = f"""
{LLM_INSTRUCTIONS}

Schema:
{SCHEMA_HINT}

Metric glossary:
{METRIC_GLOSSARY}

User question:
{question}

Candidate lists (use for disambiguation; explicit years in the question are valid even if not listed):
players: {players}
teams:   {teams}
cities:  {cities}
venues:  {venues}
seasons: {seasons}
""".strip()

    # 1) Primary LLM call (JSON)
    try:
        resp = call_gemini(
            prompt,
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=4096,
            candidate_count=1,
        )
        # Log finish reason if available
        try:
            fr = None
            if hasattr(resp, "candidates") and resp.candidates:
                fr = getattr(resp.candidates[0], "finish_reason", None) or getattr(resp.candidates[0], "finishReason", None)
            elif isinstance(resp, dict) and resp.get("candidates"):
                fr = resp["candidates"][0].get("finishReason") or resp["candidates"][0].get("finish_reason")
            logging.info("[LLM_FINISH] %s", fr)
            hit_max = str(fr).endswith("MAX_TOKENS")
        except Exception:
            hit_max = False

        raw = _extract_sdk(resp) or ""
        logging.info("[LLM_RAW] len=%d preview=%s", len(raw), raw[:300])
    except Exception as e:
        logging.error("[LLM_CALL_FAIL] %s", e)
        return {"status": "error", "message": f"LLM call failed: {e}", "_debug_prompt": prompt, "_debug_raw": ""}

    # 2) If empty/MAX_TOKENS → tiny SQL-only retry
    if raw.strip() == "" or hit_max:
        logging.warning("[LLM_EMPTY/MAX_TOKENS] retrying with minimal SQL-only prompt")
        retry_prompt = f"""
You are a DuckDB SQL generator.
Return ONE runnable SQL statement only (no prose, no JSON, no fences).

Schema:
- deliveries_q(match_id, inning, over, ball_in_over, batting_team, batter, bowler,
               runs_batter, runs_total, extras_total, wides, noballs, legbyes, byes, penalty, season_year)
- matches_q(match_id, team1, team2, winner, city, venue, match_date, match_year)

Hints:
- Use matches_q.match_year for seasons.
- Economy (bowler) = SUM(runs_total) / (COUNT(*)/6) over legal balls (exclude wides and no-balls for ball count).
- Six = runs_batter = 6; Four = runs_batter = 4.
- Join deliveries_q to matches_q USING(match_id) when a season filter is needed.

Question: {question}
SQL:
""".strip()
        try:
            resp2 = call_gemini(
                retry_prompt,
                response_mime_type="text/plain",
                temperature=0.0,
                max_output_tokens=512,
                candidate_count=1,
            )
            raw2 = _extract_sdk(resp2) or ""
            logging.info("[LLM_RAW_RETRY] len=%d preview=%s", len(raw2), raw2[:300])
            if raw2.strip() == "":
                return {"status": "error", "message": "LLM returned empty output twice.", "_debug_prompt": retry_prompt, "_debug_raw": ""}

            sql2 = clean_and_validate_sql_from_text(raw2)
            return {
                "status": "ok",
                "resolutions": {},
                "sql": sql2,
                "_debug_prompt": retry_prompt,
                "_debug_raw": raw2,
                "_mode": "retry_only_sql",
            }
        except Exception as e2:
            logging.error("[LLM_RETRY_FAIL] %s", e2)
            return {"status": "error", "message": "LLM did not return valid JSON/SQL.", "_debug_prompt": retry_prompt, "_debug_raw": ""}

    # 3) Parse JSON
    obj = _extract_json_obj(raw)

    # 3a) If model asks for clarification → return that (let the route surface NLQClarify)
    if isinstance(obj, dict) and obj.get("status") == "needs_clarification":
        obj["_debug_prompt"] = prompt
        obj["_debug_raw"] = raw
        obj["_mode"] = "json_clarify"
        return obj

    # 4) If JSON invalid or missing sql → try plain-SQL salvage ONLY if it looks like SQL
    if not isinstance(obj, dict) or "status" not in obj or not obj.get("sql"):
        if re.search(r"(?is)\b(SELECT|WITH)\b", raw or ""):
            try:
                sql_from_raw = clean_and_validate_sql_from_text(raw)
                return {
                    "status": "ok",
                    "resolutions": obj.get("resolutions", {}) if isinstance(obj, dict) else {},
                    "sql": sql_from_raw,
                    "_debug_prompt": prompt,
                    "_debug_raw": raw,
                    "_mode": "raw_sql_extracted",
                }
            except Exception:
                pass
        # No SQL present: propagate a debuggable error
        return {"status": "error", "message": "LLM did not return valid JSON/SQL.", "_debug_prompt": prompt, "_debug_raw": raw}

    # 5) Success via JSON (status=ok + sql)
    obj["_debug_prompt"] = prompt
    obj["_debug_raw"] = raw
    obj["_mode"] = "json_sql"
    return obj




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
    logging.basicConfig(
        level=logging.INFO,  # or DEBUG if you want more noise
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    assert (FRONTEND_DIR / "index.html").exists(), f"Missing {FRONTEND_DIR / 'index.html'}"
    init_views()
    init_catalogs()

@app.get("/debug/candidates")
def debug_candidates(entity: str, q: str, limit: int = 8):
    """
    Try: /debug/candidates?entity=city&q=Banglore
         /debug/candidates?entity=player&q=Kohli
         /debug/candidates?entity=team&q=RCB
         /debug/candidates?entity=venue&q=chepauk
         /debug/candidates?entity=season&q=last%205%20seasons
    """
    return {"entity": entity, "q": q, "results": get_candidates(entity, q, limit)}


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
    debug: Optional[dict] = None

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

    logging.info("[NLQ_REQ] corr_id=%s q=%r debug=%s", corr_id, req.question, debug)

    # 1) LLM-first: disambiguate & plan SQL using candidate lists
    llm = generate_sql_with_candidates(req.question)

    # If the model needs the user's choice, return options to the UI
    if llm.get("status") == "needs_clarification":
        return NLQClarify(
            status="needs_clarification",
            entity=str(llm.get("entity", "")),
            mention=str(llm.get("mention", "")),
            options=[str(o) for o in llm.get("options", [])][:6],
            correlation_id=corr_id,
        )

    # --- NEW: log/show what SQL the LLM actually returned
    llm_sql = llm.get("sql") if isinstance(llm, dict) else None
    logging.info("[NLQ_SQL_RAW] corr_id=%s sql_preview=%s", corr_id, (llm_sql or "")[:300])

    # Hard error / contract violation → friendly generic message (with debug if requested)
    if llm.get("status") != "ok" or not isinstance(llm_sql, str) or not llm_sql.strip():
        logging.error(
            "[NLQ_NO_SQL] corr_id=%s status=%s msg=%s raw_preview=%s",
            corr_id,
            llm.get("status"),
            llm.get("message"),
            (llm.get("_debug_raw") or "")[:300],
        )
        return NLQError(
            status="error",
            code="NLQ_UNSUPPORTED",
            message=("LLM did not return runnable SQL." if debug
                     else "We’re working on this type of query. Try rephrasing or narrowing it."),
            correlation_id=corr_id,
            debug=({
                "llm_status": llm.get("status"),
                "llm_message": llm.get("message"),
                "prompt": llm.get("_debug_prompt"),
                "llm_raw": (llm.get("_debug_raw") or "")[:4000],
                "question": req.question
            } if debug else None),
        )

    # 2) Normalize the SQL for DuckDB
    try:
        sql = normalize_sql(llm_sql)
        logging.info("[NLQ_SQL_NORM] corr_id=%s sql_preview=%s", corr_id, sql[:300])  # NEW
    except Exception as e:
        logging.error("[NLQ_SQL_NORM_FAIL] corr_id=%s err=%s raw_sql=%s", corr_id, e, (llm_sql or "")[:300])  # NEW
        return NLQError(
            status="error",
            code="SQL_INVALID",
            message=(f"SQL normalization failed: {e}" if debug
                     else "We’re working on this type of query. Try rephrasing or narrowing it."),
            correlation_id=corr_id,
            debug=({"sql_from_llm": llm_sql, "prompt": llm.get("_debug_prompt")} if debug else None),  # NEW
        )

    # 3) Execute (add a LIMIT guard if absent)
    try:
        sql = sql.strip().rstrip(";")

        if not re.search(r"(?i)\blimit\s+\d+\b", sql):
            sql += " LIMIT 1000"
            logging.info("[NLQ_SQL_LIMITED] corr_id=%s", corr_id)  # NEW

        # Optional but very helpful: EXPLAIN
        try:
            plan = con.execute(f"EXPLAIN {sql}").fetchone()[0]  # NEW
            logging.info("[NLQ_EXPLAIN] corr_id=%s plan_preview=%s", corr_id, str(plan)[:300])  # NEW
        except Exception as e:
            logging.error("[NLQ_EXPLAIN_FAIL] corr_id=%s err=%s sql=%s", corr_id, e, sql[:300])  # NEW

        cur = con.execute(sql)
        rows = cur.fetchmany(req.max_rows)
        cols = [d[0] for d in cur.description] if cur.description else []
        logging.info("[NLQ_EXEC_OK] corr_id=%s rows=%d cols=%s", corr_id, len(rows), cols)  # NEW

    except Exception as e:
        msg = str(e)
        code = "SQL_INVALID"
        if "Binder Error" in msg or "Catalog" in msg:
            code = "SCHEMA_MISMATCH"
        elif "Interrupted" in msg or "timeout" in msg.lower():
            code = "QUERY_TIMEOUT"

        logging.error("[NLQ_EXEC_FAIL] corr_id=%s code=%s err=%s sql=%s", corr_id, code, msg, sql[:300])  # NEW
        return NLQError(
            status="error",
            code=code,
            message=(f"SQL execution failed: {msg}" if debug
                     else "We’re working on this type of query. Try rephrasing or narrowing it."),
            correlation_id=corr_id,
            debug=({"sql": sql, "question": req.question} if debug else None),  # NEW
        )

    # 4) Shape success payload
    latency = int((time.time() - start) * 1000)
    payload = NLQSuccess(
        status="ok",
        columns=cols,
        rows=[list(r) for r in rows],
        meta={
            "t_latency_ms": latency,
            "correlation_id": corr_id,
            "no_results": len(rows) == 0,
            "resolutions": llm.get("resolutions"),
        },
    )
    if len(rows) == 0:
        payload.notice = "No results found for that query. Try broadening the season, team, or player spelling."
    if debug:
        payload.sql = sql  # dev-only echo when X-Debug: 1
        # NEW: include previews so you can see prompt/raw in the response without tailing logs
        payload.meta["llm_raw_preview"] = (llm.get("_debug_raw") or "")[:2000]
        payload.meta["prompt_preview"] = (llm.get("_debug_prompt") or "")[:2000]
    return payload

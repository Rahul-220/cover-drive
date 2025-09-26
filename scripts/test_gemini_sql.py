#!/usr/bin/env python3
"""
scripts/test_gemini_sql.py

SDK-only helper to generate a single DuckDB SELECT from a natural-language question.
Used by the FastAPI backend. Requires GEMINI_API_KEY in environment.
"""

import os
import re
import sys

# Load .env for local development (no-op if not present)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ---- env / config ----
API_KEY = os.getenv("GEMINI_API_KEY")  # validated at call time
MODEL = "gemini-2.5-flash"
MAX_OUTPUT_TOKENS = 800


# ---- prompt ----
def build_prompt(user_question: str) -> str:
    return (
        "Return ONLY one valid DuckDB SQL SELECT. No prose, no code fences, no semicolon.\n"
        "Use these queryable views (stable across environments):\n"
        "deliveries_q(\n"
        "  match_id TEXT, season TEXT, season_year INT, inning INT, over INT, ball_in_over INT,\n"
        "  batting_team TEXT, batter TEXT, non_striker TEXT, bowler TEXT,\n"
        "  runs_batter INT, runs_total INT, extras_total INT, wides INT, noballs INT, legbyes INT, byes INT, penalty INT,\n"
        "  dismissal_kind TEXT, player_out TEXT, fielder TEXT, venue TEXT, city TEXT, date TEXT\n"
        ")\n"
        "matches_q(\n"
        "  match_id TEXT, season TEXT, date TEXT, competition TEXT, venue TEXT, city TEXT,\n"
        "  team1 TEXT, team2 TEXT, winner TEXT, toss_winner TEXT, toss_decision TEXT,\n"
        "  match_ts TIMESTAMP, match_date DATE, match_year INT\n"
        ")\n"
        "Rules:\n"
        "- Use deliveries_q for ball-by-ball, matches_q for match-level; join USING(match_id) if needed.\n"
        "- Year filters: prefer match_year on matches_q; or season_year on deliveries_q.\n"
        "- Wickets = dismissal_kind IN ('bowled','caught','lbw','stumped','hit wicket','caught and bowled').\n"
        "- Economy rate: runs_conceded = SUM(runs_total - byes - legbyes); legal_balls = SUM(CASE WHEN wides=0 AND noballs=0 THEN 1 ELSE 0 END); overs = legal_balls/6.0; economy = runs_conceded / NULLIF(overs,0).\n"
        "- Batter strike rate: balls_faced = SUM(CASE WHEN wides=0 AND noballs=0 THEN 1 ELSE 0 END) for that batter; strike_rate = 100.0*SUM(runs_batter)/NULLIF(balls_faced,0).\n"
        "- Sixes: runs_batter = 6.\n"
        "- Successful chase: inning=2 team equals matches_q.winner AND SUM(runs_total in inning 2) >= SUM(runs_total in inning 1); the target is inning 1 total.\n"
        "- If the question asks for 'top'/'most', include ORDER BY ... DESC and LIMIT N (default N=1 if unspecified).\n"
        "- Do NOT use 'AS' after a table name without an alias; e.g., 'FROM matches_q AS m' (not 'FROM matches_q AS').\n"
        "- Return a single SELECT (CTE WITH is allowed).\n\n"
        "Examples:\n"
        "Q: How many matches were played in Chennai in 2023?\n"
        "SQL: SELECT COUNT(*) AS matches_played FROM matches_q WHERE city='Chennai' AND match_year=2023\n"
        "Q: Which bowler took the most wickets in 2022?\n"
        "SQL: SELECT bowler, COUNT(*) AS wickets FROM deliveries_q WHERE season_year=2022 AND dismissal_kind IN ('bowled','caught','lbw','stumped','hit wicket','caught and bowled') GROUP BY bowler ORDER BY wickets DESC LIMIT 1\n"
        "Q: Which match had the highest total runs in 2023?\n"
        "SQL: SELECT d.match_id, SUM(d.runs_total) AS total_runs FROM deliveries_q d JOIN matches_q m USING(match_id) WHERE m.match_year=2023 GROUP BY d.match_id ORDER BY total_runs DESC LIMIT 1\n\n"
        f"User question: {user_question}\n\nSQL:"
    )


# ---- cleanup & safety ----
DENY_PATTERNS = [
    r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b",
    r"\bCREATE\b", r"\bDROP\b", r"\bALTER\b",
    r"\bATTACH\b", r"\bDETACH\b", r"\bPRAGMA\b",
    r"\bVACUUM\b", r"\bEXEC\b", r"\bSYSTEM\b",
    r"\bCOPY\b", r"\bLOAD\b",
]


def _strip_fences_and_semicolon(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:sql)?", "", t, flags=re.IGNORECASE).strip()
        t = re.sub(r"```$", "", t).strip()
    t = t.strip("` \n\r\t")
    if t.endswith(";"):
        t = t[:-1].strip()
    return t


def clean_and_validate_sql_from_text(text: str) -> str:
    sql = _strip_fences_and_semicolon(text)
    # try to salvage the first SELECT if present
    if not sql.lower().startswith("select"):
        m = re.search(r"(?is)\bselect\b[\s\S]+$", sql)
        if m:
            sql = m.group(0).strip()
            sql = sql.split("```")[0].strip().rstrip(";")
    if not sql or not (sql.lstrip().lower().startswith("select") or sql.lstrip().lower().startswith("with")):
        raise ValueError("Output does not start with SELECT or SQL not found.")
    if ";" in sql:
        raise ValueError("Multiple statements not allowed.")
    up = sql.upper()
    for pat in DENY_PATTERNS:
        if re.search(pat, up):
            raise ValueError(f"Disallowed SQL keyword detected: {pat}")
    return sql


# ---- SDK extraction ----
def _extract_sdk(resp) -> str:
    # output_text
    if hasattr(resp, "output_text") and resp.output_text:
        return _strip_fences_and_semicolon(resp.output_text)

    # candidates[0].content.parts[*].text
    if hasattr(resp, "candidates") and resp.candidates:
        cand = resp.candidates[0]
        if hasattr(cand, "content") and cand.content:
            parts = getattr(cand.content, "parts", None)
            if parts:
                s = "".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None)).strip()
                if s:
                    return _strip_fences_and_semicolon(s)
        if hasattr(cand, "text") and cand.text:
            return _strip_fences_and_semicolon(cand.text)

    # last resort: stringify and regex the first SELECT
    s = str(resp).strip()
    s = _strip_fences_and_semicolon(s)
    m = re.search(r"(?is)\bSELECT\b[\s\S]+$", s)
    if m:
        sql = m.group(0).strip()
        sql = re.split(r"```", sql, maxsplit=1)[0].strip()
        if sql.endswith(";"):
            sql = sql[:-1].strip()
        return sql
    return ""


# ---- LLM call (SDK only) ----
def call_gemini(
    prompt_text: str,
    *,
    api_key: str | None = None,
    model: str = MODEL,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    temperature: float = 0.2,
    candidate_count: int = 1,
    response_mime_type: str | None = None,
):
    """
    Thin wrapper for google-genai that supports both client.responses.generate
    and client.models.generate_content. Adds output control knobs.
    """
    from google import genai
    from google.genai import types as genai_types

    key = api_key or API_KEY
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    client = genai.Client(api_key=key)

    # Build a config dict once; use in either path
    cfg = {
        "temperature": temperature,
        "candidate_count": candidate_count,
        "max_output_tokens": max_output_tokens,
    }
    if response_mime_type:
        # Supported by both new responses.generate and models.generate_content
        cfg["response_mime_type"] = response_mime_type

    # Path A: responses.generate (newer API)
    if hasattr(client, "responses") and hasattr(client.responses, "generate"):
        return client.responses.generate(
            model=model,
            input=prompt_text,
            config=genai_types.GenerateConfig(**cfg),
        )

    # Path B: models.generate_content (older API)
    if hasattr(client, "models") and hasattr(client.models, "generate_content"):
        return client.models.generate_content(
            model=model,
            contents=[genai_types.Content(parts=[genai_types.Part(text=prompt_text)])],
            # models.generate_content accepts a plain dict too
            config=cfg,
        )

    raise RuntimeError("google-genai SDK does not expose a supported generate method.")



# --- PUBLIC: import this in your backend ---
def generate_sql(question: str, *, api_key: str | None = None,
                 model: str = MODEL, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """
    Natural-language question -> validated DuckDB SELECT SQL (string).
    Raises ValueError / RuntimeError on failures.
    """
    prompt = build_prompt(question)
    resp = call_gemini(prompt, api_key=api_key, model=model, max_output_tokens=max_output_tokens)
    text = _extract_sdk(resp)
    return clean_and_validate_sql_from_text(text)


def main():
    user_q = "Top run getter in Chennai for the past 3 seasons" if len(sys.argv) == 1 else " ".join(sys.argv[1:])
    print("Sending prompt")
    try:
        sql = generate_sql(user_q)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    print("LLM Checks Passed")
    print("SQL:", sql)


if __name__ == "__main__":
    main()

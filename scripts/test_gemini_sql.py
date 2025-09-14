# #!/usr/bin/env python3
# """
# scripts/test_gemini_sql.py  (SDK-only)

# Generate a DuckDB SQL query from a natural-language question using Gemini 2.5 Flash.

# Console output (minimal):
#   - "Sending prompt"
#   - "LLM Checks Passed"
#   - "SQL: <query>"

# Setup:
#   pip install google-genai python-dotenv
#   .env must contain: GEMINI_API_KEY=your_key

# Run:
#   python scripts/test_gemini_sql.py "Top run getter in Chennai for the past 3 seasons"
# """

# import os
# import re
# import sys
# from dotenv import load_dotenv

# # ---- env / config ----
# load_dotenv()
# API_KEY = os.getenv("GEMINI_API_KEY")
# if not API_KEY:
#     print("ERROR: GEMINI_API_KEY not found in environment or .env")
#     sys.exit(1)

# MODEL = "gemini-2.5-flash"
# MAX_OUTPUT_TOKENS = 800  # raise/lower as needed

# # ---- prompt ----
# def build_prompt(user_question: str) -> str:
#     return (
#         "Return ONLY one valid DuckDB SQL SELECT (no commentary).\n"
#         "Tables:\n"
#         "deliveries(match_id, season, inning, over, ball_in_over, batting_team, bowling_team, batter, non_striker, bowler, runs_batter, runs_total, extras_total, wides, noballs, legbyes, byes, penalty, dismissal_kind, player_out, fielder, venue, city, date)\n"
#         "matches(match_id, season, date, competition, venue, city, team1, team2, winner, toss_winner, toss_decision)\n"
#         "Rules:\n"
#         "- deliveries for ball-by-ball; matches for match-level; join on match_id if needed.\n"
#         "- Wickets = dismissal_kind IN ('bowled','caught','lbw','stumped','hit wicket','caught and bowled').\n"
#         "- If 'top', include ORDER BY ... DESC LIMIT N.\n"
#         "- No INSERT/UPDATE/DELETE/DDL; single statement only.\n\n"
#         f"User question: {user_question}\n\nSQL:"
#     )

# # ---- cleanup & safety ----
# DENY_PATTERNS = [
#     r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b",
#     r"\bCREATE\b", r"\bDROP\b", r"\bALTER\b",
#     r"\bATTACH\b", r"\bDETACH\b", r"\bPRAGMA\b",
#     r"\bVACUUM\b", r"\bEXEC\b", r"\bSYSTEM\b",
#     r"\bCOPY\b", r"\bLOAD\b",
# ]

# def _strip_fences_and_semicolon(text: str) -> str:
#     t = text.strip()
#     if t.startswith("```"):
#         t = re.sub(r"^```(?:sql)?", "", t, flags=re.IGNORECASE).strip()
#         t = re.sub(r"```$", "", t).strip()
#     t = t.strip("` \n\r\t")
#     if t.endswith(";"):
#         t = t[:-1].strip()
#     return t

# def clean_and_validate_sql_from_text(text: str) -> str:
#     sql = _strip_fences_and_semicolon(text)
#     if not sql or not sql.lower().startswith("select"):
#         raise ValueError("Output does not start with SELECT or SQL not found.")
#     if ";" in sql:
#         raise ValueError("Multiple statements not allowed.")
#     up = sql.upper()
#     for pat in DENY_PATTERNS:
#         if re.search(pat, up):
#             raise ValueError(f"Disallowed SQL keyword detected: {pat}")
#     return sql

# # ---- SDK extraction ----
# def _extract_sdk(resp) -> str:
#     # output_text (newer google-genai often exposes this)
#     if hasattr(resp, "output_text") and resp.output_text:
#         return _strip_fences_and_semicolon(resp.output_text)

#     # candidates[0].content.parts[*].text
#     if hasattr(resp, "candidates") and resp.candidates:
#         cand = resp.candidates[0]
#         if hasattr(cand, "content") and cand.content:
#             parts = getattr(cand.content, "parts", None)
#             if parts:
#                 s = "".join(getattr(p, "text", "") for p in parts if getattr(p, "text", None)).strip()
#                 if s:
#                     return _strip_fences_and_semicolon(s)
#         if hasattr(cand, "text") and cand.text:
#             return _strip_fences_and_semicolon(cand.text)

#     # last resort: stringify and regex the first SELECT
#     s = str(resp).strip()
#     s = _strip_fences_and_semicolon(s)
#     m = re.search(r"(?is)\bSELECT\b[\s\S]+$", s)
#     if m:
#         sql = m.group(0).strip()
#         sql = re.split(r"```", sql, maxsplit=1)[0].strip()
#         if sql.endswith(";"):
#             sql = sql[:-1].strip()
#         return sql
#     return ""

# # ---- LLM call (SDK only) ----
# def call_gemini(prompt_text: str):
#     # We keep both SDK paths (both are SDK; no REST fallback here)
#     from google import genai
#     from google.genai import types as genai_types

#     client = genai.Client(api_key=API_KEY)

#     # Path A: responses.generate
#     if hasattr(client, "responses") and hasattr(client.responses, "generate"):
#         return client.responses.generate(
#             model=MODEL,
#             input=prompt_text,
#             config=genai_types.GenerateConfig(
#                 temperature=0.0,
#                 max_output_tokens=MAX_OUTPUT_TOKENS
#             ),
#         )

#     # Path B: models.generate_content
#     if hasattr(client, "models") and hasattr(client.models, "generate_content"):
#         return client.models.generate_content(
#             model=MODEL,
#             contents=[genai_types.Content(parts=[genai_types.Part(text=prompt_text)])],
#             config={"temperature": 0.0, "max_output_tokens": MAX_OUTPUT_TOKENS},
#         )

#     # If neither method exists, the installed SDK is incompatible
#     raise RuntimeError("google-genai SDK does not expose a supported generate method.")

# # ---- main ----
# def main():
#     user_q = "Top run getter in Chennai for the past 3 seasons" if len(sys.argv) == 1 else " ".join(sys.argv[1:])
#     prompt = build_prompt(user_q)

#     print("Sending prompt")
#     resp = call_gemini(prompt)

#     text = _extract_sdk(resp)
#     sql = clean_and_validate_sql_from_text(text)

#     print("LLM Checks Passed")
#     print("SQL:", sql)

# if __name__ == "__main__":
#     main()
#!/usr/bin/env python3
"""
scripts/test_gemini_sql.py  (SDK-only, importable)

- Exported function: generate_sql(question) -> sql
- Minimal CLI output when run directly:
    Sending prompt
    LLM Checks Passed
    SQL: ...

Setup:
  pip install google-genai python-dotenv
  .env must contain: GEMINI_API_KEY=your_key

Run:
  python scripts/test_gemini_sql.py "Top run getter in Chennai for the past 3 seasons"
"""

import os
import re
import sys
from dotenv import load_dotenv

# --- env / config ---
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")  # validated at call time
MODEL = "gemini-2.5-flash"
MAX_OUTPUT_TOKENS = 800

# --- prompt ---
def build_prompt(user_question: str) -> str:
    return (
        "Return ONLY one valid DuckDB SQL SELECT (no commentary).\n"
        "Tables:\n"
        "deliveries(match_id, season, inning, over, ball_in_over, batting_team, bowling_team, batter, non_striker, bowler, runs_batter, runs_total, extras_total, wides, noballs, legbyes, byes, penalty, dismissal_kind, player_out, fielder, venue, city, date)\n"
        "matches(match_id, season, date, competition, venue, city, team1, team2, winner, toss_winner, toss_decision)\n"
        "Rules:\n"
        "- deliveries for ball-by-ball; matches for match-level; join on match_id if needed.\n"
        "- Wickets = dismissal_kind IN ('bowled','caught','lbw','stumped','hit wicket','caught and bowled').\n"
        "- If 'top', include ORDER BY ... DESC LIMIT N.\n"
        "- No INSERT/UPDATE/DELETE/DDL; single statement only.\n\n"
        f"User question: {user_question}\n\nSQL:"
    )

# --- cleanup & safety ---
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
    if not sql or not sql.lower().startswith("select"):
        raise ValueError("Output does not start with SELECT or SQL not found.")
    if ";" in sql:
        raise ValueError("Multiple statements not allowed.")
    up = sql.upper()
    for pat in DENY_PATTERNS:
        if re.search(pat, up):
            raise ValueError(f"Disallowed SQL keyword detected: {pat}")
    return sql

# --- SDK extraction ---
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

# --- LLM call (SDK only) ---
def call_gemini(prompt_text: str, *, api_key: str | None = None,
                model: str = MODEL, max_output_tokens: int = MAX_OUTPUT_TOKENS):
    from google import genai
    from google.genai import types as genai_types

    key = api_key or API_KEY
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    client = genai.Client(api_key=key)

    # Path A: responses.generate
    if hasattr(client, "responses") and hasattr(client.responses, "generate"):
        return client.responses.generate(
            model=model,
            input=prompt_text,
            config=genai_types.GenerateConfig(
                temperature=0.0,
                max_output_tokens=max_output_tokens
            ),
        )

    # Path B: models.generate_content
    if hasattr(client, "models") and hasattr(client.models, "generate_content"):
        return client.models.generate_content(
            model=model,
            contents=[genai_types.Content(parts=[genai_types.Part(text=prompt_text)])],
            config={"temperature": 0.0, "max_output_tokens": max_output_tokens},
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

# --- CLI entrypoint (unchanged minimal prints) ---
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


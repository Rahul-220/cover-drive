# backend/entity_resolver.py
from __future__ import annotations
from typing import Dict, List, Tuple
import duckdb, re
from rapidfuzz import process, fuzz

SUPPORTED_KINDS = ["team", "city", "venue", "player"]

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _extract_candidates(question: str) -> List[str]:
    quotes = re.findall(r'"([^"]+)"', question) + re.findall(r"'([^']+)'", question)
    caps   = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b", question)
    specials = []
    for w in ["mi","rcb","csk","gt","srh","kkr","dc","dd","lsg","kxp","kxip","pbks"]:
        if re.search(rf"\b{w}\b", question, flags=re.I):
            specials.append(w)
    seen, out = set(), []
    for x in quotes + caps + specials:
        xl = x.lower()
        if xl not in seen:
            out.append(x)
            seen.add(xl)
    return out or [question]

def _get_catalog(con: duckdb.DuckDBPyConnection) -> Dict[str, List[str]]:
    # canonical choices per kind
    rows = con.execute("SELECT kind, canonical FROM canonical_catalog").fetchall()
    cat: Dict[str, List[str]] = {k: [] for k in SUPPORTED_KINDS}
    for kind, canonical in rows:
        if canonical and canonical not in cat[kind]:
            cat[kind].append(canonical)
    # add player aliases to help nickname matching
    pals = con.execute("SELECT canonical, alias FROM player_alias_view").fetchall()
    for canonical, alias in pals:
        if alias and alias not in cat["player"]:
            cat["player"].append(alias)
        if canonical and canonical not in cat["player"]:
            cat["player"].append(canonical)
    return cat

def resolve_entities(
    con: duckdb.DuckDBPyConnection,
    question: str,
    high: int = 92,
    mid: int = 85,
) -> Dict:
    """
    Returns dict:
      resolved: {kind: [canonical,...]}
      clarify:  [{kind,label,options}]
      unresolved: [raw terms]
    """
    candidates = _extract_candidates(question)
    catalog = _get_catalog(con)

    resolved: Dict[str, List[str]] = {k: [] for k in SUPPORTED_KINDS}
    clarify: List[Dict] = []
    unresolved: List[str] = []

    for cand in candidates:
        c = cand
        c_norm = _norm(c)

        # exact canonical across kinds
        exact = False
        for kind in SUPPORTED_KINDS:
            if any(_norm(x) == c_norm for x in catalog.get(kind, [])):
                resolved[kind].append(c)
                exact = True
                break
        if exact:
            continue

        # fuzzy per kind
        any_hit = False
        for kind in SUPPORTED_KINDS:
            choices = catalog.get(kind, [])
            if not choices:
                continue
            matches = process.extract(c, choices, scorer=fuzz.token_set_ratio, limit=5, score_cutoff=mid)
            if not matches:
                continue
            any_hit = True
            matches.sort(key=lambda x: x[1], reverse=True)
            top = matches[0]
            if top[1] >= high and (len(matches) == 1 or (top[1] - matches[1][1] >= 5)):
                resolved[kind].append(top[0])
            else:
                options = list(dict.fromkeys([m[0] for m in matches[:3]]))
                label = "Which player?" if kind == "player" else f"Which {kind}?"
                clarify.append({"kind": kind, "label": label, "options": options})

        if not any_hit:
            unresolved.append(c)

    # de-dup lists
    for k in resolved:
        seen = set()
        uniq = []
        for v in resolved[k]:
            if v not in seen:
                uniq.append(v); seen.add(v)
        resolved[k] = uniq

    return {"resolved": resolved, "clarify": clarify, "unresolved": unresolved}

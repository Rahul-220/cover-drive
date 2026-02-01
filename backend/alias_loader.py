# backend/alias_loader.py
from __future__ import annotations
from pathlib import Path
from typing import Optional
import duckdb
import pandas as pd
import logging
import re

ALIASES_SHEET_NAME = "aliases"             # (team/city/venue)
PLAYER_SHEET_NAME  = "player_nicknames"    # preferred
# We will accept also: "players_nicknames"

# ---------------------------
# Helpers
# ---------------------------
def _lower_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().lower() for c in df.columns]
    return df

def _strip_str(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df

def _coerce_year(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("Int64")

def _register_and_create(con: duckdb.DuckDBPyConnection, table_name: str, df: pd.DataFrame) -> None:
    view_name = f"__tmp_{table_name}"
    con.register(view_name, df)
    try:
        con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM {view_name}")
    finally:
        try:
            con.unregister(view_name)
        except Exception:
            pass

def _find_sheet(xls: pd.ExcelFile, preferred: str, fallback_index: int) -> str:
    for name in xls.sheet_names:
        if name.strip().lower() == preferred.strip().lower():
            return name
    if 0 <= fallback_index < len(xls.sheet_names):
        return xls.sheet_names[fallback_index]
    raise ValueError(f"No sheet found for preferred='{preferred}' and invalid fallback_index={fallback_index}.")

def _find_player_sheet(xls: pd.ExcelFile) -> Optional[str]:
    # 1) perfect match
    for name in xls.sheet_names:
        if name.strip().lower() == PLAYER_SHEET_NAME:
            return name
    # 2) common variant: "players_nicknames"
    for name in xls.sheet_names:
        if name.strip().lower() == "players_nicknames":
            return name
    # 3) heuristic: any sheet name containing both "player" and "nick"
    for name in xls.sheet_names:
        lo = name.strip().lower()
        if ("player" in lo) and ("nick" in lo):
            return name
    # 4) fallback to 2nd sheet if it looks right
    if len(xls.sheet_names) >= 2:
        return xls.sheet_names[1]
    return None

# ---------------------------
# Main loaders
# ---------------------------
def load_aliases_from_excel(
    con: duckdb.DuckDBPyConnection,
    excel_path: Path,
) -> None:
    """
    Expect one Excel file (e.g., data/teams_alias.xlsx) with:
      • Sheet 'aliases' (preferred) or first sheet:
           entity, alias_label, canonical_label, valid_from, valid_to
      • Sheet 'player_nicknames' (preferred) or variants:
           full_name, alias, [type]
    """
    xls = pd.ExcelFile(excel_path)

    # ---------- SHEET 1: teams/cities/venues ----------
    s1_name = _find_sheet(xls, ALIASES_SHEET_NAME, fallback_index=0)
    main = pd.read_excel(xls, s1_name)
    main = _lower_cols(main)

    required = {"entity", "alias_label", "canonical_label", "valid_from", "valid_to"}
    missing = required - set(main.columns)
    if missing:
        raise ValueError(f"[alias] Missing columns on '{s1_name}': {missing}")

    main["entity"] = main["entity"].astype(str).str.strip().str.lower()
    main = _strip_str(main, ["alias_label", "canonical_label"])
    main["valid_from"] = _coerce_year(main["valid_from"])
    main["valid_to"]   = _coerce_year(main["valid_to"])

    teams  = main[main["entity"].eq("team")].copy()
    cities = main[main["entity"].eq("city")].copy()
    venues = main[main["entity"].eq("venue")].copy()

    _register_and_create(con, "alias_teams",  teams)
    _register_and_create(con, "alias_cities", cities)
    _register_and_create(con, "alias_venues", venues)

    con.execute("CREATE INDEX IF NOT EXISTS idx_alias_teams_alias  ON alias_teams(lower(alias_label))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_alias_cities_alias ON alias_cities(lower(alias_label))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_alias_venues_alias ON alias_venues(lower(alias_label))")

    logging.info(f"[alias] Loaded aliases from '{s1_name}': teams={len(teams)}, cities={len(cities)}, venues={len(venues)}")

    # ---------- SHEET 2: player nicknames (optional) ----------
    p_sheet: Optional[str] = _find_player_sheet(xls)

    if p_sheet:
        try:
            nick = pd.read_excel(xls, p_sheet)
            nick = _lower_cols(nick)
            need = {"full_name", "alias"}
            if need.issubset(set(nick.columns)):
                if "type" not in nick.columns:
                    nick["type"] = "nickname"
                nick = _strip_str(nick, ["full_name", "alias", "type"])
                nick = nick[["full_name", "alias", "type"]]
                _register_and_create(con, "player_alias", nick)
                con.execute("CREATE INDEX IF NOT EXISTS idx_player_alias_alias ON player_alias(lower(alias))")
                logging.info(f"[alias] Loaded player aliases from '{p_sheet}': {len(nick)} rows")
            else:
                logging.warning(f"[alias] Sheet '{p_sheet}' doesn't look like player nicknames; columns present={list(nick.columns)}. Creating empty player_alias.")
                con.execute("""
                    CREATE TABLE IF NOT EXISTS player_alias (
                      full_name VARCHAR,
                      alias     VARCHAR,
                      type      VARCHAR
                    )
                """)
        except Exception as e:
            logging.warning(f"[alias] Skipping player nicknames sheet '{p_sheet}': {e}")
            con.execute("""
                CREATE TABLE IF NOT EXISTS player_alias (
                  full_name VARCHAR,
                  alias     VARCHAR,
                  type      VARCHAR
                )
            """)
    else:
        logging.info("[alias] No player nicknames sheet found; creating empty player_alias.")
        con.execute("""
            CREATE TABLE IF NOT EXISTS player_alias (
              full_name VARCHAR,
              alias     VARCHAR,
              type      VARCHAR
            )
        """)

    # ---------- Unified views for resolver ----------
    con.execute("""
        CREATE OR REPLACE VIEW alias_all AS
        SELECT 'team'  AS kind, alias_label AS alias, canonical_label AS canonical, valid_from, valid_to FROM alias_teams
        UNION ALL
        SELECT 'city'  AS kind, alias_label, canonical_label, valid_from, valid_to FROM alias_cities
        UNION ALL
        SELECT 'venue' AS kind, alias_label, canonical_label, valid_from, valid_to FROM alias_venues
    """)
    # Players (aliases only; canonicals come from full_name)
    con.execute("""
        CREATE OR REPLACE VIEW player_alias_view AS
        SELECT 'player' AS kind, alias AS alias, full_name AS canonical FROM player_alias
    """)
    con.execute("""
        CREATE OR REPLACE VIEW canonical_catalog AS
        SELECT kind, canonical FROM (
            SELECT 'team' AS kind, canonical_label AS canonical FROM alias_teams
            UNION ALL
            SELECT 'city',  canonical_label FROM alias_cities
            UNION ALL
            SELECT 'venue', canonical_label FROM alias_venues
            UNION ALL
            SELECT 'player', full_name FROM player_alias
        )
        GROUP BY kind, canonical
    """)

def ensure_player_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS players AS
        WITH names AS (
          SELECT batter AS full_name FROM deliveries_src WHERE batter IS NOT NULL
          UNION ALL SELECT bowler FROM deliveries_src WHERE bowler IS NOT NULL
          UNION ALL SELECT non_striker FROM deliveries_src WHERE non_striker IS NOT NULL
        )
        SELECT DISTINCT
          full_name,
          CASE WHEN strpos(full_name,' ')>0 THEN split_part(full_name,' ',1) ELSE full_name END AS first_name,
          CASE WHEN strpos(full_name,' ')>0 THEN split_part(full_name,' ',-1) ELSE full_name END AS last_name
        FROM names
        WHERE length(trim(full_name))>0
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS player_roster AS
        SELECT DISTINCT
          batter       AS full_name,
          COALESCE(
            TRY_CAST(season AS INTEGER),
            TRY_CAST(NULLIF(regexp_extract(CAST(season AS VARCHAR), '(20[0-3][0-9])', 1), '') AS INTEGER)
          )            AS season,
          batting_team AS team
        FROM deliveries_src
        WHERE batter IS NOT NULL
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_players_name  ON players(lower(full_name))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_roster_name   ON player_roster(lower(full_name))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_roster_season ON player_roster(season)")

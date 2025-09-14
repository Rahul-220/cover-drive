# etl/parquet_etl.py
from pathlib import Path
import shutil
import json
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from .io_paths import FILTERED_DIR, PARQUET_DIR

def safe_int(x):
    try:
        return int(x)
    except Exception:
        return None

def run_parquet_etl():
    matches_rows = []
    deliveries_rows = []

    # iterate seasons in data/filtered/<season>/
    for season_dir in FILTERED_DIR.iterdir():
        if not season_dir.is_dir():
            continue
        for f in season_dir.glob("*.json"):
            with f.open("r", encoding="utf-8") as fh:
                match = json.load(fh)

            info = match.get("info", {}) or {}
            season = str(info.get("season")).strip() if info.get("season") is not None else None
            match_id = f.stem

            # --- matches row ---
            outcome = info.get("outcome", {}) or {}
            toss = info.get("toss", {}) or {}
            matches_rows.append({
                "match_id": match_id,
                "season": season,  # <- keep as string for partitioning
                "date": (info.get("dates") or [None])[0],
                "competition": info.get("event", {}).get("name") or info.get("competition"),
                "venue": info.get("venue"),
                "city": info.get("city"),
                "team1": (info.get("teams") or [None, None])[0],
                "team2": (info.get("teams") or [None, None])[1],
                "winner": outcome.get("winner"),
                "toss_winner": toss.get("winner"),
                "toss_decision": toss.get("decision"),
            })

            # --- deliveries rows ---
            for inn_idx, inn in enumerate(match.get("innings", []) or [], start=1):
                batting_team = inn.get("team")
                for over_blk in inn.get("overs", []) or []:
                    over_no = safe_int(over_blk.get("over"))
                    for d in over_blk.get("deliveries", []) or []:
                        runs = d.get("runs", {}) or {}
                        extras_map = d.get("extras", {}) or {}
                        wickets = d.get("wickets", []) or []
                        wk = wickets[0] if wickets else {}
                        fielder = None
                        if wk.get("fielders"):
                            f0 = wk["fielders"][0]
                            fielder = f0.get("name") if isinstance(f0, dict) else f0

                        deliveries_rows.append({
                            "match_id": match_id,
                            "season": season,  # <- keep as string for partitioning
                            "inning": inn_idx,
                            "over": over_no,
                            "ball_in_over": safe_int(d.get("ball")),
                            "batting_team": batting_team,
                            "batter": d.get("batter"),
                            "non_striker": d.get("non_striker"),
                            "bowler": d.get("bowler"),
                            "runs_batter": runs.get("batter", 0),
                            "runs_total": runs.get("total", 0),
                            "extras_total": runs.get("extras", 0),
                            "wides": extras_map.get("wides", 0),
                            "noballs": extras_map.get("noballs", 0),
                            "legbyes": extras_map.get("legbyes", 0),
                            "byes": extras_map.get("byes", 0),
                            "penalty": extras_map.get("penalty", 0),
                            "dismissal_kind": wk.get("kind"),
                            "player_out": wk.get("player_out"),
                            "fielder": fielder,
                            "venue": info.get("venue"),
                            "city": info.get("city"),
                            "date": (info.get("dates") or [None])[0],
                        })

    # --- build DataFrames ---
    matches_df = pd.DataFrame(matches_rows).drop_duplicates(subset=["match_id"])
    deliveries_df = pd.DataFrame(deliveries_rows)

    # enforce numeric types
    int_cols = ["runs_batter","runs_total","extras_total","wides","noballs","legbyes","byes","penalty","inning","over","ball_in_over"]
    for col in int_cols:
        if col in deliveries_df.columns:
            deliveries_df[col] = pd.to_numeric(deliveries_df[col], errors="coerce").fillna(0).astype("int64")

    # --- clean existing parquet dirs (so we don't mix old layout) ---
    for sub in ("matches", "deliveries"):
        p = PARQUET_DIR / sub
        if p.exists():
            shutil.rmtree(p)

    (PARQUET_DIR / "matches").mkdir(parents=True, exist_ok=True)
    (PARQUET_DIR / "deliveries").mkdir(parents=True, exist_ok=True)

    # --- write with HIVE partitioning (season=YYYY) ---
    hive_part = ds.partitioning(
        flavor="hive",
        schema=pa.schema([pa.field("season", pa.string())])
    )

    ds.write_dataset(
        pa.Table.from_pandas(matches_df),
        base_dir=str(PARQUET_DIR / "matches"),
        format="parquet",
        partitioning=hive_part,
        existing_data_behavior="overwrite_or_ignore",
    )
    ds.write_dataset(
        pa.Table.from_pandas(deliveries_df),
        base_dir=str(PARQUET_DIR / "deliveries"),
        format="parquet",
        partitioning=hive_part,
        existing_data_behavior="overwrite_or_ignore",
    )

    print("✅ Wrote HIVE-partitioned Parquet to", PARQUET_DIR.resolve())

if __name__ == "__main__":
    run_parquet_etl()

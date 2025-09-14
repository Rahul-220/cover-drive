# etl/io_paths.py
from pathlib import Path

# project root = parent of this /etl/ folder
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

RAW_DIR = DATA/ "raw"         # unzipped Cricsheet JSON (all seasons)
FILTERED_DIR = DATA / "filtered"  # last 5 seasons (your Option A data lives here)
PARQUET_DIR = DATA / "parquet"    # output Parquet

# make sure these exist (won't overwrite anything)
FILTERED_DIR.mkdir(parents=True, exist_ok=True)
PARQUET_DIR.mkdir(parents=True, exist_ok=True)

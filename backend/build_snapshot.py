# backend/build_snapshot.py

import argparse
from pathlib import Path
import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARQUET_DELIVERIES = (PROJECT_ROOT / "data" / "parquet" / "deliveries" / "season=*" / "**" / "*.parquet").as_posix()
PARQUET_MATCHES    = (PROJECT_ROOT / "data" / "parquet" / "matches"    / "season=*" / "**" / "*.parquet").as_posix()


def build_snapshot(db_path: Path, vacuum: bool = True) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))

    # Create/refresh base tables from Parquet
    con.execute(
        f"""
        CREATE OR REPLACE TABLE deliveries AS
        SELECT * FROM read_parquet('{PARQUET_DELIVERIES}');
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE matches AS
        SELECT * FROM read_parquet('{PARQUET_MATCHES}');
        """
    )

    # Optional maintenance
    if vacuum:
        try:
            con.execute("VACUUM")
        except Exception:
            pass

    # Print simple counts
    deliveries_cnt = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    matches_cnt = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"Snapshot written to {db_path}")
    print(f"deliveries: {deliveries_cnt} rows")
    print(f"matches: {matches_cnt} rows")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build or refresh ipl.duckdb snapshot from Parquet")
    ap.add_argument("--db", dest="db", default=str(PROJECT_ROOT / "data" / "ipl.duckdb"), help="Output DuckDB path")
    ap.add_argument("--no-vacuum", dest="vacuum", action="store_false", help="Disable VACUUM step")
    args = ap.parse_args()

    build_snapshot(Path(args.db), vacuum=args.vacuum)


if __name__ == "__main__":
    main()


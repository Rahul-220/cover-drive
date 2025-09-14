#!/usr/bin/env python3

import sys
import json
import argparse
from pathlib import Path

import duckdb

# --- Configure your project paths (Hive-partitioned Parquet expected) ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARQUET_DELIVERIES = (PROJECT_ROOT / "data" / "parquet" / "deliveries" / "season=*" / "**" / "*.parquet").as_posix()
PARQUET_MATCHES    = (PROJECT_ROOT / "data" / "parquet" / "matches"    / "season=*" / "**" / "*.parquet").as_posix()

def make_connection():
    con = duckdb.connect()  # in-memory
    # Create views that point to your Parquet so SQL can use table names directly
    con.execute(f"CREATE OR REPLACE VIEW deliveries AS SELECT * FROM read_parquet('{PARQUET_DELIVERIES}')")
    con.execute(f"CREATE OR REPLACE VIEW matches    AS SELECT * FROM read_parquet('{PARQUET_MATCHES}')")
    return con

def read_sql_from_args(sql_arg: str | None, file_arg: str | None) -> str:
    if sql_arg and sql_arg.strip() == "-":
        # read from stdin
        return sys.stdin.read()
    if sql_arg:
        return sql_arg
    if file_arg:
        p = Path(file_arg)
        if not p.exists():
            raise FileNotFoundError(f"SQL file not found: {p}")
        return p.read_text(encoding="utf-8")
    raise ValueError("Provide --sql \"...\" or --file path.sql")

def run_query(con: duckdb.DuckDBPyConnection, sql: str, max_rows: int | None):
    # Basic guard: force single statement
    if ";" in sql.strip():
        # allow a trailing semicolon but not multiple statements
        if sql.strip().count(";") > 1 or not sql.strip().endswith(";"):
            raise ValueError("Only a single SQL statement is allowed.")
        sql = sql.strip()[:-1]  # remove trailing semicolon

    cur = con.execute(sql)
    if max_rows is not None:
        rows = cur.fetchmany(max_rows)
    else:
        rows = cur.fetchall()

    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, rows

def print_table(cols, rows):
    if not cols:
        print("(no columns)")
        return
    # simple pretty table
    widths = [len(c) for c in cols]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)) if cell is not None else 4)

    def fmt_row(r):
        return " | ".join(str(cell if cell is not None else "NULL").ljust(widths[i]) for i, cell in enumerate(r))

    sep = "-+-".join("-" * w for w in widths)
    print(fmt_row(cols))
    print(sep)
    for r in rows:
        print(fmt_row(r))

def print_json(cols, rows):
    objs = [dict(zip(cols, r)) for r in rows]
    print(json.dumps(objs, indent=2, ensure_ascii=False))

def main():
    ap = argparse.ArgumentParser(description="Run DuckDB SQL against IPL Parquet (deliveries/matches views).")
    ap.add_argument("--sql",  help='SQL string, or "-" to read from STDIN')
    ap.add_argument("--file", help="Path to a .sql file")
    ap.add_argument("--format", choices=["table","json"], default="table", help="Output format (default: table)")
    ap.add_argument("--max-rows", type=int, default=None, help="Cap the number of returned rows")
    args = ap.parse_args()

    try:
        sql = read_sql_from_args(args.sql, args.file)
    except Exception as e:
        print(f"Error reading SQL: {e}")
        sys.exit(2)

    try:
        con = make_connection()
        cols, rows = run_query(con, sql, args.max_rows)
    except Exception as e:
        print(f"Query failed: {e}")
        sys.exit(3)

    if args.format == "json":
        print_json(cols, rows)
    else:
        print_table(cols, rows)

if __name__ == "__main__":
    main()

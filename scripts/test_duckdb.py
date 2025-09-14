import duckdb

# connect to an in-memory DuckDB instance
con = duckdb.connect()

# count total deliveries in 2021
deliveries_count = con.execute("""
    SELECT COUNT(*)
    FROM read_parquet('data/parquet/deliveries/season=2021/*.parquet')
""").fetchone()[0]
print("Total deliveries in 2021:", deliveries_count)

# top 5 batters by runs in 2021
top_batters = con.execute("""
    SELECT batter, SUM(runs_batter) AS runs
    FROM read_parquet('data/parquet/deliveries/season=2021/*.parquet')
    GROUP BY batter
    ORDER BY runs DESC
    LIMIT 5
""").fetchall()
print("Top 5 batters in 2021:", top_batters)

# (optional) top 5 bowlers by wickets in 2021
top_bowlers = con.execute(""" 
    SELECT bowler, COUNT(*) AS wickets
    FROM read_parquet('data/parquet/deliveries/season=2021/*.parquet')
    WHERE dismissal_kind IN ('bowled','caught','lbw','stumped','hit wicket','caught and bowled')
    AND inning IN (1,2)  -- optional: ignore super overs
    GROUP BY bowler
    ORDER BY wickets DESC
    LIMIT 5;
""").fetchall()
print("Top 5 bowlers in 2021:", top_bowlers)

top_5_bowlers = con.execute(""" 
    SELECT bowler, COUNT(*) AS wickets
    FROM read_parquet('data/parquet/deliveries/*/*.parquet')
    WHERE season = '2022'
    AND dismissal_kind IN ('bowled','caught','lbw','stumped','hit wicket','caught and bowled')
    GROUP BY bowler
    ORDER BY wickets DESC
    LIMIT 5;
""").fetchall()
print("Top 5 bowlers in 2022:", top_5_bowlers)

top_chennai_run_getter = con.execute(""" 
    WITH last_seasons AS (
        SELECT DISTINCT season
        FROM read_parquet('data/parquet/matches/*/*.parquet')
        ORDER BY season DESC
        LIMIT 3
    )
    SELECT d.batter, SUM(d.runs_batter) AS runs
    FROM read_parquet('data/parquet/deliveries/*/*.parquet') d
    JOIN read_parquet('data/parquet/matches/*/*.parquet') m USING (match_id)
    WHERE d.season IN (SELECT season FROM last_seasons)
    AND m.city = 'Chennai'
    GROUP BY d.batter
    ORDER BY runs DESC
    LIMIT 1;
""").fetchall()
print("Top 5 bowlers in 2022:", top_chennai_run_getter)



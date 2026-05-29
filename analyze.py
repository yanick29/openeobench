#!/usr/bin/env python3
"""
Analyze benchmark runs from benchmark_results.duckdb.

Compares CRS strategies (onthefly vs. local_preprocessing) using
median-based statistics (robust to cloud variance) with bootstrap 95% CIs.

Usage:
    python analyze.py
    python analyze.py --csv results.csv
    python analyze.py --group-by region
    python analyze.py --db path/to/benchmark_results.duckdb
"""

import argparse
import csv
import random
import statistics
import sys
from collections import defaultdict

import duckdb

REGIONS = ("berlin", "hamburg")
TIMING_FIELDS = ("total_time", "queue_time", "processing_time", "preprocessing_time")
BOOTSTRAP_ITERS = 2000


def detect_region(scenario: str) -> str:
    """Derive region from scenario name; falls back to 'unknown'."""
    s = (scenario or "").lower()
    for region in REGIONS:
        if region in s:
            return region
    return "unknown"


def fetch_runs(db_path: str):
    con = duckdb.connect(db_path, read_only=True)
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    if "runs" not in tables:
        raise SystemExit(
            f"Table 'runs' not found in {db_path}. Available tables: {tables or 'none'}"
        )
    col_names = [
        "run_id", "scenario", "crs_strategy", "run_type", "status",
        "queue_time", "processing_time", "preprocessing_time", "total_time",
        "credits", "cpu_seconds", "duration_backend", "input_pixels_mp",
        "max_memory_gb", "output_crs", "timestamp",
    ]
    rows = con.execute(
        f"SELECT {','.join(col_names)} FROM runs WHERE status = 'success'"
    ).fetchall()
    return [dict(zip(col_names, r)) for r in rows]


def fetch_accuracy(db_path: str):
    """Return {run_id: {'rmse': median_rmse, 'mae': median_mae}} or {} if table missing/empty.

    If a run has multiple accuracy rows, use the per-run median.
    """
    con = duckdb.connect(db_path, read_only=True)
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    if "accuracy" not in tables:
        return {}
    cols = {r[1] for r in con.execute("PRAGMA table_info('accuracy')").fetchall()}
    if "run_id" not in cols or "rmse" not in cols:
        return {}
    mae_expr = "mae" if "mae" in cols else "NULL"
    rows = con.execute(
        f"SELECT run_id, rmse, {mae_expr} AS mae FROM accuracy WHERE run_id IS NOT NULL"
    ).fetchall()
    grouped = defaultdict(lambda: {"rmse": [], "mae": []})
    for run_id, rmse, mae in rows:
        if rmse is not None:
            grouped[run_id]["rmse"].append(rmse)
        if mae is not None:
            grouped[run_id]["mae"].append(mae)
    return {
        rid: {
            "rmse": statistics.median(v["rmse"]) if v["rmse"] else None,
            "mae": statistics.median(v["mae"]) if v["mae"] else None,
        }
        for rid, v in grouped.items()
    }


def bootstrap_median_ci(values, iters=BOOTSTRAP_ITERS, conf=0.95):
    """Percentile-bootstrap CI for the median. Returns (low, high) or (None, None)."""
    n = len(values)
    if n < 2:
        return (None, None)
    rng = random.Random(42)
    medians = []
    for _ in range(iters):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()
    alpha = (1 - conf) / 2
    lo_idx = max(0, int(alpha * iters))
    hi_idx = min(iters - 1, int((1 - alpha) * iters))
    return (medians[lo_idx], medians[hi_idx])


def summarize(runs, accuracy_map=None):
    """Return a dict of metrics for a set of runs."""
    clean = [r for r in runs if r.get("total_time") is not None]
    n = len(clean)
    if n == 0:
        return None

    total_times = [r["total_time"] for r in clean]
    ci_lo, ci_hi = bootstrap_median_ci(total_times)

    cold = sum(1 for r in clean if (r.get("run_type") or "").lower() == "cold")
    hot = sum(1 for r in clean if (r.get("run_type") or "").lower() == "hot")
    credits = sum((r.get("credits") or 0) for r in clean)

    def med(field):
        vals = [r[field] for r in clean if r.get(field) is not None]
        return statistics.median(vals) if vals else None

    rmse_median = mae_median = None
    if accuracy_map:
        rmses = [accuracy_map[r["run_id"]]["rmse"]
                 for r in clean
                 if r["run_id"] in accuracy_map
                 and accuracy_map[r["run_id"]]["rmse"] is not None]
        maes = [accuracy_map[r["run_id"]]["mae"]
                for r in clean
                if r["run_id"] in accuracy_map
                and accuracy_map[r["run_id"]]["mae"] is not None]
        rmse_median = statistics.median(rmses) if rmses else None
        mae_median = statistics.median(maes) if maes else None

    return {
        "n": n,
        "n_cold": cold,
        "n_hot": hot,
        "total_median": statistics.median(total_times),
        "total_min": min(total_times),
        "total_max": max(total_times),
        "total_ci_low": ci_lo,
        "total_ci_high": ci_hi,
        "queue_median": med("queue_time"),
        "processing_median": med("processing_time"),
        "preprocessing_median": med("preprocessing_time"),
        "credits_total": credits,
        "rmse_median": rmse_median,
        "mae_median": mae_median,
    }


def fmt(v, prec=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{prec}f}"
    return str(v)


def print_table(group_label, strategy_metrics):
    """Print a side-by-side comparison table for one group."""
    strategies = sorted(strategy_metrics.keys())
    print(f"\n=== {group_label} ===")
    if not strategies:
        print("  (no data)")
        return

    rows = [
        ("Runs (cold/hot)",      lambda m: f"{m['n']} ({m['n_cold']}/{m['n_hot']})"),
        ("total_time median [s]", lambda m: fmt(m["total_median"])),
        ("  95% CI",              lambda m: f"[{fmt(m['total_ci_low'])}, {fmt(m['total_ci_high'])}]"),
        ("  min / max [s]",       lambda m: f"{fmt(m['total_min'])} / {fmt(m['total_max'])}"),
        ("queue median [s]",      lambda m: fmt(m["queue_median"])),
        ("processing median [s]", lambda m: fmt(m["processing_median"])),
        ("preprocessing median [s]", lambda m: fmt(m["preprocessing_median"])),
        ("credits (sum)",         lambda m: fmt(m["credits_total"], 2)),
        ("RMSE median",           lambda m: fmt(m.get("rmse_median"), 4) if m.get("rmse_median") is not None else "-"),
        ("MAE median",            lambda m: fmt(m.get("mae_median"), 4) if m.get("mae_median") is not None else "-"),
    ]

    label_w = max(len(r[0]) for r in rows) + 2
    col_w = max(24, max(len(s) for s in strategies) + 2)

    header = "Metric".ljust(label_w) + "".join(s.ljust(col_w) for s in strategies)
    print(header)
    print("-" * len(header))
    for label, getter in rows:
        line = label.ljust(label_w)
        for s in strategies:
            line += getter(strategy_metrics[s]).ljust(col_w)
        print(line)


def write_csv(path, all_results):
    """Write per-group, per-strategy rows to CSV."""
    fields = [
        "group", "strategy", "n", "n_cold", "n_hot",
        "total_median", "total_ci_low", "total_ci_high",
        "total_min", "total_max",
        "queue_median", "processing_median", "preprocessing_median",
        "credits_total", "rmse_median", "mae_median",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for group_label, strategy_metrics in all_results:
            for strategy, m in sorted(strategy_metrics.items()):
                row = {"group": group_label, "strategy": strategy}
                row.update({k: m.get(k) for k in fields if k not in ("group", "strategy")})
                w.writerow(row)
    print(f"\nCSV written: {path}")


def main():
    parser = argparse.ArgumentParser(description="Analyze openEO benchmark runs.")
    parser.add_argument("--db", default="benchmark_results.duckdb",
                        help="Path to DuckDB file (default: benchmark_results.duckdb)")
    parser.add_argument("--csv", default=None, help="Optional CSV output path")
    parser.add_argument("--group-by", choices=("none", "region"), default="none",
                        help="Group strategies by region (derived from scenario name)")
    parser.add_argument("--region", default=None,
                        help="Filter to a single region (e.g. berlin, hamburg)")
    args = parser.parse_args()

    runs = fetch_runs(args.db)
    if not runs:
        print("No successful runs found.", file=sys.stderr)
        return 1

    accuracy_map = fetch_accuracy(args.db)

    for r in runs:
        r["region"] = detect_region(r["scenario"])

    if args.region:
        runs = [r for r in runs if r["region"] == args.region.lower()]
        if not runs:
            print(f"No runs for region '{args.region}'.", file=sys.stderr)
            return 1

    results = []

    # Overall
    overall = defaultdict(list)
    for r in runs:
        overall[r["crs_strategy"] or "unknown"].append(r)
    overall_metrics = {s: summarize(rs, accuracy_map) for s, rs in overall.items()}
    overall_metrics = {s: m for s, m in overall_metrics.items() if m}
    results.append(("Overall", overall_metrics))

    if args.group_by == "region":
        by_region = defaultdict(lambda: defaultdict(list))
        for r in runs:
            by_region[r["region"]][r["crs_strategy"] or "unknown"].append(r)
        for region in sorted(by_region.keys()):
            metrics = {s: summarize(rs, accuracy_map) for s, rs in by_region[region].items()}
            metrics = {s: m for s, m in metrics.items() if m}
            if metrics:
                results.append((f"Region: {region}", metrics))

    for label, metrics in results:
        print_table(label, metrics)

    if args.csv:
        write_csv(args.csv, results)

    return 0


if __name__ == "__main__":
    sys.exit(main())

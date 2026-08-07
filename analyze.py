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

TIMING_FIELDS = ("total_time", "queue_time", "processing_time", "preprocessing_time",
                 "dem_download_time")
BOOTSTRAP_ITERS = 2000
NON_REGION_TOKENS = {"laea", "test"}

# Default Ziel-UTM-EPSG pro Region (Spiegel von REGIONS in run_benchmark.py).
# Wird als Backward-Compat-Fallback fuer Runs benutzt die noch kein
# target_crs in der DB haben.
REGION_DEFAULT_EPSG = {
    "amsterdam": 32631, "berlin":   32633, "hamburg":  32632,
    "kapstadt":  32734, "newyork":  32618, "rom":      32633,
    "tokio":     32654, "wien":     32633, "zuerich":  32632,
}


def detect_region(scenario: str) -> str:
    """Derive region from the last underscore-separated token of the scenario name."""
    s = (scenario or "").lower().strip()
    if not s or "_" not in s:
        return "unknown"
    last = s.rsplit("_", 1)[-1]
    if not last or last.isdigit() or last in NON_REGION_TOKENS:
        return "unknown"
    return last


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
    existing_cols = {r[1] for r in con.execute("PRAGMA table_info('runs')").fetchall()}
    select_cols = list(col_names)
    if "dem_download_time" in existing_cols:
        select_cols.append("dem_download_time")
    if "extent_size" in existing_cols:
        select_cols.append("extent_size")
    if "workflow" in existing_cols:
        select_cols.append("workflow")
    if "local_resampling" in existing_cols:
        select_cols.append("local_resampling")
    if "target_crs" in existing_cols:
        select_cols.append("target_crs")
    rows = con.execute(
        f"SELECT {','.join(select_cols)} FROM runs WHERE status = 'success'"
    ).fetchall()
    out = [dict(zip(select_cols, r)) for r in rows]
    if "dem_download_time" not in existing_cols:
        for r in out:
            r["dem_download_time"] = None
    # Backward-Compat:
    #   NULL extent_size      -> "medium"   (bisheriger fester Extent)
    #   NULL workflow         -> "merge_add" (bisheriges Standard-Szenario)
    #   NULL local_resampling -> "nearest"   (bisheriges Verhalten in reproject_dem_local)
    for r in out:
        if not r.get("extent_size"):
            r["extent_size"] = "medium"
        if not r.get("workflow"):
            r["workflow"] = "merge_add"
        if not r.get("local_resampling"):
            r["local_resampling"] = "nearest"
    return out


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


def fetch_categorical_accuracy(db_path: str):
    """Kategoriale Accuracy-Zeilen (--dataset landcover) als Liste von Dicts.

    Getrennt von fetch_accuracy: dort werden ausschliesslich rmse/mae
    ausgewertet, und die sind bei kategorialen Laeufen bewusst NULL (der
    Abstand zwischen Klasse 10 und Klasse 50 ist keine 40). Beide Sichten
    duerfen sich nicht vermischen - sonst laufen Uebereinstimmungsquoten in
    Mittelwerte kontinuierlicher Fehler ein.

    Liefert [] wenn die Tabelle oder die Spalten fehlen (alte DBs).
    """
    con = duckdb.connect(db_path, read_only=True)
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    if "accuracy" not in tables:
        return []
    cols = {r[1] for r in con.execute("PRAGMA table_info('accuracy')").fetchall()}
    if not {"agreement_pct", "metric_kind"} <= cols:
        return []
    run_cols = {r[1] for r in con.execute("PRAGMA table_info('runs')").fetchall()}
    ds_expr = "r.dataset" if "dataset" in run_cols else "NULL"
    res_expr = "r.resolution_m" if "resolution_m" in run_cols else "NULL"
    rows = con.execute(
        f"""SELECT a.run_id, a.metric_kind, a.agreement_pct, a.kappa,
                   a.confusion_json, r.crs_strategy, r.workflow, r.extent_size,
                   {ds_expr} AS dataset, {res_expr} AS resolution_m
            FROM accuracy a LEFT JOIN runs r ON r.run_id = a.run_id
            WHERE a.agreement_pct IS NOT NULL
            ORDER BY a.run_id"""
    ).fetchall()
    keys = ("run_id", "metric_kind", "agreement_pct", "kappa",
            "confusion_json", "strategy", "workflow", "extent_size",
            "dataset", "resolution_m")
    return [dict(zip(keys, row)) for row in rows]


def print_categorical_accuracy(db_path: str) -> None:
    """Kategoriale Ergebnisse tabellarisch ausgeben."""
    rows = fetch_categorical_accuracy(db_path)
    print("\n=== Kategoriale Genauigkeit (Uebereinstimmung / Cohen's Kappa) ===")
    if not rows:
        print("  keine kategorialen Accuracy-Eintraege "
              "(--dataset landcover noch nicht gelaufen?)")
        return
    print(f"  {'run':>5}  {'Strategie':<20} {'Workflow':<11} {'Extent':<7} "
          f"{'Aufl.':>6}  {'Uebereinst.':>11}  {'Kappa':>8}  Metrik")
    for r in rows:
        res = f"{r['resolution_m']:g} m" if r["resolution_m"] else "-"
        kappa = f"{r['kappa']:.6f}" if r["kappa"] is not None else "n/a"
        print(f"  {r['run_id']:>5}  {(r['strategy'] or '-'):<20} "
              f"{(r['workflow'] or '-'):<11} {(r['extent_size'] or '-'):<7} "
              f"{res:>6}  {r['agreement_pct']:>10.4f}%  {kappa:>8}  "
              f"{r['metric_kind']}")


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


def summarize(runs, accuracy_map=None, amortize_dem_seconds=None):
    """Return a dict of metrics for a set of runs.

    amortize_dem_seconds: if set (e.g. median dem_download_time of non-cached
        runs in the same region), compute total_amortized_median as the median
        of (total_time + amortize_dem_seconds). Meant for cached-strategy runs.
    """
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

    dem_download_median = med("dem_download_time")

    total_amortized_median = None
    if amortize_dem_seconds is not None:
        amortized = [t + amortize_dem_seconds for t in total_times]
        total_amortized_median = statistics.median(amortized)

    resampling_vals = sorted({r["local_resampling"] for r in clean
                              if r.get("local_resampling")})
    local_resampling_label = ",".join(resampling_vals) if resampling_vals else None

    target_crs_vals = sorted({r["target_crs"] for r in clean
                              if r.get("target_crs")})
    target_crs_label = ",".join(target_crs_vals) if target_crs_vals else None

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
        "dem_download_median": dem_download_median,
        "total_amortized_median": total_amortized_median,
        "amortize_dem_seconds": amortize_dem_seconds,
        "credits_total": credits,
        "rmse_median": rmse_median,
        "mae_median": mae_median,
        "local_resampling": local_resampling_label,
        "target_crs": target_crs_label,
    }


def _try_import_mannwhitneyu():
    """Importiere scipy.stats.mannwhitneyu lazy; gib None zurueck wenn nicht
    installiert. Damit funktioniert analyze.py auch ohne scipy, nur die
    Signifikanz-Tests sind dann deaktiviert."""
    try:
        from scipy.stats import mannwhitneyu  # type: ignore
        return mannwhitneyu
    except ImportError:
        return None


_SCIPY_HINT_PRINTED = False


def print_significance_tests(group_label, runs_subset):
    """Paarweiser Mann-Whitney-U-Test auf total_time fuer alle crs_strategies
    in runs_subset. Gibt eine kompakte Tabelle aus. Bei n<3 pro Gruppe wird
    der Test uebersprungen (zu wenige Datenpunkte fuer U)."""
    global _SCIPY_HINT_PRINTED
    mwu = _try_import_mannwhitneyu()
    if mwu is None:
        if not _SCIPY_HINT_PRINTED:
            print("\n[--significance] scipy nicht installiert. "
                  "Installation: pip install scipy")
            _SCIPY_HINT_PRINTED = True
        return

    by_strategy = defaultdict(list)
    for r in runs_subset:
        v = r.get("total_time")
        if v is None:
            continue
        by_strategy[r.get("crs_strategy") or "unknown"].append(v)
    strategies = sorted(by_strategy.keys())
    if len(strategies) < 2:
        return
    pairs = [(strategies[i], strategies[j])
             for i in range(len(strategies))
             for j in range(i + 1, len(strategies))]
    print(f"\n--- {group_label} | Mann-Whitney-U (paarweise, total_time) ---")
    for a, b in pairs:
        va, vb = by_strategy[a], by_strategy[b]
        if len(va) < 3 or len(vb) < 3:
            print(f"  {a} vs {b}: zu wenige Datenpunkte "
                  f"(n_a={len(va)}, n_b={len(vb)}, brauche >=3 pro Gruppe)")
            continue
        try:
            stat, pval = mwu(va, vb, alternative="two-sided")
        except Exception as exc:
            print(f"  {a} vs {b}: Test fehlgeschlagen ({exc})")
            continue
        med_diff = statistics.median(va) - statistics.median(vb)
        sig = "signifikant" if pval < 0.05 else "nicht signifikant"
        print(f"  {a} vs {b}: Median-Diff = {med_diff:+.2f}s "
              f"(n_a={len(va)}, n_b={len(vb)}), "
              f"U = {stat:.1f}, p = {pval:.4f} ({sig})")


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
        ("dem_download median [s]", lambda m: fmt(m.get("dem_download_median"))),
        ("total_time_amortized median [s]", lambda m: fmt(m.get("total_amortized_median"))),
        ("credits (sum)",         lambda m: fmt(m["credits_total"], 2)),
        ("RMSE median",           lambda m: fmt(m.get("rmse_median"), 4) if m.get("rmse_median") is not None else "-"),
        ("MAE median",            lambda m: fmt(m.get("mae_median"), 4) if m.get("mae_median") is not None else "-"),
        ("local_resampling",      lambda m: m.get("local_resampling") or "-"),
        ("target_crs",            lambda m: m.get("target_crs") or "-"),
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


def print_nginx_stats(db_path: str):
    """Auswertung der nginx_access_log Tabelle pro Strategie/Region/Extent.

    Pro Run werden zunaechst (request_count, bytes_total, n_206, n_200, n_other)
    bestimmt; daraus dann pro Gruppe Median(count), Sum(bytes_total),
    Anteil 206 vs 200. Das macht den Zusammenhang zwischen Extent-Groesse
    und Anzahl Range Requests sichtbar (xlarge-Timeout-Finding).
    """
    con = duckdb.connect(db_path, read_only=True)
    tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
    if "nginx_access_log" not in tables:
        print("\n[--nginx-stats] Tabelle 'nginx_access_log' existiert nicht. "
              "Wurden Runs mit nginx-Logging gefahren (local_pp/full_pp)?")
        return
    n_logs = con.execute("SELECT COUNT(*) FROM nginx_access_log").fetchone()[0]
    if n_logs == 0:
        print("\n[--nginx-stats] nginx_access_log ist leer.")
        return

    # Per-run-Aggregate; dann gegen runs joinen um Strategie/Scenario/Extent
    # mit reinzuziehen. Backward-Compat: extent_size NULL -> 'medium'.
    rows = con.execute(
        """
        WITH per_run AS (
            SELECT
                run_id,
                COUNT(*)                                          AS n_req,
                COALESCE(SUM(bytes_sent), 0)                      AS bytes_total,
                SUM(CASE WHEN http_status = 200 THEN 1 ELSE 0 END) AS n_200,
                SUM(CASE WHEN http_status = 206 THEN 1 ELSE 0 END) AS n_206,
                SUM(CASE WHEN http_status NOT IN (200, 206)
                         THEN 1 ELSE 0 END)                       AS n_other
            FROM nginx_access_log
            GROUP BY run_id
        )
        SELECT
            r.crs_strategy,
            r.scenario,
            COALESCE(r.extent_size, 'medium') AS extent_size,
            pr.run_id,
            pr.n_req, pr.bytes_total, pr.n_200, pr.n_206, pr.n_other
        FROM per_run pr
        LEFT JOIN runs r ON r.run_id = pr.run_id
        """
    ).fetchall()
    con.close()

    if not rows:
        print("\n[--nginx-stats] Keine matchenden runs.")
        return

    grouped = defaultdict(list)
    for crs_strategy, scenario, extent_size, run_id, n_req, btot, n200, n206, nother in rows:
        region = detect_region(scenario)
        key = (crs_strategy or "unknown", region, extent_size or "medium")
        grouped[key].append({
            "run_id": run_id,
            "n_req":  int(n_req or 0),
            "bytes":  int(btot or 0),
            "n_200":  int(n200 or 0),
            "n_206":  int(n206 or 0),
            "n_other": int(nother or 0),
        })

    def _fmt_bytes(n):
        if n < 1024:
            return f"{n} B"
        for unit in ("KB", "MB", "GB", "TB"):
            n /= 1024.0
            if n < 1024:
                return f"{n:.1f} {unit}"
        return f"{n:.1f} PB"

    print(f"\n{'='*92}")
    print(" nginx Access-Log Stats (pro Strategie / Region / Extent)")
    print(f"{'='*92}")
    header = (f"{'Strategy':<22} {'Region':<10} {'Extent':<8} "
              f"{'n':>3} {'req_median':>11} {'bytes_sum':>11} "
              f"{'%206':>6} {'%200':>6} {'%other':>7}")
    print(header)
    print("-" * len(header))

    EXTENT_ORDER = ("small", "medium", "large", "xlarge", "xxlarge")
    for key in sorted(grouped.keys(),
                      key=lambda k: (k[0], k[1],
                                     EXTENT_ORDER.index(k[2])
                                     if k[2] in EXTENT_ORDER else 99)):
        strat, region, extent = key
        runs = grouped[key]
        n = len(runs)
        req_counts = [r["n_req"] for r in runs]
        bytes_sum = sum(r["bytes"] for r in runs)
        total_reqs = sum(r["n_req"] for r in runs)
        total_206 = sum(r["n_206"] for r in runs)
        total_200 = sum(r["n_200"] for r in runs)
        total_other = sum(r["n_other"] for r in runs)
        pct_206 = (100.0 * total_206 / total_reqs) if total_reqs else 0.0
        pct_200 = (100.0 * total_200 / total_reqs) if total_reqs else 0.0
        pct_other = (100.0 * total_other / total_reqs) if total_reqs else 0.0
        print(f"{strat[:22]:<22} {region[:10]:<10} {extent[:8]:<8} "
              f"{n:>3} {statistics.median(req_counts):>11.1f} "
              f"{_fmt_bytes(bytes_sum):>11} "
              f"{pct_206:>5.1f}% {pct_200:>5.1f}% {pct_other:>6.1f}%")
    print("=" * 92)


def write_csv(path, all_results):
    """Write per-group, per-strategy rows to CSV."""
    fields = [
        "group", "strategy", "n", "n_cold", "n_hot",
        "total_median", "total_ci_low", "total_ci_high",
        "total_min", "total_max",
        "queue_median", "processing_median", "preprocessing_median",
        "dem_download_median", "total_amortized_median", "amortize_dem_seconds",
        "credits_total", "rmse_median", "mae_median", "local_resampling",
        "target_crs",
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
    parser.add_argument("--group-by",
                        choices=("none", "region", "extent", "workflow"),
                        default="none",
                        help="Group strategies by region (derived from scenario name), "
                             "by extent_size (small/medium/large/xlarge/xxlarge), or by "
                             "workflow (merge_add/subtract/mask/aggregation/focal/resample/filter_bbox)")
    parser.add_argument("--region", default=None,
                        help="Filter to a single region (e.g. berlin, hamburg)")
    parser.add_argument("--extent-size",
                        choices=("small", "medium", "large", "xlarge", "xxlarge"),
                        default=None,
                        help="Filter to a single extent size. Runs without recorded "
                             "extent_size are treated as 'medium'.")
    parser.add_argument("--split-run-type", action="store_true",
                        help="Zeige zusaetzlich getrennte Tabellen fuer cold "
                             "und hot Runs (statt sie zu mischen). Wichtig "
                             "weil cold-Runs (z.B. erster Run mit DEM-Cache-"
                             "Miss) andere Laufzeiten haben als hot-Runs.")
    parser.add_argument("--significance", action="store_true",
                        help="Pro Gruppe paarweise Mann-Whitney-U-Tests "
                             "(scipy.stats.mannwhitneyu) zwischen den "
                             "crs_strategies auf total_time. Zeigt Median-"
                             "Diff, U-Statistik und p-Wert sowie ob der "
                             "Unterschied signifikant (p<0.05) ist. "
                             "Benoetigt scipy: pip install scipy.")
    parser.add_argument("--nginx-stats", action="store_true",
                        help="Auswertung der nginx_access_log Tabelle (CDSE "
                             "Zugriffe auf Hetzner-Assets). Pro Strategie/"
                             "Region/Extent: Median Anzahl Requests, Summe "
                             "bytes_sent, Anteil HTTP 206 (Range) vs 200. "
                             "Zeigt den Zusammenhang zwischen Extent-Groesse "
                             "und Anzahl Range Requests.")
    parser.add_argument("--categorical", action="store_true",
                        help="Kategoriale Genauigkeit ausgeben "
                             "(--dataset landcover): Uebereinstimmungsquote "
                             "und Cohen's Kappa je Run. Diese Zeilen tragen "
                             "bewusst KEIN rmse/mae und tauchen deshalb in "
                             "den normalen Accuracy-Spalten nicht auf.")
    args = parser.parse_args()

    if args.nginx_stats:
        print_nginx_stats(args.db)

    if args.categorical:
        print_categorical_accuracy(args.db)

    runs = fetch_runs(args.db)
    if not runs:
        print("No successful runs found.", file=sys.stderr)
        return 1

    accuracy_map = fetch_accuracy(args.db)

    for r in runs:
        r["region"] = detect_region(r["scenario"])
        # Backward-Compat: NULL target_crs -> UTM-EPSG der Region
        if not r.get("target_crs"):
            default_epsg = REGION_DEFAULT_EPSG.get(r["region"])
            if default_epsg:
                r["target_crs"] = f"EPSG:{default_epsg}"

    if args.region:
        runs = [r for r in runs if r["region"] == args.region.lower()]
        if not runs:
            print(f"No runs for region '{args.region}'.", file=sys.stderr)
            return 1

    if args.extent_size:
        runs = [r for r in runs if r.get("extent_size") == args.extent_size]
        if not runs:
            print(f"No runs for extent-size '{args.extent_size}'.", file=sys.stderr)
            return 1

    def _amortize_base(runs_subset):
        """Median dem_download_time of non-cached local_preprocessing runs."""
        vals = [r["dem_download_time"] for r in runs_subset
                if (r.get("crs_strategy") or "") == "local_preprocessing"
                and r.get("dem_download_time") is not None]
        return statistics.median(vals) if vals else None

    def _summarize_group(runs_subset):
        amort_base = _amortize_base(runs_subset)
        by_strategy = defaultdict(list)
        for r in runs_subset:
            by_strategy[r["crs_strategy"] or "unknown"].append(r)
        metrics = {}
        for strategy, rs in by_strategy.items():
            amort = amort_base if strategy == "local_pp_cached" else None
            m = summarize(rs, accuracy_map, amortize_dem_seconds=amort)
            if m:
                metrics[strategy] = m
        return metrics

    def _append_group(results, label, runs_subset):
        """Append summary for runs_subset; with --split-run-type zusaetzlich
        getrennte cold/hot Sub-Tabellen. Sammelt zugleich die Runs-Listen
        fuer eine spaetere Signifikanz-Auswertung pro Gruppe."""
        metrics = _summarize_group(runs_subset)
        if metrics:
            results.append((label, metrics))
            sig_groups.append((label, list(runs_subset)))
        if args.split_run_type:
            cold_runs = [r for r in runs_subset
                         if (r.get("run_type") or "").lower() == "cold"]
            hot_runs = [r for r in runs_subset
                        if (r.get("run_type") or "").lower() == "hot"]
            if cold_runs:
                m_cold = _summarize_group(cold_runs)
                if m_cold:
                    results.append((f"{label}  [cold]", m_cold))
                    sig_groups.append((f"{label}  [cold]", cold_runs))
            if hot_runs:
                m_hot = _summarize_group(hot_runs)
                if m_hot:
                    results.append((f"{label}  [hot]", m_hot))
                    sig_groups.append((f"{label}  [hot]", hot_runs))

    sig_groups = []

    results = []

    _append_group(results, "Overall", runs)

    if args.group_by == "region":
        by_region = defaultdict(list)
        for r in runs:
            by_region[r["region"]].append(r)
        for region in sorted(by_region.keys()):
            _append_group(results, f"Region: {region}", by_region[region])
    elif args.group_by == "extent":
        EXTENT_ORDER = ("small", "medium", "large", "xlarge", "xxlarge")
        by_extent = defaultdict(list)
        for r in runs:
            by_extent[r.get("extent_size") or "medium"].append(r)
        for ext in sorted(by_extent.keys(),
                          key=lambda e: EXTENT_ORDER.index(e) if e in EXTENT_ORDER else 99):
            _append_group(results, f"Extent: {ext}", by_extent[ext])
    elif args.group_by == "workflow":
        WORKFLOW_ORDER = ("merge_add", "subtract", "mask", "aggregation",
                          "focal", "resample", "filter_bbox")
        by_wf = defaultdict(list)
        for r in runs:
            by_wf[r.get("workflow") or "merge_add"].append(r)
        for wf in sorted(by_wf.keys(),
                         key=lambda w: WORKFLOW_ORDER.index(w) if w in WORKFLOW_ORDER else 99):
            _append_group(results, f"Workflow: {wf}", by_wf[wf])

    for label, metrics in results:
        print_table(label, metrics)

    if args.significance:
        print(f"\n{'='*60}")
        print(" Signifikanz-Tests (Mann-Whitney-U, alpha=0.05)")
        print(f"{'='*60}")
        for label, runs_subset in sig_groups:
            print_significance_tests(label, runs_subset)

    if args.csv:
        write_csv(args.csv, results)

    return 0


if __name__ == "__main__":
    sys.exit(main())

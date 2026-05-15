#!/usr/bin/env python3
"""
run_benchmark.py - Automatisierter Ablauf fuer alle drei CRS-Strategien

Strategien:
  onthefly            - Cross-CRS merge direkt auf CDSE
  backend_preprocessing - resample_spatial vor merge auf CDSE
  local_preprocessing - DEM lokal runterladen + reprojizieren, dann load_stac auf CDSE

Aufruf:
  python run_benchmark.py \\
    --api-url https://openeo.dataspace.copernicus.eu/openeo/1.2 \\
    --strategy all --repeat 3 --run-type auto
"""

import argparse
import glob
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject

from database import import_run

CDSE_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2"

ALL_STRATEGIES = ["onthefly", "backend_preprocessing", "local_preprocessing"]


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _make_outdir(base: str, strategy: str) -> Path:
    label = strategy.replace("_preprocessing", "_pp")
    p = Path(base) / f"run_{_ts()}_{label}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def reproject_dem_local(input_tif: str, output_tif: str,
                        dst_crs: str = "EPSG:32633") -> float:
    """Reprojiziert ein GeoTIFF lokal (bilinear). Gibt Laufzeit in Sekunden zurueck."""
    t0 = time.time()
    with rasterio.open(input_tif) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        meta = src.meta.copy()
        meta.update({"crs": dst_crs, "transform": transform,
                     "width": width, "height": height})
        with rasterio.open(output_tif, "w", **meta) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
    return time.time() - t0


def run_openeo(api_url: str, scenario: str, output_dir: str) -> dict:
    """
    Fuehrt openeotest.py run aus. Gibt den Inhalt von results.json zurueck.
    Wirft RuntimeError wenn results.json nicht geschrieben wurde.
    """
    cmd = [
        sys.executable, "openeotest.py", "run",
        "--api-url", api_url,
        "--scenario", scenario,
        "--output-directory", output_dir,
    ]
    print(f"\n  [openeotest] {' '.join(cmd)}")
    subprocess.run(cmd, check=False)

    results_path = Path(output_dir) / "results.json"
    if not results_path.exists():
        raise RuntimeError(
            f"results.json nicht gefunden in {output_dir} – openeotest.py ist moeglicherweise abgestuerzt."
        )
    with open(results_path) as f:
        return json.load(f)


def _run_type_for(repeat_idx: int, run_type_arg: str) -> str:
    if run_type_arg == "auto":
        return "cold" if repeat_idx == 0 else "hot"
    return run_type_arg


# ---------------------------------------------------------------------------
# Strategie-Runner
# ---------------------------------------------------------------------------

def run_strategy_onthefly(args, repeat_idx: int) -> dict:
    run_type = _run_type_for(repeat_idx, args.run_type)
    outdir = _make_outdir(args.output_dir, "onthefly")

    print(f"\n{'='*60}")
    print(f"  Strategie: onthefly  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {outdir}")

    try:
        results = run_openeo(args.api_url, args.scenario_onthefly, str(outdir))
        total_time = results.get("total_time")
        run_id = import_run(str(outdir), crs_strategy="onthefly", run_type=run_type)
        return {
            "strategy": "onthefly", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": results.get("status", "unknown"),
            "preprocessing_time": None, "total_time": total_time,
            "run_id": run_id, "outdir": str(outdir),
        }
    except Exception as exc:
        print(f"  FEHLER: {exc}")
        return {
            "strategy": "onthefly", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": "error", "preprocessing_time": None, "total_time": None,
            "run_id": None, "outdir": str(outdir),
        }


def run_strategy_backend_pp(args, repeat_idx: int) -> dict:
    run_type = _run_type_for(repeat_idx, args.run_type)
    outdir = _make_outdir(args.output_dir, "backend_preprocessing")

    print(f"\n{'='*60}")
    print(f"  Strategie: backend_preprocessing  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {outdir}")

    try:
        results = run_openeo(args.api_url, args.scenario_backend_pp, str(outdir))
        total_time = results.get("total_time")
        run_id = import_run(str(outdir), crs_strategy="backend_preprocessing", run_type=run_type)
        return {
            "strategy": "backend_preprocessing", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": results.get("status", "unknown"),
            "preprocessing_time": None, "total_time": total_time,
            "run_id": run_id, "outdir": str(outdir),
        }
    except Exception as exc:
        print(f"  FEHLER: {exc}")
        return {
            "strategy": "backend_preprocessing", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": "error", "preprocessing_time": None, "total_time": None,
            "run_id": None, "outdir": str(outdir),
        }


def run_strategy_local_pp(args, repeat_idx: int) -> dict:
    run_type = _run_type_for(repeat_idx, args.run_type)
    base = _make_outdir(args.output_dir, "local_preprocessing")
    step1_dir = base / "step1_dem_download"
    step2_tif = str(base / "step2_reprojected.tif")
    step3_dir = base / "step3_main"
    step1_dir.mkdir()
    step3_dir.mkdir()

    print(f"\n{'='*60}")
    print(f"  Strategie: local_preprocessing  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}")

    try:
        # Schritt 1: DEM herunterladen
        print(f"\n  [Schritt 1/3] DEM herunterladen...")
        results_step1 = run_openeo(args.api_url, args.scenario_dem_download, str(step1_dir))
        t_download = results_step1.get("total_time") or 0.0

        # heruntergeladene TIF-Datei finden
        tif_files = glob.glob(str(step1_dir / "*.tif"))
        if not tif_files:
            raise RuntimeError(f"Kein .tif gefunden in {step1_dir}")
        dem_tif = tif_files[0]
        print(f"  DEM heruntergeladen: {dem_tif}  ({t_download:.1f} s)")

        # Schritt 2: Lokal reprojizieren
        print(f"\n  [Schritt 2/3] Lokal reprojizieren nach EPSG:32633...")
        t_reproject = reproject_dem_local(dem_tif, step2_tif)
        print(f"  Reprojektion abgeschlossen: {step2_tif}  ({t_reproject:.2f} s)")

        preprocessing_time = t_download + t_reproject
        print(f"  Gesamte Pre-Processing-Zeit: {preprocessing_time:.2f} s")

        # Schritt 3: load_stac Szenario ausfuehren
        print(f"\n  [Schritt 3/3] load_stac Szenario auf CDSE ausfuehren...")
        results_step3 = run_openeo(args.api_url, args.scenario_local_pp, str(step3_dir))
        t_main = results_step3.get("total_time") or 0.0
        total_time = preprocessing_time + t_main

        run_id = import_run(
            str(step3_dir),
            crs_strategy="local_preprocessing",
            run_type=run_type,
            preprocessing_time=preprocessing_time,
        )
        return {
            "strategy": "local_preprocessing", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": results_step3.get("status", "unknown"),
            "preprocessing_time": preprocessing_time, "total_time": total_time,
            "run_id": run_id, "outdir": str(base),
        }
    except Exception as exc:
        print(f"  FEHLER: {exc}")
        return {
            "strategy": "local_preprocessing", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": "error", "preprocessing_time": None, "total_time": None,
            "run_id": None, "outdir": str(base),
        }


# ---------------------------------------------------------------------------
# Zusammenfassung
# ---------------------------------------------------------------------------

def print_summary(results: list) -> None:
    print(f"\n{'='*78}")
    print("BENCHMARK ZUSAMMENFASSUNG")
    print(f"{'='*78}")

    col = "{:<25} {:>4} {:>5} {:>8} {:>10} {:>10} {:>6}"
    print(col.format("Strategie", "Run", "Type", "Status", "PP-Zeit", "Total", "run_id"))
    print("-" * 78)

    for r in results:
        pp = f"{r['preprocessing_time']:.1f}s" if r["preprocessing_time"] else "-"
        tt = f"{r['total_time']:.1f}s" if r["total_time"] else "-"
        rid = str(r["run_id"]) if r["run_id"] else "-"
        print(col.format(
            r["strategy"], r["repeat"], r["run_type"],
            r["status"][:8], pp, tt, rid,
        ))

    print(f"{'='*78}")

    # Einfache Statistik pro Strategie
    from collections import defaultdict
    by_strategy = defaultdict(list)
    for r in results:
        if r["total_time"] and r["status"] == "success":
            by_strategy[r["strategy"]].append(r["total_time"])

    if any(by_strategy.values()):
        print("\nMittelwerte (nur erfolgreiche Runs):")
        for strategy, times in sorted(by_strategy.items()):
            mean = sum(times) / len(times)
            print(f"  {strategy:<30}  {mean:.1f} s  (n={len(times)})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automatisierter Benchmark fuer alle drei CRS-Strategien"
    )
    parser.add_argument("--api-url", default=CDSE_URL,
                        help=f"OpenEO Backend URL (Standard: {CDSE_URL})")
    parser.add_argument("--strategy", default="all",
                        choices=ALL_STRATEGIES + ["all"],
                        help="Strategie(n) ausfuehren")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Wie oft jede Strategie wiederholen (Standard: 1)")
    parser.add_argument("--run-type", default="auto",
                        choices=["cold", "hot", "auto"],
                        help="cold/hot/auto (auto: erster Run cold, Rest hot)")
    parser.add_argument("--output-dir", default="outputs",
                        help="Basisverzeichnis fuer Output-Ordner (Standard: outputs/)")

    parser.add_argument("--scenario-onthefly",
                        default="scenarios/07_cross_crs_merge_berlin.json")
    parser.add_argument("--scenario-backend-pp", default=None,
                        help="Szenario fuer backend_preprocessing (erforderlich fuer diese Strategie)")
    parser.add_argument("--scenario-local-pp",
                        default="scenarios/07c_local_preprocessing_merge_berlin.json")
    parser.add_argument("--scenario-dem-download",
                        default="scenarios/08_download_dem.json")

    args = parser.parse_args()

    strategies = ALL_STRATEGIES if args.strategy == "all" else [args.strategy]

    # Fruehzeitige Validierung
    if "backend_preprocessing" in strategies and not args.scenario_backend_pp:
        print("WARNUNG: --scenario-backend-pp nicht angegeben. "
              "Strategie 'backend_preprocessing' wird uebersprungen.")
        strategies = [s for s in strategies if s != "backend_preprocessing"]

    if not strategies:
        print("Fehler: Keine ausfuehrbaren Strategien.")
        sys.exit(1)

    for scenario_arg, label in [
        (args.scenario_onthefly, "--scenario-onthefly"),
        (args.scenario_local_pp, "--scenario-local-pp"),
        (args.scenario_dem_download, "--scenario-dem-download"),
    ]:
        if "onthefly" in strategies and label == "--scenario-onthefly":
            pass
        if not Path(scenario_arg).exists():
            print(f"WARNUNG: Szenario nicht gefunden: {scenario_arg} ({label})")

    print(f"\nBenchmark gestartet: {datetime.now().isoformat()}")
    print(f"API-URL:    {args.api_url}")
    print(f"Strategien: {strategies}")
    print(f"Repeats:    {args.repeat}")
    print(f"Run-Type:   {args.run_type}")

    all_results = []
    runners = {
        "onthefly": run_strategy_onthefly,
        "backend_preprocessing": run_strategy_backend_pp,
        "local_preprocessing": run_strategy_local_pp,
    }

    for strategy in strategies:
        for i in range(args.repeat):
            result = runners[strategy](args, i)
            all_results.append(result)

    print_summary(all_results)


if __name__ == "__main__":
    main()

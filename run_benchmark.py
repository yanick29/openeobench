#!/usr/bin/env python3
"""
run_benchmark.py - Automatisierter Ablauf fuer die beiden CRS-Strategien

Strategien:
  onthefly            - Cross-CRS merge direkt auf CDSE
  local_preprocessing - DEM lokal runterladen + reprojizieren,
                        per scp auf Hetzner hochladen, STAC Item generieren,
                        dann load_stac auf CDSE

Aufruf:
  python run_benchmark.py \\
    --api-url https://openeo.dataspace.copernicus.eu/openeo/1.2 \\
    --strategy all --region berlin --repeat 3 --run-type auto
"""

import argparse
import copy
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

ALL_STRATEGIES = ["onthefly", "local_preprocessing"]

# ---------------------------------------------------------------------------
# Hetzner-Konfiguration
# ---------------------------------------------------------------------------
HETZNER_HOST = "root@46.224.62.97"
HETZNER_WEB_PATH = "/var/www/benchmark-data/"
HETZNER_URL_BASE = "http://46.224.62.97/benchmark-data/"

# ---------------------------------------------------------------------------
# Regionen: extent + Ziel-UTM-CRS
# ---------------------------------------------------------------------------
# Extents stimmen 1:1 mit den scenarios/bench_onthefly_{region}.json ueberein.
REGIONS = {
    "amsterdam": {
        "extent": {"west": 4.8,    "south": 52.33, "east": 4.95,  "north": 52.43},
        "epsg":   32631,
    },
    "berlin": {
        "extent": {"west": 13.3,   "south": 52.45, "east": 13.45, "north": 52.55},
        "epsg":   32633,
    },
    "hamburg": {
        "extent": {"west": 9.85,   "south": 53.5,  "east": 10.0,  "north": 53.6},
        "epsg":   32632,
    },
    "kapstadt": {
        "extent": {"west": 18.35,  "south": -34.0, "east": 18.5,  "north": -33.9},
        "epsg":   32734,
    },
    "newyork": {
        "extent": {"west": -74.05, "south": 40.7,  "east": -73.9, "north": 40.8},
        "epsg":   32618,
    },
    "rom": {
        "extent": {"west": 12.4,   "south": 41.85, "east": 12.55, "north": 41.95},
        "epsg":   32633,
    },
    "tokio": {
        "extent": {"west": 139.65, "south": 35.6,  "east": 139.8, "north": 35.7},
        "epsg":   32654,
    },
    "wien": {
        "extent": {"west": 16.3,   "south": 48.15, "east": 16.45, "north": 48.25},
        "epsg":   32633,
    },
    "zuerich": {
        "extent": {"west": 8.45,   "south": 47.33, "east": 8.6,   "north": 47.43},
        "epsg":   32632,
    },
}


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
# Dynamische Szenario-Erzeugung pro Region
# ---------------------------------------------------------------------------

def _load_bench_template(region: str) -> dict:
    path = Path("scenarios") / f"bench_onthefly_{region}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Szenario-Template fuer Region '{region}' nicht gefunden: {path}"
        )
    with open(path) as f:
        return json.load(f)


def build_onthefly_scenario(region: str, target_path: Path) -> Path:
    """Onthefly = bench_onthefly_{region}.json unveraendert kopieren."""
    template = _load_bench_template(region)
    with open(target_path, "w") as f:
        json.dump(template, f, indent=2)
    return target_path


def build_dem_download_scenario(region: str, target_path: Path) -> Path:
    """Baut ein Szenario das nur COPERNICUS_30 fuer die Region herunterlaedt."""
    template = _load_bench_template(region)
    dem_args = template["process_graph"]["loadcollection2"]["arguments"]
    scenario = {
        "process_graph": {
            "loadcollection1": {
                "arguments": dem_args,
                "process_id": "load_collection",
            },
            "saveresult1": {
                "arguments": {
                    "data": {"from_node": "loadcollection1"},
                    "format": "GTiff",
                    "options": {},
                },
                "process_id": "save_result",
                "result": True,
            },
        }
    }
    with open(target_path, "w") as f:
        json.dump(scenario, f, indent=2)
    return target_path


def build_local_pp_scenario(region: str, stac_item_url: str,
                            target_path: Path) -> Path:
    """
    Erzeugt das load_stac Szenario aus bench_onthefly_{region}.json:
    loadcollection2 (DEM) wird durch loadstac1 ersetzt, das auf die
    Hetzner-STAC-Item-URL zeigt.
    """
    template = _load_bench_template(region)
    pg = copy.deepcopy(template["process_graph"])

    # loadcollection2 entfernen und durch loadstac1 ersetzen
    pg.pop("loadcollection2", None)
    pg["loadstac1"] = {
        "arguments": {"url": stac_item_url},
        "process_id": "load_stac",
    }
    # merge1.cube2 auf loadstac1 umbiegen
    pg["merge1"]["arguments"]["cube2"] = {"from_node": "loadstac1"}

    scenario = {"process_graph": pg}
    with open(target_path, "w") as f:
        json.dump(scenario, f, indent=2)
    return target_path


def build_stac_item(region: str, asset_href: str, epsg: int,
                    item_id: str) -> dict:
    """STAC Item passend zum reprojizierten DEM-Asset."""
    ext = REGIONS[region]["extent"]
    w, s, e, n = ext["west"], ext["south"], ext["east"], ext["north"]
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        },
        "bbox": [w, s, e, n],
        "properties": {"datetime": "2011-01-06T00:00:00Z"},
        "assets": {
            "data": {
                "href": asset_href,
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
                "proj:epsg": epsg,
            }
        },
        "links": [],
    }


def scp_upload(local_path: str, remote_filename: str) -> float:
    """scp eine Datei auf Hetzner. Gibt die Upload-Dauer in Sekunden zurueck."""
    remote = f"{HETZNER_HOST}:{HETZNER_WEB_PATH}{remote_filename}"
    cmd = ["scp", "-o", "StrictHostKeyChecking=no", local_path, remote]
    print(f"  [scp] {' '.join(cmd)}")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(
            f"scp fehlgeschlagen ({result.returncode}): {result.stderr.strip()}"
        )
    return elapsed


# ---------------------------------------------------------------------------
# Strategie-Runner
# ---------------------------------------------------------------------------

def run_strategy_onthefly(args, repeat_idx: int) -> dict:
    run_type = _run_type_for(repeat_idx, args.run_type)
    outdir = _make_outdir(args.output_dir, "onthefly")

    print(f"\n{'='*60}")
    print(f"  Strategie: onthefly  |  Region: {args.region}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {outdir}")

    try:
        scenario_path = build_onthefly_scenario(
            args.region, outdir / "scenario_onthefly.json"
        )
        results = run_openeo(args.api_url, str(scenario_path), str(outdir))
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


def _get_or_download_dem(args, region: str, base: Path, cache_dir: Path,
                         use_cache: bool) -> tuple:
    """
    Liefert (dem_tif_path, t_download). Die Download-Zeit zaehlt bewusst NICHT
    zur preprocessing_time und wird hier nur zu Info-Zwecken zurueckgegeben.

    use_cache=True : DEM einmal pro Region herunterladen + in cache_dir ablegen,
                     bei weiteren Runs wiederverwenden (t_download=0.0 bei Hit).
    use_cache=False: DEM bei jedem Run frisch in den run-spezifischen base/step1_dem_download
                     herunterladen.
    """
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"dem_{region}.tif"
        if cached.exists():
            print(f"  Cache-Hit: {cached}  (Download uebersprungen)")
            return str(cached), 0.0

        dl_dir = cache_dir / f"_dl_{region}_{_ts()}"
        dl_dir.mkdir()
        dem_scenario = build_dem_download_scenario(
            region, dl_dir / "scenario_dem_download.json"
        )
        results = run_openeo(args.api_url, str(dem_scenario), str(dl_dir))
        t_download = results.get("total_time") or 0.0

        tif_files = glob.glob(str(dl_dir / "*.tif"))
        if not tif_files:
            raise RuntimeError(f"Kein .tif gefunden in {dl_dir}")
        Path(tif_files[0]).replace(cached)
        print(f"  DEM heruntergeladen + gecached: {cached}  ({t_download:.1f} s)")
        return str(cached), t_download

    # Kein Cache: jedes Mal frisch herunterladen
    step1_dir = base / "step1_dem_download"
    step1_dir.mkdir()
    dem_scenario = build_dem_download_scenario(
        region, base / "scenario_dem_download.json"
    )
    results = run_openeo(args.api_url, str(dem_scenario), str(step1_dir))
    t_download = results.get("total_time") or 0.0

    tif_files = glob.glob(str(step1_dir / "*.tif"))
    if not tif_files:
        raise RuntimeError(f"Kein .tif gefunden in {step1_dir}")
    dem_tif = tif_files[0]
    print(f"  DEM heruntergeladen: {dem_tif}  ({t_download:.1f} s)")
    return dem_tif, t_download


def run_strategy_local_pp(args, repeat_idx: int) -> dict:
    run_type = _run_type_for(repeat_idx, args.run_type)
    base = _make_outdir(args.output_dir, "local_preprocessing")
    step2_tif = str(base / "step2_reprojected.tif")
    step3_dir = base / "step3_main"
    step3_dir.mkdir()

    region = args.region
    epsg = REGIONS[region]["epsg"]
    dst_crs = f"EPSG:{epsg}"
    run_ts = _ts()
    remote_tif_name = f"dem_reprojected_{region}_{run_ts}.tif"
    remote_stac_name = f"stac_item_{region}_{run_ts}.json"
    asset_url = f"{HETZNER_URL_BASE}{remote_tif_name}"
    stac_url = f"{HETZNER_URL_BASE}{remote_stac_name}"
    cache_dir = Path(args.output_dir) / "dem_cache"

    print(f"\n{'='*60}")
    print(f"  Strategie: local_preprocessing  |  Region: {region}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  Ziel-CRS: {dst_crs}")

    try:
        # Schritt 1: DEM aus Cache laden oder herunterladen (Download NICHT in preprocessing_time)
        cache_mode = "Cache aktiv" if args.dem_cache else "Cache deaktiviert (frischer Download)"
        print(f"\n  [Schritt 1/5] DEM bereitstellen ({region}, {cache_mode})...")
        dem_tif, t_download = _get_or_download_dem(
            args, region, base, cache_dir, use_cache=args.dem_cache
        )

        # Schritt 2: Lokal reprojizieren
        print(f"\n  [Schritt 2/5] Lokal reprojizieren nach {dst_crs}...")
        t_reproject = reproject_dem_local(dem_tif, step2_tif, dst_crs=dst_crs)
        print(f"  Reprojektion abgeschlossen: {step2_tif}  ({t_reproject:.2f} s)")

        # Schritt 3: TIF nach Hetzner hochladen
        print(f"\n  [Schritt 3/5] TIF auf Hetzner hochladen -> {remote_tif_name}...")
        t_scp_tif = scp_upload(step2_tif, remote_tif_name)
        print(f"  TIF Upload fertig: {asset_url}  ({t_scp_tif:.2f} s)")

        # Schritt 4: STAC Item generieren + hochladen
        print(f"\n  [Schritt 4/5] STAC Item generieren + hochladen...")
        t_stac_start = time.time()
        stac_item = build_stac_item(
            region=region,
            asset_href=asset_url,
            epsg=epsg,
            item_id=f"dem_reprojected_{region}_{run_ts}",
        )
        local_stac_path = str(base / remote_stac_name)
        with open(local_stac_path, "w") as f:
            json.dump(stac_item, f, indent=2)
        t_stac_build = time.time() - t_stac_start
        t_scp_stac = scp_upload(local_stac_path, remote_stac_name)
        t_stac = t_stac_build + t_scp_stac
        print(f"  STAC Item Upload fertig: {stac_url}  ({t_stac:.2f} s)")

        # preprocessing_time = Reprojektion + SCP Upload + STAC (OHNE DEM Download)
        preprocessing_time = t_reproject + t_scp_tif + t_stac
        print(f"  Pre-Processing-Zeit (ohne DEM Download): {preprocessing_time:.2f} s")
        if t_download > 0.0:
            print(f"  (DEM Download {t_download:.1f} s separat, nicht in preprocessing_time)")

        # Schritt 5: load_stac Szenario ausfuehren
        print(f"\n  [Schritt 5/5] load_stac Szenario auf CDSE ausfuehren...")
        local_pp_scenario = build_local_pp_scenario(
            region, stac_url, base / "scenario_local_pp.json"
        )
        results_step5 = run_openeo(args.api_url, str(local_pp_scenario), str(step3_dir))
        t_main = results_step5.get("total_time") or 0.0
        total_time = preprocessing_time + t_main

        run_id = import_run(
            str(step3_dir),
            crs_strategy="local_preprocessing",
            run_type=run_type,
            preprocessing_time=preprocessing_time,
        )
        return {
            "strategy": "local_preprocessing", "repeat": repeat_idx + 1, "run_type": run_type,
            "status": results_step5.get("status", "unknown"),
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
        description="Automatisierter Benchmark fuer CRS-Strategien"
    )
    parser.add_argument("--api-url", default=CDSE_URL,
                        help=f"OpenEO Backend URL (Standard: {CDSE_URL})")
    parser.add_argument("--strategy", default="all",
                        choices=ALL_STRATEGIES + ["all"],
                        help="Strategie(n) ausfuehren")
    parser.add_argument("--region", default="berlin",
                        choices=sorted(REGIONS.keys()),
                        help="Region (waehlt extent + Ziel-UTM-CRS)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Wie oft jede Strategie wiederholen (Standard: 1)")
    parser.add_argument("--run-type", default="auto",
                        choices=["cold", "hot", "auto"],
                        help="cold/hot/auto (auto: erster Run cold, Rest hot)")
    parser.add_argument("--output-dir", default="outputs",
                        help="Basisverzeichnis fuer Output-Ordner (Standard: outputs/)")
    parser.add_argument("--dem-cache", action="store_true",
                        help="DEM nur einmal pro Region herunterladen + cachen "
                             "(outputs/dem_cache/dem_{region}.tif). "
                             "Ohne Flag wird das DEM bei jedem Run neu geladen. "
                             "Download zaehlt in keinem Fall zur preprocessing_time.")

    args = parser.parse_args()

    strategies = ALL_STRATEGIES if args.strategy == "all" else [args.strategy]

    print(f"\nBenchmark gestartet: {datetime.now().isoformat()}")
    print(f"API-URL:    {args.api_url}")
    print(f"Region:     {args.region}  (EPSG:{REGIONS[args.region]['epsg']})")
    print(f"Strategien: {strategies}")
    print(f"Repeats:    {args.repeat}")
    print(f"Run-Type:   {args.run_type}")

    all_results = []
    runners = {
        "onthefly": run_strategy_onthefly,
        "local_preprocessing": run_strategy_local_pp,
    }

    for strategy in strategies:
        for i in range(args.repeat):
            result = runners[strategy](args, i)
            all_results.append(result)

    print_summary(all_results)


if __name__ == "__main__":
    main()

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
import math
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject

from database import import_nginx_access_log, import_run

CDSE_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2"

ALL_STRATEGIES = ["onthefly", "local_preprocessing"]

# AOI-Groessen (Kantenlaenge in km) um den Region-Mittelpunkt.
# 'medium' bleibt Backward-Compat = unveraenderter REGIONS-Extent.
SIZE_KM = {"small": 5.0, "medium": 10.0, "large": 50.0, "xlarge": 100.0}

# Verfuegbare openEO-Workflows. 'merge_add' = bisheriges Verhalten.
WORKFLOWS = ("merge_add", "subtract", "mask", "aggregation")

# Lokale DEM-Resampling-Methoden. CDSE intern nutzt immer NearestNeighbor;
# bilinear/cubic lokal erzeugen messbare Abweichungen zum onthefly-Output.
LOCAL_RESAMPLING = {
    "nearest":  Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic":    Resampling.cubic,
}

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
                        dst_crs: str = "EPSG:32633",
                        resampling: str = "nearest") -> float:
    """Reprojiziert ein GeoTIFF lokal.

    resampling: 'nearest' (Default, pixelidentisch zu CDSE), 'bilinear' oder
    'cubic'. Letztere weichen vom CDSE-Output ab und machen den
    Accuracy-Check aussagekraeftig.

    Gibt Laufzeit in Sekunden zurueck.
    """
    if resampling not in LOCAL_RESAMPLING:
        raise ValueError(f"Unbekannte Resampling-Methode: {resampling}")
    method = LOCAL_RESAMPLING[resampling]
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
                    resampling=method,
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

def _compute_extent(region: str, extent_size: str) -> dict:
    """AOI um den REGIONS[region]-Mittelpunkt fuer die gewuenschte Groesse.

    'medium' liefert den unveraenderten REGIONS-Extent (Backward-Compat).
    Sonst: Kantenlaenge = SIZE_KM[extent_size]; 1 deg lat ~= 111 km,
    1 deg lon ~= 111 km * cos(lat).
    """
    base = REGIONS[region]["extent"]
    if extent_size == "medium":
        return dict(base)
    if extent_size not in SIZE_KM:
        raise ValueError(f"Unbekannte extent_size: {extent_size}")
    half_km = SIZE_KM[extent_size] / 2.0
    cx = (base["west"] + base["east"]) / 2.0
    cy = (base["south"] + base["north"]) / 2.0
    d_lat = half_km / 111.0
    d_lon = half_km / (111.0 * max(math.cos(math.radians(cy)), 1e-6))
    return {
        "west":  cx - d_lon,
        "south": cy - d_lat,
        "east":  cx + d_lon,
        "north": cy + d_lat,
    }


def _apply_extent_to_template(template: dict, new_extent: dict) -> None:
    """In-place: ersetzt jeden spatial_extent-Dict im Process Graph."""
    pg = template.get("process_graph", {})
    if not isinstance(pg, dict):
        return
    for node in pg.values():
        if not isinstance(node, dict):
            continue
        args = node.get("arguments")
        if isinstance(args, dict) and isinstance(args.get("spatial_extent"), dict):
            args["spatial_extent"] = dict(new_extent)


def _load_bench_template(region: str, extent_size: str = "medium") -> dict:
    path = Path("scenarios") / f"bench_onthefly_{region}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Szenario-Template fuer Region '{region}' nicht gefunden: {path}"
        )
    with open(path) as f:
        template = json.load(f)
    if extent_size != "medium":
        _apply_extent_to_template(template, _compute_extent(region, extent_size))
    return template


def _build_workflow_pg(template: dict, workflow: str) -> dict:
    """Baut den process_graph fuer den gewuenschten Workflow.

    Alle Workflows starten von der merge_add-Baseline (bench_onthefly_{region}.json)
    und mutieren sie:
      merge_add   -> Baseline (unveraendert)
      subtract    -> overlap_resolver wird 'subtract' statt 'add'
      mask        -> SCL Band laden, Cloud-Mask (SCL not in {4,5}) auf B04 anwenden,
                     dann merge_add mit DEM
      aggregation -> merge_add gefolgt von temporalem reduce_dimension(mean)
    """
    pg = copy.deepcopy(template["process_graph"])
    if workflow == "merge_add":
        return pg

    if workflow == "subtract":
        pg["merge1"]["arguments"]["overlap_resolver"] = {
            "process_graph": {
                "subtract1": {
                    "arguments": {"x": {"from_parameter": "x"},
                                  "y": {"from_parameter": "y"}},
                    "process_id": "subtract",
                    "result": True,
                }
            }
        }
        return pg

    if workflow == "mask":
        # S2 zusaetzlich mit SCL laden
        pg["loadcollection1"]["arguments"]["bands"] = ["B04", "SCL"]
        # SCL extrahieren
        pg["filterbands_scl"] = {
            "arguments": {"data": {"from_node": "loadcollection1"},
                          "bands": ["SCL"]},
            "process_id": "filter_bands",
        }
        # Mask-Cube bauen: True wo SCL not in {4, 5}  -> diese Pixel werden maskiert
        pg["apply_mask_build"] = {
            "arguments": {
                "data": {"from_node": "filterbands_scl"},
                "process": {
                    "process_graph": {
                        "eq1": {"arguments": {"x": {"from_parameter": "x"}, "y": 4},
                                "process_id": "eq"},
                        "eq2": {"arguments": {"x": {"from_parameter": "x"}, "y": 5},
                                "process_id": "eq"},
                        "or1": {"arguments": {"x": {"from_node": "eq1"},
                                              "y": {"from_node": "eq2"}},
                                "process_id": "or"},
                        "not1": {"arguments": {"x": {"from_node": "or1"}},
                                 "process_id": "not", "result": True},
                    }
                },
            },
            "process_id": "apply",
        }
        # B04 isoliert + Maske anwenden
        pg["filterbands_b04"] = {
            "arguments": {"data": {"from_node": "loadcollection1"},
                          "bands": ["B04"]},
            "process_id": "filter_bands",
        }
        pg["mask1"] = {
            "arguments": {"data": {"from_node": "filterbands_b04"},
                          "mask": {"from_node": "apply_mask_build"}},
            "process_id": "mask",
        }
        # merge1.cube1 jetzt vom maskierten B04
        pg["merge1"]["arguments"]["cube1"] = {"from_node": "mask1"}
        return pg

    if workflow == "aggregation":
        # Nach merge_cubes ein temporales reduce_dimension(mean) einhaengen
        pg["reducedimension1"] = {
            "arguments": {
                "data": {"from_node": "merge1"},
                "dimension": "t",
                "reducer": {
                    "process_graph": {
                        "mean1": {
                            "arguments": {"data": {"from_parameter": "data"}},
                            "process_id": "mean",
                            "result": True,
                        }
                    }
                },
            },
            "process_id": "reduce_dimension",
        }
        pg["saveresult1"]["arguments"]["data"] = {"from_node": "reducedimension1"}
        return pg

    raise ValueError(f"Unbekannter Workflow: {workflow}")


def build_onthefly_scenario(region: str, target_path: Path,
                            extent_size: str = "medium",
                            workflow: str = "merge_add") -> Path:
    """Onthefly = Workflow-PG aus bench_onthefly_{region}.json gebaut."""
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow)
    with open(target_path, "w") as f:
        json.dump({"process_graph": pg}, f, indent=2)
    return target_path


def build_dem_download_scenario(region: str, target_path: Path,
                                extent_size: str = "medium") -> Path:
    """Baut ein Szenario das nur COPERNICUS_30 fuer die Region herunterlaedt."""
    template = _load_bench_template(region, extent_size)
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
                            target_path: Path,
                            extent_size: str = "medium",
                            workflow: str = "merge_add") -> Path:
    """
    Erzeugt das load_stac Szenario fuer den gewuenschten Workflow:
    Workflow-PG (s. _build_workflow_pg) wird gebaut, dann wird
    loadcollection2 (DEM) durch loadstac1 ersetzt, das auf die
    Hetzner-STAC-Item-URL zeigt.
    """
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow)

    # loadcollection2 entfernen und durch loadstac1 ersetzen
    pg.pop("loadcollection2", None)
    pg["loadstac1"] = {
        "arguments": {"url": stac_item_url},
        "process_id": "load_stac",
    }
    # merge1.cube2 auf loadstac1 umbiegen (cube1 bleibt vom Workflow gesetzt)
    pg["merge1"]["arguments"]["cube2"] = {"from_node": "loadstac1"}

    scenario = {"process_graph": pg}
    with open(target_path, "w") as f:
        json.dump(scenario, f, indent=2)
    return target_path


def build_stac_item(region: str, asset_href: str, epsg: int,
                    item_id: str, extent: dict = None) -> dict:
    """STAC Item passend zum reprojizierten DEM-Asset.

    `extent` ueberschreibt REGIONS[region]['extent'] (z.B. fuer small/large
    Modi). Default = REGIONS-Extent (medium).
    """
    ext = extent if extent is not None else REGIONS[region]["extent"]
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
    print(f"  Strategie: onthefly  |  Region: {args.region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {outdir}")

    try:
        scenario_path = build_onthefly_scenario(
            args.region, outdir / "scenario_onthefly.json",
            extent_size=args.extent_size,
            workflow=args.workflow,
        )
        results = run_openeo(args.api_url, str(scenario_path), str(outdir))
        total_time = results.get("total_time")
        run_id = import_run(str(outdir), crs_strategy="onthefly",
                            run_type=run_type, extent_size=args.extent_size,
                            workflow=args.workflow)
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

    use_cache=True : DEM einmal pro (Region, extent_size) herunterladen +
                     in cache_dir ablegen, bei weiteren Runs wiederverwenden
                     (t_download=None bei Hit).
    use_cache=False: DEM bei jedem Run frisch in den run-spezifischen
                     base/step1_dem_download herunterladen.
    """
    extent_size = getattr(args, "extent_size", "medium")
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"dem_{region}_{extent_size}.tif"
        if cached.exists():
            print(f"  Cache-Hit: {cached}  (Download uebersprungen)")
            return str(cached), None

        dl_dir = cache_dir / f"_dl_{region}_{extent_size}_{_ts()}"
        dl_dir.mkdir()
        dem_scenario = build_dem_download_scenario(
            region, dl_dir / "scenario_dem_download.json",
            extent_size=extent_size,
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
        region, base / "scenario_dem_download.json",
        extent_size=extent_size,
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
    strategy_label = "local_pp_cached" if args.dem_cache else "local_preprocessing"

    print(f"\n{'='*60}")
    print(f"  Strategie: {strategy_label}  |  Region: {region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  Ziel-CRS: {dst_crs}")

    try:
        # Schritt 1: DEM aus Cache laden oder herunterladen (Download NICHT in preprocessing_time)
        cache_mode = "Cache aktiv" if args.dem_cache else "Cache deaktiviert (frischer Download)"
        print(f"\n  [Schritt 1/5] DEM bereitstellen ({region}, {cache_mode})...")
        dem_tif, t_download = _get_or_download_dem(
            args, region, base, cache_dir, use_cache=args.dem_cache
        )

        # Schritt 2: Lokal reprojizieren
        print(f"\n  [Schritt 2/5] Lokal reprojizieren nach {dst_crs} ({args.local_resampling})...")
        t_reproject = reproject_dem_local(dem_tif, step2_tif, dst_crs=dst_crs,
                                          resampling=args.local_resampling)
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
            extent=_compute_extent(region, args.extent_size),
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
        if t_download is not None and t_download > 0.0:
            print(f"  (DEM Download {t_download:.1f} s separat, nicht in preprocessing_time)")

        # Schritt 5: load_stac Szenario ausfuehren
        print(f"\n  [Schritt 5/5] load_stac Szenario auf CDSE ausfuehren...")
        scenario_filename = f"{strategy_label}_{region}.json"
        local_pp_scenario = build_local_pp_scenario(
            region, stac_url, base / scenario_filename,
            extent_size=args.extent_size,
            workflow=args.workflow,
        )
        results_step5 = run_openeo(args.api_url, str(local_pp_scenario), str(step3_dir))
        t_main = results_step5.get("total_time") or 0.0
        total_time = preprocessing_time + t_main

        run_id = import_run(
            str(step3_dir),
            crs_strategy=strategy_label,
            run_type=run_type,
            preprocessing_time=preprocessing_time,
            dem_download_time=t_download,
            extent_size=args.extent_size,
            workflow=args.workflow,
            local_resampling=args.local_resampling,
        )

        # Nginx Access-Logs vom Hetzner-Server holen (CDSE Zugriffe auf TIF + STAC)
        print(f"\n  [Logs] Hole nginx Access-Logs vom Hetzner-Server...")
        try:
            import_nginx_access_log(
                run_id, filenames=[remote_tif_name, remote_stac_name]
            )
        except Exception as exc:
            print(f"  WARNUNG: nginx-Logs konnten nicht geholt werden: {exc}")

        return {
            "strategy": strategy_label, "repeat": repeat_idx + 1, "run_type": run_type,
            "status": results_step5.get("status", "unknown"),
            "preprocessing_time": preprocessing_time, "total_time": total_time,
            "run_id": run_id, "outdir": str(base),
        }
    except Exception as exc:
        print(f"  FEHLER: {exc}")
        return {
            "strategy": strategy_label, "repeat": repeat_idx + 1, "run_type": run_type,
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
# Accuracy-Check (onthefly vs. local_pp)
# ---------------------------------------------------------------------------

def _pg_extent_matches(pg: dict, target: dict, exact: bool = False) -> bool:
    """True wenn ein Knoten ein spatial_extent mit gleichem Mittelpunkt hat.

    exact=False: Center-basiert (Toleranz ~0.01 deg = ~1 km), damit
    verschiedene extent_size-Werte (small/medium/large/xlarge) trotzdem
    zur selben Region matchen.

    exact=True: zusaetzlich muessen alle 4 Bounds uebereinstimmen
    (Toleranz 1e-4 deg ~ 10 m), so dass auch die extent_size passen muss.
    """
    pg_root = pg.get("process_graph", pg)
    if not isinstance(pg_root, dict):
        return False
    try:
        tw, te = float(target["west"]), float(target["east"])
        ts, tn = float(target["south"]), float(target["north"])
    except (KeyError, TypeError, ValueError):
        return False
    tcx, tcy = (tw + te) / 2.0, (ts + tn) / 2.0
    for node in pg_root.values():
        if not isinstance(node, dict):
            continue
        ext = node.get("arguments", {}).get("spatial_extent")
        if not isinstance(ext, dict):
            continue
        try:
            ew, ee = float(ext["west"]), float(ext["east"])
            es, en = float(ext["south"]), float(ext["north"])
        except (KeyError, TypeError, ValueError):
            continue
        cx, cy = (ew + ee) / 2.0, (es + en) / 2.0
        if abs(cx - tcx) >= 0.01 or abs(cy - tcy) >= 0.01:
            continue
        if not exact:
            return True
        if (abs(ew - tw) < 1e-4 and abs(ee - te) < 1e-4
                and abs(es - ts) < 1e-4 and abs(en - tn) < 1e-4):
            return True
    return False


def _folder_matches_extent(folder: Path, target_extent: dict) -> bool:
    """True wenn eine Scenario-JSON im Ordner exakt diesen extent enthaelt."""
    for cand in folder.glob("*.json"):
        try:
            pg = json.loads(cand.read_text())
        except Exception:
            continue
        if _pg_extent_matches(pg, target_extent, exact=True):
            return True
    return False


def _detect_folder_region(folder: Path) -> str:
    """Region eines Run-Ordners bestimmen, oder None."""
    # 1) local_pp: scenario_file heisst {strategy_label}_{region}.json
    for j in folder.glob("*.json"):
        stem = j.stem
        for region in REGIONS:
            if stem.endswith(f"_{region}"):
                return region
    # 2) Fallback: spatial_extent aus dem Process Graph gegen REGIONS matchen
    candidates = (
        folder / "scenario_onthefly.json",
        folder / "processgraph.json",
        folder / "step3_main" / "processgraph.json",
    )
    for cand in candidates:
        if not cand.exists():
            continue
        try:
            pg = json.loads(cand.read_text())
        except Exception:
            continue
        for region, info in REGIONS.items():
            if _pg_extent_matches(pg, info["extent"]):
                return region
    return None


def _find_latest_run_dir(base: str, suffix: str, region: str,
                          extent_size: str = None):
    """Neuesten outputs/run_*_{suffix} fuer Region zurueckgeben, oder None.

    Wenn extent_size gesetzt ist, werden nur Ordner beruecksichtigt, deren
    Scenario-JSON exakt diesen Extent enthaelt (Bounding-Box-Vergleich).
    """
    base_p = Path(base)
    if not base_p.is_dir():
        return None
    target_extent = None
    if extent_size is not None:
        try:
            target_extent = _compute_extent(region, extent_size)
        except (KeyError, ValueError):
            target_extent = None
    candidates = []
    for d in base_p.glob(f"run_*_{suffix}"):
        if not d.is_dir():
            continue
        if _detect_folder_region(d) != region:
            continue
        if target_extent is not None and not _folder_matches_extent(d, target_extent):
            continue
        candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _compare_tif_pair(ref_tif: Path, test_tif: Path):
    """Pro Band MAE/RMSE, dann gemittelt. Returns (mae, rmse, n_bands)."""
    try:
        from accuracy_calculator import align_rasters, calculate_metrics
        import numpy as np
    except Exception as exc:
        print(f"  Import-Fehler fuer accuracy_calculator: {exc}")
        return (None, None, 0)

    try:
        ref_data, test_data, _ = align_rasters(str(ref_tif), str(test_tif))
        results = calculate_metrics(ref_data, test_data)
    except Exception as exc:
        print(f"  Vergleich fehlgeschlagen ({ref_tif.name}): {exc}")
        return (None, None, 0)

    bands = results.get("bands") or []
    if not bands:
        return (None, None, 0)
    mae = float(np.mean([b["MAE"] for b in bands]))
    rmse = float(np.mean([b["RMSE"] for b in bands]))
    return (mae, rmse, len(bands))


def _lookup_run_id_for_dir(step3_dir: Path):
    """run_id ueber timestamp aus results.json in der DB nachschlagen."""
    results_path = step3_dir / "results.json"
    if not results_path.exists():
        return None
    try:
        with open(results_path) as f:
            r = json.load(f)
        ts = r.get("timestamp")
        if not ts:
            return None
        import duckdb
        from database import DB_PATH
        conn = duckdb.connect(DB_PATH, read_only=True)
        row = conn.execute(
            "SELECT run_id FROM runs WHERE timestamp = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (ts,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as exc:
        print(f"  WARNUNG: run_id Lookup fehlgeschlagen: {exc}")
        return None


def _persist_accuracy(run_id: int, mae: float, rmse: float,
                      reference_file: str) -> None:
    """MAE/RMSE in die accuracy-Tabelle schreiben."""
    try:
        import duckdb
        from database import DB_PATH
        conn = duckdb.connect(DB_PATH)
        next_id = conn.execute(
            "SELECT COALESCE(MAX(accuracy_id), 0) + 1 FROM accuracy"
        ).fetchone()[0]
        conn.execute(
            '''INSERT INTO accuracy
               (accuracy_id, run_id, reference_file, rmse, mae)
               VALUES (?, ?, ?, ?, ?)''',
            (next_id, run_id, reference_file, rmse, mae),
        )
        conn.commit()
        conn.close()
        print(f"  Accuracy gespeichert (accuracy_id={next_id}, run_id={run_id})")
    except Exception as exc:
        print(f"  WARNUNG: Accuracy nicht in DB geschrieben: {exc}")


def run_accuracy_check(output_base: str, region: str,
                       local_pp_run_id=None, extent_size: str = None):
    """Neuesten onthefly- und local_pp-Run der Region vergleichen.

    Wenn extent_size gesetzt ist, werden nur Runs mit passendem Extent
    beruecksichtigt (verhindert das Vergleichen unterschiedlich grosser AOIs).

    Speichert den Median(MAE)/Median(RMSE) ueber alle gemeinsamen Date-TIFs in
    die accuracy-Tabelle (run_id des local_pp Runs).
    """
    print(f"\n{'='*60}")
    extent_info = f"  |  Extent: {extent_size}" if extent_size else ""
    print(f"  Accuracy-Check  |  Region: {region}{extent_info}")

    onthefly_dir = _find_latest_run_dir(output_base, "onthefly", region, extent_size)
    local_pp_dir = _find_latest_run_dir(output_base, "local_pp", region, extent_size)

    if not onthefly_dir or not local_pp_dir:
        miss = "onthefly" if not onthefly_dir else "local_pp"
        extent_msg = f" mit extent_size='{extent_size}'" if extent_size else ""
        print(f"  Skip: kein {miss}-Run fuer Region '{region}'{extent_msg} gefunden.")
        return None

    print(f"  Referenz (onthefly): {onthefly_dir.name}")
    print(f"  Test (local_pp):     {local_pp_dir.name}")

    onthefly_tifs = {p.name: p for p in onthefly_dir.glob("*.tif")}
    local_pp_tifs = {p.name: p for p in (local_pp_dir / "step3_main").glob("*.tif")}
    common = sorted(set(onthefly_tifs) & set(local_pp_tifs))
    if not common:
        print(f"  Skip: keine gemeinsamen TIF-Dateien.")
        print(f"    onthefly TIFs: {sorted(onthefly_tifs)}")
        print(f"    local_pp TIFs: {sorted(local_pp_tifs)}")
        return None

    per_mae, per_rmse, n_bands_last = [], [], 0
    for name in common:
        mae, rmse, n_bands = _compare_tif_pair(onthefly_tifs[name],
                                               local_pp_tifs[name])
        if mae is None:
            continue
        per_mae.append(mae)
        per_rmse.append(rmse)
        n_bands_last = n_bands
        print(f"    {name}: MAE={mae:.6f}, RMSE={rmse:.6f} ({n_bands} Bands)")

    if not per_mae:
        print("  Skip: kein valider Pixel-Vergleich moeglich.")
        return None

    median_mae = statistics.median(per_mae)
    median_rmse = statistics.median(per_rmse)

    run_id = local_pp_run_id
    if run_id is None:
        run_id = _lookup_run_id_for_dir(local_pp_dir / "step3_main")
    if run_id is not None:
        _persist_accuracy(run_id, median_mae, median_rmse, str(onthefly_dir))
    else:
        print("  WARNUNG: kein run_id fuer local_pp gefunden, nicht in DB geschrieben.")

    print(f"\n  Accuracy-Check: MAE={median_mae:.6f}, RMSE={median_rmse:.6f} "
          f"({len(per_mae)} Dates, {n_bands_last} Bands verglichen)")

    return {
        "region": region,
        "mae": median_mae,
        "rmse": median_rmse,
        "n_dates": len(per_mae),
        "n_bands": n_bands_last,
        "run_id": run_id,
        "onthefly_dir": str(onthefly_dir),
        "local_pp_dir": str(local_pp_dir),
    }


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
    parser.add_argument("--accuracy-check", action="store_true",
                        help="Nach den Runs Accuracy-Vergleich (MAE/RMSE) zwischen "
                             "dem neuesten onthefly- und local_pp-Output fuer die "
                             "Region ausfuehren. Mit --repeat 0 auch standalone "
                             "auf existierenden Outputs verwendbar.")
    parser.add_argument("--extent-size", default="medium",
                        choices=("small", "medium", "large", "xlarge"),
                        help="AOI-Kantenlaenge um das Region-Zentrum: "
                             "small=5km, medium=10km (Default = bisheriger fester "
                             "REGIONS-Extent, rueckwaertskompatibel), large=50km, "
                             "xlarge=100km. Wirkt auf onthefly, DEM-Download und "
                             "local_pp Szenarien sowie das STAC Item.")
    parser.add_argument("--workflow", default="merge_add",
                        choices=WORKFLOWS,
                        help="openEO Workflow: "
                             "merge_add (Default, B04+DEM via merge_cubes/add), "
                             "subtract (B04-DEM via merge_cubes/subtract), "
                             "mask (B04 mit SCL Cloud-Mask, SCL not in {4,5} "
                             "wird maskiert, dann B04+DEM/add), "
                             "aggregation (B04+DEM/add, dann temporal mean).")
    parser.add_argument("--local-resampling", default="nearest",
                        choices=tuple(LOCAL_RESAMPLING.keys()),
                        help="Resampling-Methode fuer die lokale DEM-Reprojektion "
                             "(nur local_preprocessing). nearest (Default) ist "
                             "pixelidentisch zu CDSE - der Accuracy-Check liefert "
                             "dann MAE=RMSE=0. bilinear/cubic weichen vom "
                             "CDSE-Output ab und machen den Accuracy-Check "
                             "aussagekraeftig.")

    args = parser.parse_args()

    strategies = ALL_STRATEGIES if args.strategy == "all" else [args.strategy]

    print(f"\nBenchmark gestartet: {datetime.now().isoformat()}")
    print(f"API-URL:    {args.api_url}")
    print(f"Region:     {args.region}  (EPSG:{REGIONS[args.region]['epsg']})")
    print(f"Extent:     {args.extent_size}  ({SIZE_KM[args.extent_size]:.0f} km Kantenlaenge)")
    print(f"Workflow:   {args.workflow}")
    print(f"Local-Resampling: {args.local_resampling}")
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

    if args.accuracy_check:
        local_pp_run_id = None
        for r in all_results:
            if (r.get("strategy") in ("local_preprocessing", "local_pp_cached")
                    and r.get("run_id") is not None
                    and r.get("status") == "success"):
                local_pp_run_id = r["run_id"]
        run_accuracy_check(args.output_dir, args.region, local_pp_run_id,
                           extent_size=args.extent_size)


if __name__ == "__main__":
    main()

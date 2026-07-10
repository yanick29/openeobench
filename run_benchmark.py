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
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import re

import rasterio
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject

from database import import_nginx_access_log, import_run

CDSE_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2"

ALL_STRATEGIES = ["onthefly", "local_preprocessing", "full_preprocessing"]
# local_reference ist die unabhaengige lokale Ground-Truth-Pipeline ohne
# CDSE-Workflow-Job - separat opt-in, weil sie nicht direkt mit CDSE-Strategien
# vergleichbar ist (wird per --reference-check als REFERENZ benutzt).
EXTRA_STRATEGIES = ["local_reference"]

# Extents bei denen full_preprocessing per Default ausgesetzt wird, weil
# die hohe Anzahl Range-Requests (siehe nginx_access_log -> xlarge ~1170
# Requests) regelmaessig zu CDSE-Timeouts fuehrt. Mit --include-full-pp=yes
# kann das uebersteuert werden.
LARGE_EXTENTS_FOR_FULL_PP = ("xlarge", "xxlarge")

# AOI-Groessen (Kantenlaenge in km) um den Region-Mittelpunkt.
# 'medium' bleibt Backward-Compat = unveraenderter REGIONS-Extent.
SIZE_KM = {"small": 5.0, "medium": 10.0, "large": 50.0, "xlarge": 100.0, "xxlarge": 200.0}

# Verfuegbare openEO-Workflows. 'merge_add' = bisheriges Verhalten.
WORKFLOWS = ("merge_add", "subtract", "mask", "aggregation", "focal", "resample",
             "filter_bbox")

# Lokale DEM-Resampling-Methoden. CDSE intern nutzt immer NearestNeighbor;
# bilinear/cubic lokal erzeugen messbare Abweichungen zum onthefly-Output.
LOCAL_RESAMPLING = {
    "nearest":  Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic":    Resampling.cubic,
}

# ---------------------------------------------------------------------------
# DEM-Layout Experiment (Nebenkapitel): interne Struktur des extern
# bereitgestellten DEM bei local_preprocessing. Die drei Varianten
# unterscheiden sich AUSSCHLIESSLICH im Schreibprofil des GeoTIFF - die
# reprojizierten Pixelwerte, CRS, Transform und Aufloesung sind identisch.
#   striped              - gestreiftes GeoTIFF, keine Kachelung, keine
#                          Kompression, keine Overviews. Aktuelles Verhalten
#                          (Default -> rueckwaertskompatibel).
#   tiled_uncompressed   - gekachelt 128x128, keine Kompression, keine
#                          Overviews. Isoliert den Effekt der Kachelung.
#   cog                  - gekachelt 128x128, deflate, interne Overviews.
#                          Fuegt gegenueber tiled_uncompressed Kompression
#                          und Overviews hinzu.
# 128x128 = CDSE-Output-Blockgroesse -> fairer Vergleich.
DEM_LAYOUTS = ("striped", "tiled_uncompressed", "cog")
_COG_BLOCK_SIZE = 128


def _normalize_crs(crs_str: str) -> str:
    """Normalisiert 'epsg:3035' / '3035' / 'EPSG:3035' -> 'EPSG:3035'."""
    s = crs_str.strip().upper()
    if s.startswith("EPSG:"):
        s = s[5:]
    return f"EPSG:{int(s)}"


def _parse_epsg(crs_str: str) -> int:
    """EPSG-Code als int aus 'EPSG:3035' oder '3035'."""
    return int(_normalize_crs(crs_str).split(":", 1)[1])


def _is_utm_epsg(epsg: int) -> bool:
    """True wenn EPSG ein UTM-CRS ist (32601-32660 N, 32701-32760 S)."""
    return (32601 <= epsg <= 32660) or (32701 <= epsg <= 32760)

# ---------------------------------------------------------------------------
# Hetzner-Konfiguration (per ENV ueberschreibbar; CLI-Flags --host / --web-path
# / --url-base ueberschreiben die ENV). Trailing slash am Pfad ist erwartet
# fuer die String-Konkatenation in scp_upload / asset URLs.
# ---------------------------------------------------------------------------
HETZNER_HOST = os.environ.get("BENCHMARK_HOST", "root@46.224.62.97")
HETZNER_WEB_PATH = os.environ.get("BENCHMARK_WEB_PATH", "/var/www/benchmark-data/")
HETZNER_URL_BASE = os.environ.get("BENCHMARK_URL_BASE", "http://46.224.62.97/benchmark-data/")


def _ensure_trailing_slash(s: str) -> str:
    return s if s.endswith("/") else s + "/"

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


def _compute_overview_factors(width: int, height: int, min_size: int = 256) -> list:
    """Overview-Faktoren [2, 4, 8, ...] bis kleinste Seite < min_size.

    Fuer die COG-Variante: Overviews sollen genug Ebenen haben, damit CDSE
    bei einer typischen Anfrage nicht sofort den Vollpyramidenpixel liest.
    min_size=256 -> letzte Overview >= 128 px pro Seite, keine winzigen
    Ebenen die nur Overhead sind.
    """
    factors = []
    f = 2
    while max(width, height) // f >= min_size:
        factors.append(f)
        f *= 2
    return factors


def _write_dem_with_layout(data, dst_meta: dict, output_tif: str,
                           layout: str = "striped",
                           block: int = _COG_BLOCK_SIZE) -> None:
    """Schreibt ein reprojiziertes DEM-Array mit einem der 3 Layout-Profile.

    data: 3D numpy Array (bands, height, width) - die Pixelwerte sind
    zwischen allen Layouts identisch, nur die on-disk Repraesentation
    unterscheidet sich.

    dst_meta: rasterio meta-Dict mit driver, dtype, crs, transform, width,
    height, count, nodata. Wird pro Layout mit den layout-spezifischen
    Feldern (tiled, blockxsize, blockysize, compress, interleave)
    ueberschrieben, damit z.B. ein aus dem Input geerbtes tiled=True
    fuer die striped-Variante zurueckgesetzt wird.

    layout:
      striped              - tiled=False, keine Kompression, keine Overviews
      tiled_uncompressed   - tiled=True, block x block, keine Kompression,
                             keine Overviews
      cog                  - tiled=True, block x block, deflate, interne
                             Overviews via rasterio.build_overviews
    """
    if layout not in DEM_LAYOUTS:
        raise ValueError(
            f"Unbekanntes dem_layout: {layout!r}. Erlaubt: {DEM_LAYOUTS}"
        )

    profile = dict(dst_meta)
    profile["driver"] = "GTiff"

    if layout == "striped":
        profile.update({
            "tiled": False,
            "compress": None,
            "interleave": "band",
        })
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
    elif layout == "tiled_uncompressed":
        profile.update({
            "tiled": True,
            "blockxsize": block,
            "blockysize": block,
            "compress": None,
            "interleave": "band",
        })
    elif layout == "cog":
        profile.update({
            "tiled": True,
            "blockxsize": block,
            "blockysize": block,
            "compress": "deflate",
            "interleave": "band",
        })

    if profile.get("compress") is None:
        profile.pop("compress", None)

    with rasterio.open(output_tif, "w", **profile) as dst:
        dst.write(data)

    if layout == "cog":
        factors = _compute_overview_factors(profile["width"], profile["height"])
        if factors:
            with rasterio.open(output_tif, "r+") as dst:
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")


def _inspect_tif_layout(path: str) -> dict:
    """Liest die tatsaechliche interne Struktur zurueck.

    Wird nach dem Schreiben aufgerufen, damit belegbar ist, dass die
    gewuenschte Variante wirklich erzeugt wurde (und nicht z.B. tiled=True
    weil rasterio vom Input geerbt hat).
    """
    with rasterio.open(path) as src:
        profile = src.profile
        overviews = list(src.overviews(1)) if src.count >= 1 else []
        return {
            "path": path,
            "size_bytes": Path(path).stat().st_size,
            "width": src.width,
            "height": src.height,
            "dtype": str(src.dtypes[0]),
            "tiled": bool(profile.get("tiled", False)),
            "blockxsize": profile.get("blockxsize"),
            "blockysize": profile.get("blockysize"),
            "compress": profile.get("compress"),
            "interleave": profile.get("interleave"),
            "num_overviews": len(overviews),
            "overview_factors": overviews,
        }


def _log_tif_layout(info: dict, prefix: str = "  ") -> None:
    """Formatierter Log der Layout-Info fuer die Konsole."""
    size_mb = info["size_bytes"] / (1024 * 1024)
    print(f"{prefix}Layout-Verifikation: {Path(info['path']).name}")
    print(f"{prefix}  size            = {size_mb:.2f} MB "
          f"({info['size_bytes']:,} Bytes)")
    print(f"{prefix}  shape           = {info['width']} x {info['height']} "
          f"({info['dtype']})")
    print(f"{prefix}  tiled           = {info['tiled']}")
    if info["tiled"]:
        print(f"{prefix}  blocksize       = {info['blockxsize']} x "
              f"{info['blockysize']}")
    print(f"{prefix}  compress        = {info['compress'] or 'none'}")
    print(f"{prefix}  overviews       = {info['num_overviews']} "
          f"(factors={info['overview_factors']})")


def reproject_dem_local(input_tif: str, output_tif: str,
                        dst_crs: str = "EPSG:32633",
                        resampling: str = "nearest",
                        target_resolution: float = 10.0,
                        layout: str = "striped") -> float:
    """Reprojiziert ein GeoTIFF lokal und resampelt auf target_resolution.

    resampling: 'nearest' (Default, pixelidentisch zu CDSE), 'bilinear' oder
    'cubic'. Letztere weichen vom CDSE-Output ab und machen den
    Accuracy-Check aussagekraeftig.

    target_resolution: Pixelgroesse im Ziel-CRS (Default 10 m, gleich wie
    Sentinel-2 B04). Wird nur bei UTM-Ziel-CRS erzwungen + S2-Grid-Snap.
    Bei Nicht-UTM-Zielen (LAEA, WGS84, ...) wird die native Aufloesung der
    Reprojektion uebernommen, ohne Grid-Snap - dort hat 10 m / S2-Snap
    keine sinnvolle Semantik.

    layout: DEM-Layout Experiment. Steuert NUR das Schreibprofil des
    Ausgabe-GeoTIFF (striped / tiled_uncompressed / cog). Die reprojizierten
    Pixelwerte sind ueber alle Layouts pixelidentisch - garantiert dadurch
    dass die Reprojektion in einen In-Memory-Puffer laeuft und ausschliesslich
    der finale Write vom Layout abhaengt. Default 'striped' = Verhalten vor
    dem Layout-Experiment (rueckwaertskompatibel fuer full_pp).

    Gibt Laufzeit in Sekunden zurueck (inklusive Overview-Berechnung).
    """
    if resampling not in LOCAL_RESAMPLING:
        raise ValueError(f"Unbekannte Resampling-Methode: {resampling}")
    if layout not in DEM_LAYOUTS:
        raise ValueError(
            f"Unbekanntes dem_layout: {layout!r}. Erlaubt: {DEM_LAYOUTS}"
        )
    import numpy as np
    method = LOCAL_RESAMPLING[resampling]

    # UTM-Detection: nur dann 10 m erzwingen + auf S2-Grid snappen.
    is_utm = False
    try:
        is_utm = _is_utm_epsg(_parse_epsg(dst_crs))
    except (ValueError, AttributeError):
        pass

    t0 = time.time()
    with rasterio.open(input_tif) as src:
        if is_utm:
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds,
                resolution=target_resolution,
            )
            # Origin auf target_resolution-Grid snappen (S2-aligned).
            # Outward auf allen Seiten -> Original-Extent bleibt vollstaendig abgedeckt.
            res = target_resolution
            left, top = transform.c, transform.f
            right = left + width * res
            bottom = top - height * res
            snapped_left   = math.floor(left   / res) * res
            snapped_top    = math.ceil(top     / res) * res
            snapped_right  = math.ceil(right   / res) * res
            snapped_bottom = math.floor(bottom / res) * res
            width  = int(round((snapped_right - snapped_left) / res))
            height = int(round((snapped_top - snapped_bottom) / res))
            transform = Affine(res, 0, snapped_left, 0, -res, snapped_top)
        else:
            # Nicht-UTM (LAEA, WGS84, Web Mercator, ...): native Reprojektions-
            # Aufloesung, kein S2-Snap. CDSE bekommt damit ein "echtes"
            # cross-CRS Resampling-Problem zu loesen.
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds,
            )

        dst_meta = src.meta.copy()
        dst_meta.update({"crs": dst_crs, "transform": transform,
                         "width": width, "height": height})

        # Reprojektion in In-Memory-Array. Wichtig: der Puffer ist die
        # gemeinsame Quelle fuer ALLE Layout-Varianten - so ist garantiert,
        # dass sich striped/tiled/cog nur im Schreibprofil unterscheiden.
        dtype = np.dtype(dst_meta["dtype"])
        data = np.empty((src.count, height, width), dtype=dtype)
        for i in range(1, src.count + 1):
            reproject(
                source=rasterio.band(src, i),
                destination=data[i - 1],
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=method,
            )

    _write_dem_with_layout(data, dst_meta, output_tif, layout=layout)
    return time.time() - t0


def run_openeo(api_url: str, scenario: str, output_dir: str,
               job_timeout: int = 3600) -> dict:
    """
    Fuehrt openeotest.py run aus. Gibt den Inhalt von results.json zurueck.
    Wirft RuntimeError wenn results.json nicht geschrieben wurde oder der
    Subprozess mit Returncode != 0 endet ohne erkennbares Result.

    stdout flowt live (Progress des Backends), stderr wird gecaptured und
    bei non-zero Returncode komplett ausgegeben.
    """
    cmd = [
        sys.executable, "openeotest.py", "run",
        "--api-url", api_url,
        "--scenario", scenario,
        "--output-directory", output_dir,
        "--job-timeout", str(job_timeout),
    ]
    print(f"\n  [openeotest] {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False, stderr=subprocess.PIPE, text=True)

    if proc.returncode != 0:
        stderr_text = (proc.stderr or "").strip()
        print(f"\n  [openeotest] Returncode {proc.returncode} (nicht 0).")
        if stderr_text:
            print("  ---- openeotest.py stderr ----")
            for line in stderr_text.splitlines():
                print(f"  {line}")
            print("  ------------------------------")
        else:
            print("  (kein stderr-Output)")

    results_path = Path(output_dir) / "results.json"
    if not results_path.exists():
        msg = (f"results.json nicht gefunden in {output_dir} - "
               f"openeotest.py mit Returncode {proc.returncode} beendet.")
        if proc.stderr:
            msg += f" stderr: {proc.stderr.strip()[:500]}"
        raise RuntimeError(msg)

    with open(results_path) as f:
        results = json.load(f)

    if proc.returncode != 0 and not results.get("error"):
        results["error"] = (
            f"openeotest.py exit {proc.returncode}; "
            f"stderr: {(proc.stderr or '').strip()[:500]}"
        )
        if results.get("status") not in ("error", "failed"):
            results["status"] = "error"
        try:
            with open(results_path, "w") as f:
                json.dump(results, f, indent=2)
        except OSError as exc:
            print(f"  WARNUNG: results.json konnte nicht zurueckgeschrieben werden: {exc}")

    # environment-Block (git_commit, openeo/rasterio/numpy/proj Versionen)
    # idempotent ergaenzen - egal ob Erfolg oder Fehler.
    _augment_results_json(results_path)
    if "environment" not in results:
        results["environment"] = _collect_environment()

    return results


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


def _build_workflow_pg(template: dict, workflow: str, region: str = None) -> dict:
    """Baut den process_graph fuer den gewuenschten Workflow.

    Alle Workflows starten von der merge_add-Baseline (bench_onthefly_{region}.json)
    und mutieren sie:
      merge_add   -> Baseline (unveraendert)
      subtract    -> overlap_resolver wird 'subtract' statt 'add'
      mask        -> SCL Band laden, Cloud-Mask (SCL not in {4,5}) auf B04 anwenden,
                     dann merge_add mit DEM
      aggregation -> merge_add gefolgt von temporalem reduce_dimension(mean)
      focal       -> nach merge_add ein 3x3 Mittelwert-apply_kernel
      resample    -> DEM wird CDSE-seitig nach EPSG:3035@30m und zurueck nach
                     Region-UTM@10m resamplet, bevor es in merge1.cube2 geht.
                     Testet CDSEs eigene Resampling-Genauigkeit.
      filter_bbox -> nach merge_add ein filter_bbox auf die mittleren 50%
                     des Original-Extents (raeumliche Filteroperation aus
                     dem Proposal).

    BANDNAMEN-FIX: COPERNICUS_30 liefert ein Band "DEM", S2 ein Band "B04".
    Bei verschiedenen Namen konkateniert merge_cubes die Cubes statt sie
    pixelweise zu addieren. Daher rename_labels DEM->B04 nach loadcollection2.

    TEMPORAL-FIX: COPERNICUS_30 hat einen Zeitstempel in 2010-2015, S2 in
    2024. merge_cubes addiert nur wo sich BEIDE Cubes in ALLEN Dimensionen
    (Bands + t + spatial) ueberlappen. Wenn die Zeitdimensionen disjunkt sind,
    konkateniert merge_cubes entlang t und das DEM verschwindet beim
    Speichern der S2-Dates (verifiziert: Output median=2742 = reines S2,
    erwartet 2788 = S2+DEM). Loesung: reduce_dimension(t, first) entfernt die
    Zeitdimension komplett. Ein 2D-DEM-Cube wird beim merge_cubes per
    openEO-Spec auf jeden S2-Zeitschritt gebroadcastet -> der overlap_resolver
    (add/subtract) greift fuer jeden S2-Zeitschritt einzeln.

    Reihenfolge: loadcollection2 -> renamelabels1 (DEM->B04)
                 -> reducedimension_dem (t entfernen)
                 -> [optional resample-Kette]
                 -> merge1.cube2
    """
    pg = copy.deepcopy(template["process_graph"])

    # rename_labels: cube2 Bandname auf "B04" -> ueberlappt mit cube1.
    # source=["DEM"] ist der COPERNICUS_30 Bandname; bei load_stac
    # (local_pp / full_pp) ueberschreiben die Builder source=[], weil
    # der vom Backend vergebene Bandname nicht garantiert "DEM" ist.
    pg["renamelabels1"] = {
        "arguments": {
            "data": {"from_node": "loadcollection2"},
            "dimension": "bands",
            "target": ["B04"],
            "source": ["DEM"],
        },
        "process_id": "rename_labels",
    }
    # reduce_dimension(t, first): DEM ist statisch -> der erste (und einzige
    # in der temporal_extent enthaltene) Zeitwert reicht. Ergebnis ist ein
    # Cube ohne t-Dimension, der per merge_cubes-Broadcasting auf jeden
    # S2-Zeitschritt gebroadcastet wird.
    pg["reducedimension_dem"] = {
        "arguments": {
            "data": {"from_node": "renamelabels1"},
            "dimension": "t",
            "reducer": {
                "process_graph": {
                    "first1": {
                        "arguments": {"data": {"from_parameter": "data"}},
                        "process_id": "first",
                        "result": True,
                    }
                }
            },
        },
        "process_id": "reduce_dimension",
    }
    # merge1.cube2 zeigt jetzt auf das zeitlose DEM.
    pg["merge1"]["arguments"]["cube2"] = {"from_node": "reducedimension_dem"}

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

    if workflow == "focal":
        # 3x3 Mittelwert-Kernel auf den merge1-Output anwenden.
        # Nachbarschaftsoperation -> reagiert auf jede Pixel-Aenderung.
        kernel = [[1.0 / 9.0] * 3 for _ in range(3)]
        pg["applykernel1"] = {
            "arguments": {
                "data": {"from_node": "merge1"},
                "kernel": kernel,
            },
            "process_id": "apply_kernel",
        }
        pg["saveresult1"]["arguments"]["data"] = {"from_node": "applykernel1"}
        return pg

    if workflow == "resample":
        if region is None:
            raise ValueError("workflow=resample benoetigt 'region' fuer das Ziel-UTM.")
        target_epsg = REGIONS[region]["epsg"]
        # DEM (bereits umbenannt auf B04 + t-Dimension entfernt) nach EPSG:3035
        # @ 30m und zurueck nach UTM @ 10m resamplen. Reine CDSE-Operation -
        # testet das interne Resampling.
        pg["resamplespatial1"] = {
            "arguments": {
                "data": {"from_node": "reducedimension_dem"},
                "projection": 3035,
                "resolution": 30,
                "method": "near",
            },
            "process_id": "resample_spatial",
        }
        pg["resamplespatial2"] = {
            "arguments": {
                "data": {"from_node": "resamplespatial1"},
                "projection": target_epsg,
                "resolution": 10,
                "method": "near",
            },
            "process_id": "resample_spatial",
        }
        pg["merge1"]["arguments"]["cube2"] = {"from_node": "resamplespatial2"}
        return pg

    if workflow == "filter_bbox":
        # raeumliche Filteroperation: nach dem merge_add die mittleren 50%
        # der spatial_extent ausschneiden. Damit testen wir CDSEs filter_bbox
        # / filter_spatial-Operation als eigenstaendigen Workflow.
        src_extent = template["process_graph"]["loadcollection1"]["arguments"].get(
            "spatial_extent"
        )
        if not isinstance(src_extent, dict):
            raise ValueError(
                "workflow=filter_bbox: kein spatial_extent in loadcollection1."
            )
        w = float(src_extent["west"])
        s = float(src_extent["south"])
        e = float(src_extent["east"])
        n = float(src_extent["north"])
        cx, cy = (w + e) / 2.0, (s + n) / 2.0
        half_w = (e - w) / 4.0
        half_h = (n - s) / 4.0
        inner = {
            "west":  cx - half_w,
            "south": cy - half_h,
            "east":  cx + half_w,
            "north": cy + half_h,
        }
        crs_val = src_extent.get("crs")
        if crs_val is not None:
            inner["crs"] = crs_val
        pg["filterbbox1"] = {
            "arguments": {
                "data":   {"from_node": "merge1"},
                "extent": inner,
            },
            "process_id": "filter_bbox",
        }
        pg["saveresult1"]["arguments"]["data"] = {"from_node": "filterbbox1"}
        return pg

    raise ValueError(f"Unbekannter Workflow: {workflow}")


def build_onthefly_scenario(region: str, target_path: Path,
                            extent_size: str = "medium",
                            workflow: str = "merge_add") -> Path:
    """Onthefly = Workflow-PG aus bench_onthefly_{region}.json gebaut."""
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow, region=region)
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
    pg = _build_workflow_pg(template, workflow, region=region)

    # loadcollection2 entfernen und durch loadstac1 ersetzen
    pg.pop("loadcollection2", None)
    pg["loadstac1"] = {
        "arguments": {"url": stac_item_url},
        "process_id": "load_stac",
    }
    # Alle Knoten umbiegen die noch auf loadcollection2 zeigen
    # (renamelabels1.data, oder bei workflow=resample auch resamplespatial1.data).
    def _retarget_dem(node_args):
        for k, v in list(node_args.items()):
            if isinstance(v, dict):
                if v.get("from_node") == "loadcollection2":
                    node_args[k] = {"from_node": "loadstac1"}
                else:
                    _retarget_dem(v)
    for node in pg.values():
        if isinstance(node, dict) and isinstance(node.get("arguments"), dict):
            _retarget_dem(node["arguments"])

    # Beim Wechsel von load_collection(COPERNICUS_30) auf load_stac ist der
    # Quellen-Bandname nicht garantiert "DEM" (haengt von der STAC-Item-
    # Metadata und vom Backend ab). source=[] (Default) heisst "rename alle
    # vorhandenen Labels in Reihenfolge" - bei einem Single-Band-DEM also
    # genau das was wir wollen: das eine Band heisst danach "B04".
    if "renamelabels1" in pg:
        pg["renamelabels1"]["arguments"]["source"] = []

    scenario = {"process_graph": pg}
    with open(target_path, "w") as f:
        json.dump(scenario, f, indent=2)
    return target_path


# ---------------------------------------------------------------------------
# full_preprocessing: S2 + DEM komplett von Hetzner laden
# ---------------------------------------------------------------------------

# Beispiel-Dateiname: "openEO_2024-07-14Z.tif" -> date "2024-07-14"
_S2_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _extract_date_from_filename(filename: str) -> str | None:
    """ISO-Datum aus einem S2-Output-Dateinamen extrahieren oder None."""
    m = _S2_DATE_RE.search(filename)
    return m.group(1) if m else None


def build_s2_download_scenario(region: str, target_path: Path,
                               extent_size: str = "medium",
                               workflow: str = "merge_add") -> Path:
    """
    Baut ein Szenario das NUR die S2-Daten herunterlaedt (kein DEM, kein merge).
    Verwendet die loadcollection1-Args (inkl. Cloud-Cover-Filter, bands,
    spatial_extent, temporal_extent) aus dem Region-Template; bei workflow=mask
    werden zusaetzlich SCL-Bands geladen.
    """
    template = _load_bench_template(region, extent_size)
    s2_args = copy.deepcopy(template["process_graph"]["loadcollection1"]["arguments"])
    if workflow == "mask":
        s2_args["bands"] = ["B04", "SCL"]
    scenario = {
        "process_graph": {
            "loadcollection1": {
                "arguments": s2_args,
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


def read_s2_grid(s2_tif: str) -> dict:
    """
    Liest Transform, CRS, Shape und Bounds eines S2-TIFs aus.
    Wird genutzt um das DEM EXAKT auf das gleiche Grid zu reprojizieren.
    """
    with rasterio.open(s2_tif) as src:
        return {
            "transform": src.transform,
            "crs": src.crs.to_string() if src.crs else None,
            "epsg": src.crs.to_epsg() if src.crs else None,
            "width": src.width,
            "height": src.height,
            "bounds": src.bounds,
            "shape": (src.height, src.width),
        }


def reproject_dem_to_grid(input_tif: str, output_tif: str, grid: dict,
                          resampling: str = "nearest") -> float:
    """
    Reprojiziert ein DEM-GeoTIFF auf EXAKT das gegebene Grid (Transform, CRS,
    Width, Height). Gibt Laufzeit in Sekunden zurueck.
    """
    if resampling not in LOCAL_RESAMPLING:
        raise ValueError(f"Unbekannte Resampling-Methode: {resampling}")
    method = LOCAL_RESAMPLING[resampling]
    dst_crs = grid["crs"]
    dst_transform = grid["transform"]
    dst_width = grid["width"]
    dst_height = grid["height"]

    t0 = time.time()
    with rasterio.open(input_tif) as src:
        meta = src.meta.copy()
        meta.update({"crs": dst_crs, "transform": dst_transform,
                     "width": dst_width, "height": dst_height})
        with rasterio.open(output_tif, "w", **meta) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    resampling=method,
                )
    return time.time() - t0


def reproject_s2_local(input_tif: str, output_tif: str,
                       dst_crs: str, resampling: str = "nearest") -> float:
    """Reprojiziert ein S2-TIF lokal nach dst_crs (Szenario 3: BEIDE Raster
    in Nicht-UTM-CRS). Default-Aufloesung aus calculate_default_transform.
    """
    method = LOCAL_RESAMPLING.get(resampling, Resampling.nearest)
    t0 = time.time()
    with rasterio.open(input_tif) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds,
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


def build_s2_stac_item(item_id: str, asset_href: str, datetime_iso: str,
                       grid: dict, bbox_geo: list) -> dict:
    """Ein STAC Item pro S2-Date. bbox_geo = [w, s, e, n] in WGS84 (aus dem
    Region-Extent), grid liefert proj:epsg / proj:shape / proj:bbox.
    """
    w, s, e, n = bbox_geo
    left, bottom, right, top = grid["bounds"]
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v1.1.0/schema.json"
        ],
        "id": item_id,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        },
        "bbox": [w, s, e, n],
        "properties": {
            "datetime": datetime_iso,
            "proj:epsg": grid["epsg"],
            "proj:shape": [grid["height"], grid["width"]],
            "proj:bbox": [left, bottom, right, top],
        },
        "assets": {
            "data": {
                "href": asset_href,
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
                "proj:epsg": grid["epsg"],
                "proj:shape": [grid["height"], grid["width"]],
                "proj:bbox": [left, bottom, right, top],
            }
        },
        "links": [],
    }


def build_s2_stac_collection(collection_id: str,
                             collection_self_url: str,
                             item_links: list,
                             item_dates: list,
                             bbox_geo: list) -> dict:
    """STAC Collection fuer N S2-Items.

    item_links: Liste von (item_id, item_url, item_path_on_remote).
    item_dates: Liste der ISO-Datetime-Strings (zur Berechnung des temporal
    extent).
    """
    if item_dates:
        sorted_dates = sorted(item_dates)
        temporal_interval = [[sorted_dates[0], sorted_dates[-1]]]
    else:
        temporal_interval = [[None, None]]
    links = [{"rel": "self", "href": collection_self_url, "type": "application/json"},
             {"rel": "root", "href": collection_self_url, "type": "application/json"}]
    for _item_id, item_url, _ in item_links:
        links.append({"rel": "item", "href": item_url,
                      "type": "application/json"})
    return {
        "type": "Collection",
        "stac_version": "1.0.0",
        "id": collection_id,
        "description": "S2 L2A subset, locally preprocessed and hosted on Hetzner for openEO load_stac.",
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [bbox_geo]},
            "temporal": {"interval": temporal_interval},
        },
        "links": links,
    }


def build_full_pp_scenario(region: str, s2_stac_url: str, dem_stac_url: str,
                           target_path: Path,
                           extent_size: str = "medium",
                           workflow: str = "merge_add") -> Path:
    """
    Process Graph fuer full_preprocessing: ZWEI load_stac Aufrufe
    (loadstac1=S2, loadstac2=DEM) + Workflow-Verknuepfung.

    Wir starten von der Workflow-PG und ersetzen
    - loadcollection1 (S2)  -> loadstac1
    - loadcollection2 (DEM) -> loadstac2
    sowie biegen merge1.cube1/cube2 und (workflow=mask) filterbands_b04/_scl
    auf den S2 STAC um.
    """
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow, region=region)

    pg.pop("loadcollection1", None)
    pg.pop("loadcollection2", None)
    pg["loadstac1"] = {
        "arguments": {"url": s2_stac_url},
        "process_id": "load_stac",
    }
    pg["loadstac2"] = {
        "arguments": {"url": dem_stac_url},
        "process_id": "load_stac",
    }

    # Alle Knoten umbiegen die noch auf loadcollection1/2 zeigen.
    def _retarget(node_args):
        for k, v in list(node_args.items()):
            if isinstance(v, dict):
                if v.get("from_node") == "loadcollection1":
                    node_args[k] = {"from_node": "loadstac1"}
                elif v.get("from_node") == "loadcollection2":
                    node_args[k] = {"from_node": "loadstac2"}
                else:
                    _retarget(v)
    for node in pg.values():
        if isinstance(node, dict) and isinstance(node.get("arguments"), dict):
            _retarget(node["arguments"])

    # Sicherstellen, dass merge1 die richtigen Cubes bekommt (cube1=S2, cube2=DEM).
    # cube2 muss durch renamelabels1 laufen, damit S2.B04 + DEM.B04 in merge_cubes
    # ueberlappen und der overlap_resolver (add/subtract) greift. _retarget hat
    # renamelabels1.data bereits von loadcollection2 auf loadstac2 umgebogen.
    if "merge1" in pg:
        merge_args = pg["merge1"]["arguments"]
        # cube2 zeigt auf renamelabels1 (oder bei resample auf resamplespatial2,
        # was wiederum auf renamelabels1 zeigt) - in beiden Faellen liefert
        # _build_workflow_pg merge1.cube2 schon korrekt.
        # cube1: bei workflow=mask kommt es aus mask1; sonst direkt loadstac1.
        if workflow != "mask":
            merge_args["cube1"] = {"from_node": "loadstac1"}

    # load_stac auf den Hetzner-DEM-STAC liefert nicht garantiert einen Band
    # mit Name "DEM". source=[] -> rename alle vorhandenen Labels in Reihenfolge
    # (Single-Band-DEM -> wird zu "B04").
    if "renamelabels1" in pg:
        pg["renamelabels1"]["arguments"]["source"] = []

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


SCP_SSH_TIMEOUT = 120  # Sekunden - haengende Uploads/Logs hart abbrechen


_ENVIRONMENT_CACHE = None


def _collect_environment() -> dict:
    """Sammelt Versionen + git-State fuer Reproduzierbarkeit.

    Wird einmal pro Prozess gecacht weil nichts davon zur Laufzeit kippt.
    Felder die nicht ermittelt werden koennen sind None - der Benchmark
    bricht nie wegen Environment-Capture ab.

    Plattform-Hinweis: ueberall subprocess+importlib statt platformspezifischer
    Pfade, damit das auf Linux + Windows + macOS funktioniert.
    """
    global _ENVIRONMENT_CACHE
    if _ENVIRONMENT_CACHE is not None:
        return _ENVIRONMENT_CACHE

    env = {
        "git_commit": None,
        "git_dirty": None,
        "openeo_version": None,
        "rasterio_version": None,
        "numpy_version": None,
        "proj_version": None,
        "gdal_version": None,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
    }

    repo_dir = str(Path(__file__).resolve().parent)
    try:
        commit = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if commit.returncode == 0:
            env["git_commit"] = commit.stdout.strip() or None
        status = subprocess.run(
            ["git", "-C", repo_dir, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if status.returncode == 0:
            env["git_dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        import openeo  # type: ignore
        env["openeo_version"] = getattr(openeo, "__version__", None)
    except Exception:
        pass

    try:
        env["rasterio_version"] = getattr(rasterio, "__version__", None)
        # rasterio __gdal_version__ / __proj_version__ sind die einfachsten
        # Quellen (vermeiden zusaetzliche pyproj-Abhaengigkeit).
        env["gdal_version"] = getattr(rasterio, "__gdal_version__", None)
        env["proj_version"] = getattr(rasterio, "__proj_version__", None)
    except Exception:
        pass

    try:
        import numpy  # type: ignore
        env["numpy_version"] = getattr(numpy, "__version__", None)
    except Exception:
        pass

    if env["proj_version"] is None:
        try:
            import pyproj  # type: ignore
            env["proj_version"] = pyproj.proj_version_str
        except Exception:
            pass

    _ENVIRONMENT_CACHE = env
    return env


def _augment_results_json(results_path: Path) -> None:
    """Schreibt den `environment` Block in eine existierende results.json.
    Idempotent - vorhandene Felder werden nicht ueberschrieben.
    """
    if not results_path.exists():
        return
    try:
        with open(results_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARNUNG: results.json fuer Environment-Augment nicht lesbar: {exc}")
        return
    if "environment" in data and isinstance(data["environment"], dict):
        return
    data["environment"] = _collect_environment()
    try:
        with open(results_path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        print(f"  WARNUNG: results.json fuer Environment-Augment nicht schreibbar: {exc}")


def scp_upload(local_path: str, remote_filename: str) -> float:
    """scp eine Datei auf Hetzner. Gibt die Upload-Dauer in Sekunden zurueck.

    Bricht nach SCP_SSH_TIMEOUT s ab statt unbegrenzt zu haengen (Default 120 s).
    """
    remote = f"{HETZNER_HOST}:{HETZNER_WEB_PATH}{remote_filename}"
    cmd = ["scp", "-o", "StrictHostKeyChecking=no",
           "-o", f"ConnectTimeout={min(SCP_SSH_TIMEOUT, 30)}",
           local_path, remote]
    print(f"  [scp] {' '.join(cmd)}")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=SCP_SSH_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - t0
        raise RuntimeError(
            f"scp Timeout nach {elapsed:.1f}s (Limit {SCP_SSH_TIMEOUT}s) "
            f"fuer {local_path} -> {remote}"
        ) from exc
    elapsed = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(
            f"scp fehlgeschlagen ({result.returncode}): {result.stderr.strip()}"
        )
    return elapsed


def _rewrite_tif_clean(input_tif: str, output_tif: str,
                       blocksize: int = 256,
                       compress: str = "deflate") -> float:
    """Schreibt ein GeoTIFF neu mit einem robusten, einfachen GTiff-Profil.

    Hintergrund: CDSE liefert S2 als COG mit getilten/komprimierten Bloecken.
    Beim Roundtrip (Download -> scp -> nginx -> load_stac) bricht das Tile-
    Profil regelmaessig - rasterio meldet dann "TIFFReadEncodedTile() failed,
    IReadBlock failed" auf der Empfaengerseite. Wir re-encoden hier mit
    einem konservativen Profil: driver=GTiff, tiled, 256x256 Bloecke,
    deflate.

    CRS, Transform, Dtype, Nodata und Band-Beschreibungen werden 1:1
    uebernommen, sodass STAC-Geometrie und Pixel-Werte unveraendert bleiben.
    Idempotent - mehrfaches Rewriting aendert das Profil weiter nicht.
    """
    t0 = time.time()
    with rasterio.open(input_tif) as src:
        profile = src.profile.copy()
        profile.update({
            "driver":     "GTiff",
            "tiled":      True,
            "blockxsize": blocksize,
            "blockysize": blocksize,
            "compress":   compress,
            "interleave": "band",
            "BIGTIFF":    "IF_SAFER",
        })
        with rasterio.open(output_tif, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                dst.write(src.read(i), i)
            if src.descriptions and any(src.descriptions):
                dst.descriptions = src.descriptions
            if src.nodata is not None:
                dst.nodata = src.nodata
    return time.time() - t0


def _remote_file_size(host: str, remote_path: str,
                      timeout: int = SCP_SSH_TIMEOUT) -> int:
    """Liest die Dateigroesse per ssh+stat. Wirft RuntimeError bei Fehler.

    stat -c '%s' ist GNU coreutils (Linux/Hetzner). Auf BSD/macOS waere
    'stat -f %z' noetig; das ssh-Target ist hier aber immer der Linux-
    Webserver, daher reicht die GNU-Variante.
    """
    connect_timeout = min(int(timeout), 30)
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
           "-o", f"ConnectTimeout={connect_timeout}",
           host, f"stat -c %s {remote_path}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ssh stat Timeout nach {timeout}s fuer {remote_path}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"ssh stat fehlgeschlagen ({result.returncode}) fuer "
            f"{remote_path}: {result.stderr.strip()}"
        )
    try:
        return int(result.stdout.strip())
    except ValueError:
        raise RuntimeError(
            f"Unerwartete stat Ausgabe fuer {remote_path}: {result.stdout!r}"
        )


def scp_upload_verified(local_path: str, remote_filename: str) -> float:
    """scp_upload + anschliessende Groessen-Verifikation per ssh stat.

    Faengt stille Abbrueche der scp-Verbindung ab, die sonst zu einem
    truncated TIFF auf dem Server fuehren. Bei Groessen-Mismatch wird
    geworfen statt mit korrupten Daten weiterzulaufen.
    """
    elapsed = scp_upload(local_path, remote_filename)
    local_size = Path(local_path).stat().st_size
    remote_path = f"{HETZNER_WEB_PATH}{remote_filename}"
    remote_size = _remote_file_size(HETZNER_HOST, remote_path)
    if remote_size != local_size:
        raise RuntimeError(
            f"Upload-Integritaet verletzt fuer {remote_filename}: "
            f"lokal={local_size} Bytes, remote={remote_size} Bytes "
            f"(Differenz {local_size - remote_size:+d}). "
            f"Wahrscheinlich Verbindung waehrend scp abgebrochen."
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
        results = run_openeo(args.api_url, str(scenario_path), str(outdir),
                             job_timeout=args.job_timeout)
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
        results = run_openeo(args.api_url, str(dem_scenario), str(dl_dir),
                             job_timeout=getattr(args, "job_timeout", 3600))
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
    results = run_openeo(args.api_url, str(dem_scenario), str(step1_dir),
                         job_timeout=getattr(args, "job_timeout", 3600))
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
    region_epsg = REGIONS[region]["epsg"]
    if getattr(args, "target_crs", None):
        target_crs_str = _normalize_crs(args.target_crs)
        target_epsg = _parse_epsg(target_crs_str)
    else:
        target_epsg = region_epsg
        target_crs_str = f"EPSG:{region_epsg}"
    dst_crs = target_crs_str
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

        # Schritt 2: Lokal reprojizieren (bei UTM auch auf 10 m S2-Grid snappen)
        grid_info = "10 m, S2-snap" if _is_utm_epsg(target_epsg) else "native res"
        dem_layout = getattr(args, "dem_layout", "striped")
        print(f"\n  [Schritt 2/5] Lokal reprojizieren nach {dst_crs} "
              f"({args.local_resampling}, {grid_info}, dem_layout={dem_layout})...")
        t_reproject = reproject_dem_local(dem_tif, step2_tif, dst_crs=dst_crs,
                                          resampling=args.local_resampling,
                                          layout=dem_layout)
        print(f"  Reprojektion abgeschlossen: {step2_tif}  ({t_reproject:.2f} s)")
        _log_tif_layout(_inspect_tif_layout(step2_tif))

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
            epsg=target_epsg,
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
        results_step5 = run_openeo(args.api_url, str(local_pp_scenario), str(step3_dir),
                                   job_timeout=args.job_timeout)
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
            target_crs=target_crs_str,
            dem_layout=dem_layout,
        )

        # Nginx Access-Logs vom Hetzner-Server holen (CDSE Zugriffe auf TIF + STAC)
        print(f"\n  [Logs] Hole nginx Access-Logs vom Hetzner-Server...")
        try:
            import_nginx_access_log(
                run_id, filenames=[remote_tif_name, remote_stac_name],
                ssh_host=HETZNER_HOST,
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


def run_strategy_full_pp(args, repeat_idx: int) -> dict:
    """
    full_preprocessing: BEIDE Raster (S2 + DEM) extern.
      1. S2 von CDSE runterladen (load_collection + save_result).
      2. DEM von CDSE runterladen (wie local_preprocessing).
      3. DEM lokal reprojizieren (Default: exakt auf S2-Grid).
      4. Optional: S2 lokal nach target_crs reprojizieren (--reproject-s2).
      5. Alles per scp auf Hetzner hochladen.
      6. STAC Collection (S2, mehrere Items) + STAC Item (DEM) bauen+hochladen.
      7. Prozessgraph mit zwei load_stac Aufrufen, Job auf CDSE.
    """
    run_type = _run_type_for(repeat_idx, args.run_type)
    base = _make_outdir(args.output_dir, "full_preprocessing")
    s2_dl_dir = base / "step1_s2_download"
    s2_dl_dir.mkdir()
    dem_dl_dir = base / "step2_dem_download"
    dem_dl_dir.mkdir()
    dem_repro_tif = str(base / "step3_dem_reprojected.tif")
    s2_repro_dir = base / "step3b_s2_reprojected"
    main_dir = base / "step5_main"
    main_dir.mkdir()

    region = args.region
    region_epsg = REGIONS[region]["epsg"]
    if getattr(args, "target_crs", None):
        target_crs_str = _normalize_crs(args.target_crs)
        target_epsg = _parse_epsg(target_crs_str)
    else:
        target_crs_str = None
        target_epsg = None
    reproject_s2 = bool(getattr(args, "reproject_s2", False))
    run_ts = _ts()
    geo_extent = _compute_extent(region, args.extent_size)
    bbox_geo = [geo_extent["west"], geo_extent["south"],
                geo_extent["east"], geo_extent["north"]]

    print(f"\n{'='*60}")
    target_info = f"target_crs={target_crs_str or 'EPSG:{}'.format(region_epsg)}"
    if reproject_s2 and target_crs_str:
        target_info += " (DEM+S2 reprojiziert)"
    elif target_crs_str:
        target_info += " (nur DEM reprojiziert)"
    else:
        target_info += " (DEM auf S2-Grid gesnapped)"
    print(f"  Strategie: full_preprocessing  |  Region: {region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  {target_info}")

    try:
        # Schritt 1: S2 von CDSE
        print(f"\n  [Schritt 1/7] S2 von CDSE herunterladen ({region}, {args.extent_size})...")
        s2_scenario = build_s2_download_scenario(
            region, base / "scenario_s2_download.json",
            extent_size=args.extent_size, workflow=args.workflow,
        )
        t_s2_dl_start = time.time()
        s2_results = run_openeo(args.api_url, str(s2_scenario), str(s2_dl_dir),
                                job_timeout=args.job_timeout)
        # CDSE total_time fuer S2 separat festhalten (waere genauer als wall-time,
        # aber wall-time deckt auch Submit/Queue ab. Beides ist 'extern'.)
        t_s2_download = s2_results.get("total_time") or (time.time() - t_s2_dl_start)
        s2_tifs = sorted(Path(p) for p in glob.glob(str(s2_dl_dir / "*.tif")))
        if not s2_tifs:
            raise RuntimeError(f"Keine S2-TIFs heruntergeladen in {s2_dl_dir}")
        print(f"  {len(s2_tifs)} S2-TIFs heruntergeladen  ({t_s2_download:.1f} s CDSE-Zeit)")

        # Schritt 2: DEM von CDSE (separate Messung, NICHT in preprocessing_time)
        print(f"\n  [Schritt 2/7] DEM von CDSE herunterladen ({region}, {args.extent_size})...")
        dem_scenario = build_dem_download_scenario(
            region, base / "scenario_dem_download.json",
            extent_size=args.extent_size,
        )
        dem_results = run_openeo(args.api_url, str(dem_scenario), str(dem_dl_dir),
                                 job_timeout=args.job_timeout)
        t_dem_download = dem_results.get("total_time") or 0.0
        dem_tifs = glob.glob(str(dem_dl_dir / "*.tif"))
        if not dem_tifs:
            raise RuntimeError(f"Kein DEM-TIF heruntergeladen in {dem_dl_dir}")
        dem_tif = dem_tifs[0]
        print(f"  DEM heruntergeladen: {dem_tif}  ({t_dem_download:.1f} s CDSE-Zeit)")

        # Schritt 3: Lokale Reprojektion(en)
        # Default: DEM exakt auf S2-Grid (kein target_crs).
        # target_crs ohne reproject_s2: DEM nach target_crs (S2 unveraendert).
        # target_crs + reproject_s2: BEIDE nach target_crs.
        if target_crs_str is None:
            print(f"\n  [Schritt 3/7] DEM auf S2-Grid reprojizieren (lese Grid aus {s2_tifs[0].name})...")
            s2_grid = read_s2_grid(str(s2_tifs[0]))
            print(f"    S2-Grid: EPSG:{s2_grid['epsg']}, shape={s2_grid['shape']}, transform={s2_grid['transform']}")
            t_dem_reproject = reproject_dem_to_grid(
                dem_tif, dem_repro_tif, s2_grid,
                resampling=args.local_resampling,
            )
            dem_epsg = s2_grid["epsg"]
        else:
            print(f"\n  [Schritt 3/7] DEM nach {target_crs_str} reprojizieren ({args.local_resampling})...")
            t_dem_reproject = reproject_dem_local(
                dem_tif, dem_repro_tif, dst_crs=target_crs_str,
                resampling=args.local_resampling,
            )
            dem_epsg = target_epsg
        print(f"  DEM Reprojektion fertig: {dem_repro_tif}  ({t_dem_reproject:.2f} s)")

        # S2 ggf. reprojizieren (Szenario 3): jedes TIF einzeln nach target_crs.
        t_s2_reproject = 0.0
        s2_for_upload = list(s2_tifs)
        if target_crs_str is not None and reproject_s2:
            s2_repro_dir.mkdir(exist_ok=True)
            print(f"\n  [Schritt 3b/7] S2 nach {target_crs_str} reprojizieren ({len(s2_tifs)} TIFs)...")
            new_tifs = []
            for stif in s2_tifs:
                out = s2_repro_dir / stif.name
                t_s2_reproject += reproject_s2_local(
                    str(stif), str(out), dst_crs=target_crs_str,
                    resampling=args.local_resampling,
                )
                new_tifs.append(out)
            s2_for_upload = new_tifs
            print(f"  S2 Reprojektion fertig  ({t_s2_reproject:.2f} s)")

        # Schritt 3c: ALLE TIFs mit robustem Tile-Profil neu schreiben (S2 + DEM).
        # Verhindert "TIFFReadEncodedTile() failed, IReadBlock failed" beim
        # Roundtrip durch Hetzner+CDSE. Gilt fuer beide S2-Pfade (original vs
        # reprojiziert) und auch fuer das DEM, damit das Upload-Profil
        # konsistent ist.
        clean_dir = base / "step3c_clean"
        clean_dir.mkdir(exist_ok=True)
        print(f"\n  [Schritt 3c/7] S2 + DEM mit robustem GTiff-Profil neu "
              f"schreiben (tiled 256x256, deflate)...")
        t_clean_start = time.time()
        clean_s2 = []
        for stif in s2_for_upload:
            out = clean_dir / stif.name
            _rewrite_tif_clean(str(stif), str(out))
            clean_s2.append(out)
        s2_for_upload = clean_s2
        clean_dem_tif = str(clean_dir / "dem.tif")
        _rewrite_tif_clean(dem_repro_tif, clean_dem_tif)
        dem_for_upload = clean_dem_tif
        t_clean = time.time() - t_clean_start
        print(f"  {len(clean_s2)} S2 + 1 DEM clean rewritten  ({t_clean:.2f} s)")

        # Schritt 4: Alle TIFs auf Hetzner hochladen (mit Groessen-Verifikation).
        print(f"\n  [Schritt 4/7] {len(s2_for_upload)} S2 + 1 DEM TIF auf Hetzner hochladen (verified)...")
        s2_remote_names = []
        t_tif_uploads = 0.0
        for stif in s2_for_upload:
            remote_name = f"s2_{region}_{run_ts}_{stif.name}"
            t_tif_uploads += scp_upload_verified(str(stif), remote_name)
            s2_remote_names.append((stif, remote_name))
        dem_remote_tif_name = f"full_pp_dem_{region}_{run_ts}.tif"
        t_tif_uploads += scp_upload_verified(dem_for_upload, dem_remote_tif_name)
        print(f"  TIF Uploads fertig  ({t_tif_uploads:.2f} s)")

        # Schritt 5: STAC Collection + DEM STAC Item bauen und hochladen
        print(f"\n  [Schritt 5/7] STAC Collection (S2) + STAC Item (DEM) generieren...")
        t_stac_build_start = time.time()
        collection_id = f"s2_{region}_{run_ts}"
        collection_remote_name = f"s2_collection_{region}_{run_ts}.json"
        collection_url = f"{HETZNER_URL_BASE}{collection_remote_name}"

        # Pro S2-TIF ein eigenes STAC Item (mit datetime) erzeugen.
        item_links = []
        item_dates = []
        s2_item_local_paths = []
        s2_item_remote_names = []
        for stif, remote_name in s2_remote_names:
            date_str = _extract_date_from_filename(stif.name)
            if not date_str:
                print(f"  WARNUNG: konnte kein Datum aus {stif.name} extrahieren, ueberspringe")
                continue
            dt_iso = f"{date_str}T00:00:00Z"
            item_dates.append(dt_iso)
            item_id = f"s2_{region}_{run_ts}_{date_str}"
            item_remote_name = f"s2_item_{region}_{run_ts}_{date_str}.json"
            item_url = f"{HETZNER_URL_BASE}{item_remote_name}"
            asset_url = f"{HETZNER_URL_BASE}{remote_name}"
            grid = read_s2_grid(str(stif))
            item = build_s2_stac_item(item_id, asset_url, dt_iso, grid, bbox_geo)
            local_item = str(base / item_remote_name)
            with open(local_item, "w") as f:
                json.dump(item, f, indent=2)
            s2_item_local_paths.append((local_item, item_remote_name))
            s2_item_remote_names.append(item_remote_name)
            item_links.append((item_id, item_url, item_remote_name))

        collection = build_s2_stac_collection(
            collection_id, collection_url, item_links, item_dates, bbox_geo,
        )
        local_collection = str(base / collection_remote_name)
        with open(local_collection, "w") as f:
            json.dump(collection, f, indent=2)

        # DEM STAC Item (single)
        dem_stac_remote_name = f"full_pp_dem_stac_{region}_{run_ts}.json"
        dem_stac_url = f"{HETZNER_URL_BASE}{dem_stac_remote_name}"
        dem_asset_url = f"{HETZNER_URL_BASE}{dem_remote_tif_name}"
        dem_item = build_stac_item(
            region=region, asset_href=dem_asset_url, epsg=dem_epsg,
            item_id=f"full_pp_dem_{region}_{run_ts}", extent=geo_extent,
        )
        local_dem_stac = str(base / dem_stac_remote_name)
        with open(local_dem_stac, "w") as f:
            json.dump(dem_item, f, indent=2)
        t_stac_build = time.time() - t_stac_build_start

        # STAC-Dateien hochladen (separat von TIF-Uploads gemessen)
        t_stac_uploads = 0.0
        for local_item, item_remote_name in s2_item_local_paths:
            t_stac_uploads += scp_upload(local_item, item_remote_name)
        t_stac_uploads += scp_upload(local_collection, collection_remote_name)
        t_stac_uploads += scp_upload(local_dem_stac, dem_stac_remote_name)
        t_stac = t_stac_build + t_stac_uploads
        print(f"  STAC fertig: {collection_url}  +  {dem_stac_url}  ({t_stac:.2f} s)")

        # preprocessing_time = DEM-Reproject + S2-Reproject + Clean-Rewrite +
        # alle Uploads + STAC-Build (S2/DEM Downloads zaehlen separat, wie bei
        # local_preprocessing).
        preprocessing_time = (t_dem_reproject + t_s2_reproject + t_clean
                              + t_tif_uploads + t_stac_build + t_stac_uploads)
        print(f"  Pre-Processing-Zeit (ohne CDSE-Downloads): {preprocessing_time:.2f} s")
        print(f"  (S2 Download {t_s2_download:.1f} s + DEM Download {t_dem_download:.1f} s separat)")

        # Schritt 6: full_pp Szenario bauen + ausfuehren
        print(f"\n  [Schritt 6/7] full_pp Szenario (2x load_stac) auf CDSE ausfuehren...")
        scenario_path = build_full_pp_scenario(
            region, collection_url, dem_stac_url,
            base / f"full_preprocessing_{region}.json",
            extent_size=args.extent_size, workflow=args.workflow,
        )
        results_main = run_openeo(args.api_url, str(scenario_path), str(main_dir),
                                  job_timeout=args.job_timeout)
        t_main = results_main.get("total_time") or 0.0
        total_time = preprocessing_time + t_main

        # Schritt 7: persistieren
        run_id = import_run(
            str(main_dir),
            crs_strategy="full_preprocessing",
            run_type=run_type,
            preprocessing_time=preprocessing_time,
            dem_download_time=t_dem_download,
            s2_download_time=t_s2_download,
            extent_size=args.extent_size,
            workflow=args.workflow,
            local_resampling=args.local_resampling,
            target_crs=target_crs_str,
        )

        # Nginx-Logs fuer ALLE relevanten Dateien (TIFs + STAC Items + Collection + DEM)
        print(f"\n  [Logs] Hole nginx Access-Logs vom Hetzner-Server...")
        try:
            log_filenames = [n for _, n in s2_remote_names] + s2_item_remote_names + [
                collection_remote_name, dem_remote_tif_name, dem_stac_remote_name,
            ]
            import_nginx_access_log(run_id, filenames=log_filenames,
                                     ssh_host=HETZNER_HOST)
        except Exception as exc:
            print(f"  WARNUNG: nginx-Logs konnten nicht geholt werden: {exc}")

        return {
            "strategy": "full_preprocessing", "repeat": repeat_idx + 1,
            "run_type": run_type,
            "status": results_main.get("status", "unknown"),
            "preprocessing_time": preprocessing_time, "total_time": total_time,
            "run_id": run_id, "outdir": str(base),
        }
    except Exception as exc:
        print(f"  FEHLER: {exc}")
        return {
            "strategy": "full_preprocessing", "repeat": repeat_idx + 1,
            "run_type": run_type,
            "status": "error", "preprocessing_time": None, "total_time": None,
            "run_id": None, "outdir": str(base),
        }


# ---------------------------------------------------------------------------
# local_reference: vollstaendig lokale Ground-Truth-Pipeline
# ---------------------------------------------------------------------------

def _box3_mean(arr):
    """3x3 Mittelwert-Filter mit Edge-Padding.

    Aequivalent zu apply_kernel mit kernel=[[1/9]*3]*3 + replicate-padding.
    Reine numpy-Implementierung, keine zusaetzliche Dependency.
    """
    import numpy as np
    a = arr.astype(np.float64, copy=False)
    pad = np.pad(a, 1, mode="edge")
    s = (pad[:-2, :-2] + pad[:-2, 1:-1] + pad[:-2, 2:] +
         pad[1:-1, :-2] + pad[1:-1, 1:-1] + pad[1:-1, 2:] +
         pad[2:, :-2] + pad[2:, 1:-1] + pad[2:, 2:])
    return s / 9.0


def _apply_local_workflow(workflow: str, s2_tifs: list, dem_tif: Path,
                          out_dir: Path) -> list:
    """Wende den Workflow lokal mit rasterio+numpy an. Alle Eingaben muessen
    bereits auf dasselbe Grid (CRS, Aufloesung, Transform, Shape) reprojiziert
    sein.

    workflow:
      merge_add / resample -> S2[B04] + DEM
      subtract             -> S2[B04] - DEM
      mask                 -> S2 mit SCL not in {4,5} maskiert, dann + DEM
      focal                -> (S2[B04] + DEM) -> 3x3 Mittelwert-Kernel
      aggregation          -> mean_t(S2[B04] + DEM) ueber alle Dates
      filter_bbox          -> (S2[B04] + DEM) -> mittlere 50% des Extents

    Schreibt openEO_*.tif unter denselben Dateinamen wie die S2-Eingaben in
    out_dir und gibt deren Pfade zurueck.
    """
    import numpy as np

    with rasterio.open(str(dem_tif)) as dem_src:
        dem_data = dem_src.read(1).astype(np.float64)
        ref_meta = dem_src.meta.copy()
        ref_transform = dem_src.transform

    def _write_single(out_path: Path, data, meta=None):
        m = (meta if meta is not None else ref_meta).copy()
        m.update({"count": 1, "dtype": "float32"})
        with rasterio.open(out_path, "w", **m) as dst:
            dst.write(data.astype(np.float32), 1)

    output_tifs = []
    per_date_results = []

    for s2_tif in s2_tifs:
        try:
            with rasterio.open(str(s2_tif)) as s2_src:
                s2_data = s2_src.read().astype(np.float64)

            # Defensiver Shape-Check: S2 (per Band) und DEM muessen das
            # IDENTISCHE 2D-Grid haben. Sonst crasht das numpy-Broadcasting
            # mit einer wenig hilfreichen Meldung; wir fangen das frueh ab
            # und sagen explizit was nicht passt.
            if s2_data.ndim != 3:
                raise RuntimeError(
                    f"S2-Raster {s2_tif.name} hat ndim={s2_data.ndim}, "
                    f"erwartet 3 (bands, height, width)."
                )
            if s2_data.shape[1:] != dem_data.shape:
                raise RuntimeError(
                    f"Shape-Mismatch zwischen S2 und DEM: "
                    f"S2 {s2_tif.name} hat Shape {s2_data.shape[1:]}, "
                    f"DEM hat Shape {dem_data.shape}. "
                    f"Beide muessen auf dem gleichen Grid liegen - "
                    f"in run_strategy_local_reference muss das DEM via "
                    f"reproject_dem_to_grid auf das S2-Grid reprojiziert "
                    f"werden, nicht unabhaengig via reproject_dem_local."
                )

            if workflow in ("merge_add", "resample"):
                result = s2_data[0] + dem_data
                _write_single(out_dir / s2_tif.name, result)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow == "subtract":
                result = s2_data[0] - dem_data
                _write_single(out_dir / s2_tif.name, result)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow == "mask":
                if s2_data.shape[0] < 2:
                    raise RuntimeError(
                        f"workflow=mask erwartet 2 Baender (B04+SCL), "
                        f"in {s2_tif.name} sind nur {s2_data.shape[0]}"
                    )
                b04 = s2_data[0]
                scl = s2_data[1].astype(int)
                keep = np.isin(scl, (4, 5))
                b04_masked = np.where(keep, b04, np.nan)
                result = b04_masked + dem_data
                _write_single(out_dir / s2_tif.name, result)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow == "focal":
                combined = s2_data[0] + dem_data
                result = _box3_mean(combined)
                _write_single(out_dir / s2_tif.name, result)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow == "filter_bbox":
                combined = s2_data[0] + dem_data
                h, w = combined.shape
                i0, i1 = h // 4, h - h // 4
                j0, j1 = w // 4, w - w // 4
                result = combined[i0:i1, j0:j1]
                crop_meta = ref_meta.copy()
                crop_meta.update({
                    "count":  1,
                    "dtype":  "float32",
                    "height": result.shape[0],
                    "width":  result.shape[1],
                    "transform": Affine(
                        ref_transform.a, 0,
                        ref_transform.c + j0 * ref_transform.a,
                        0, ref_transform.e,
                        ref_transform.f + i0 * ref_transform.e,
                    ),
                })
                _write_single(out_dir / s2_tif.name, result, meta=crop_meta)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow == "aggregation":
                per_date_results.append(s2_data[0] + dem_data)

            else:
                raise ValueError(
                    f"workflow={workflow} ist lokal nicht implementiert."
                )
        except Exception as exc:
            # Klare Fehlermeldung mit Dateinamen und Workflow, BEVOR die
            # Exception nach oben propagiert. So weiss man sofort, an
            # welcher S2-Datei + welcher Operation es haengt.
            print(f"\n  FEHLER in _apply_local_workflow "
                  f"(workflow={workflow}, file={s2_tif.name}): "
                  f"{type(exc).__name__}: {exc}")
            raise

    if workflow == "aggregation" and per_date_results:
        # Temporal mean. CDSE-Output-Dateiname fuer aggregation ist nicht
        # garantiert; wir schreiben das mean-Ergebnis unter JEDER Date-
        # Dateinamen, damit der Accuracy-Check den match auf die tatsaechliche
        # CDSE-Datei zuverlaessig findet, unabhaengig von der Naming-
        # Konvention des Backends.
        import numpy as np
        stacked = np.stack(per_date_results, axis=0)
        mean = np.nanmean(stacked, axis=0)
        for s2_tif in s2_tifs:
            _write_single(out_dir / s2_tif.name, mean)
            output_tifs.append(out_dir / s2_tif.name)

    return output_tifs


def run_strategy_local_reference(args, repeat_idx: int) -> dict:
    """
    local_reference: KOMPLETT lokale Berechnung (S2 + DEM) als unabhaengige
    Ground-Truth gegen die alle CDSE-Strategien per --reference-check
    verglichen werden koennen. Kein CDSE-Workflow-Job - nur die beiden
    Downloads sind CDSE.

    Schritte:
      1. S2 von CDSE herunterladen (load_collection + save_result)
      2. DEM von CDSE herunterladen
      3. Beide lokal mit rasterio auf Ziel-CRS / 10 m / --local-resampling
         reprojizieren (definierte, dokumentierte Reprojektions-Settings)
      4. Workflow-Operation lokal mit numpy ausfuehren
      5. Ergebnis-TIFs schreiben (gleiche Dateinamen wie CDSE-S2 -> direkter
         Filename-Match im Accuracy-Check)

    preprocessing_time = s2_download + dem_download + reprojection + operation.
    total_time = preprocessing_time (kein CDSE-Job hier).
    """
    run_type = _run_type_for(repeat_idx, args.run_type)
    base = _make_outdir(args.output_dir, "local_reference")
    region = args.region
    region_epsg = REGIONS[region]["epsg"]

    if getattr(args, "target_crs", None):
        target_crs_str = _normalize_crs(args.target_crs)
    else:
        target_crs_str = f"EPSG:{region_epsg}"

    # Marker-Scenario-JSON: enthaelt den equivalenten onthefly-Process-Graph
    # plus ein _local_reference-Metadaten-Objekt. Wird von
    # _detect_folder_region / _detect_folder_workflow gefunden, ohne dass das
    # Backend ihn ausgefuehrt hat.
    marker_scenario_path = base / f"local_reference_{region}.json"
    template = _load_bench_template(region, args.extent_size)
    marker_pg = _build_workflow_pg(template, args.workflow, region=region)
    with open(marker_scenario_path, "w") as f:
        json.dump({
            "process_graph": marker_pg,
            "_local_reference": {
                "target_crs": target_crs_str,
                "resampling": args.local_resampling,
                "target_resolution_m": 10.0,
                "workflow": args.workflow,
            },
        }, f, indent=2)

    print(f"\n{'='*60}")
    print(f"  Strategie: local_reference  |  Region: {region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  Target-CRS: {target_crs_str}  |  Resampling: {args.local_resampling}")

    try:
        # Schritt 1: S2 von CDSE
        print(f"\n  [Schritt 1/4] S2 von CDSE herunterladen ({region}, {args.extent_size})...")
        s2_dl_dir = base / "step1_s2_download"
        s2_dl_dir.mkdir()
        s2_scenario = build_s2_download_scenario(
            region, base / "scenario_s2_download.json",
            extent_size=args.extent_size, workflow=args.workflow,
        )
        s2_results = run_openeo(args.api_url, str(s2_scenario), str(s2_dl_dir),
                                job_timeout=args.job_timeout)
        s2_download_time = s2_results.get("total_time") or 0.0
        s2_tifs = sorted(Path(p) for p in glob.glob(str(s2_dl_dir / "*.tif")))
        if not s2_tifs:
            raise RuntimeError(f"Keine S2-TIFs heruntergeladen in {s2_dl_dir}")
        print(f"  {len(s2_tifs)} S2-TIFs heruntergeladen ({s2_download_time:.1f} s)")

        # Schritt 2: DEM von CDSE
        print(f"\n  [Schritt 2/4] DEM von CDSE herunterladen ({region}, {args.extent_size})...")
        dem_dl_dir = base / "step2_dem_download"
        dem_dl_dir.mkdir()
        dem_scenario = build_dem_download_scenario(
            region, base / "scenario_dem_download.json",
            extent_size=args.extent_size,
        )
        dem_results = run_openeo(args.api_url, str(dem_scenario), str(dem_dl_dir),
                                 job_timeout=args.job_timeout)
        dem_download_time = dem_results.get("total_time") or 0.0
        dem_tifs = glob.glob(str(dem_dl_dir / "*.tif"))
        if not dem_tifs:
            raise RuntimeError(f"Kein DEM-TIF heruntergeladen in {dem_dl_dir}")
        dem_tif_raw = dem_tifs[0]
        print(f"  DEM heruntergeladen ({dem_download_time:.1f} s)")

        # Schritt 3: lokale Reprojektion. Reihenfolge ist wichtig:
        #   1. ZUERST S2 reprojizieren - das definiert das Ziel-Grid (Transform,
        #      Width, Height, CRS), inkl. dem 10 m S2-Snap.
        #   2. DANN das DEM auf EXAKT das S2-Grid reprojizieren
        #      (reproject_dem_to_grid - keine eigene Snap-Logik mehr).
        # Frueher wurden beide unabhaengig via reproject_dem_local gesnappt,
        # was bei leicht unterschiedlichen Source-Bounds zu (1139,1047) vs
        # (1136,1044) Shape-Mismatches fuehrte und _apply_local_workflow zum
        # Crash brachte.
        print(f"\n  [Schritt 3/4] Lokale Reprojektion (rasterio, "
              f"{args.local_resampling}, 10 m, {target_crs_str})...")
        repro_dir = base / "step3_reprojected"
        repro_dir.mkdir()
        t_repro_start = time.time()

        s2_repro_tifs = []
        for s2_tif in s2_tifs:
            out = repro_dir / s2_tif.name
            reproject_dem_local(
                str(s2_tif), str(out),
                dst_crs=target_crs_str, resampling=args.local_resampling,
                target_resolution=10.0,
            )
            s2_repro_tifs.append(out)

        # Ziel-Grid vom ersten reprojizierten S2 lesen (alle S2-TIFs derselben
        # Region/Extent haben identisches Grid, weil sie aus demselben CDSE-
        # Download-Job kommen).
        with rasterio.open(str(s2_repro_tifs[0])) as ref:
            target_grid = {
                "crs":       ref.crs,
                "transform": ref.transform,
                "width":     ref.width,
                "height":    ref.height,
            }

        dem_repro = repro_dir / "dem.tif"
        reproject_dem_to_grid(
            dem_tif_raw, str(dem_repro),
            grid=target_grid, resampling=args.local_resampling,
        )
        t_reproject = time.time() - t_repro_start
        print(f"  {len(s2_repro_tifs)} S2 reprojiziert + DEM auf S2-Grid "
              f"({target_grid['width']}x{target_grid['height']}) gesnapped "
              f"({t_reproject:.1f} s)")

        # Schritt 4: lokale Workflow-Operation. Der finale, gemergte Output
        # liegt in step4_result/ - getrennt von den reprojizierten
        # Zwischenrastern in step3_reprojected/, die zufaellig die gleichen
        # openEO_DATE.tif Dateinamen haben.
        print(f"\n  [Schritt 4/4] Lokale Workflow-Operation ({args.workflow})...")
        result_dir = base / "step4_result"
        result_dir.mkdir()
        t_op_start = time.time()
        output_tifs = _apply_local_workflow(
            args.workflow, s2_repro_tifs, dem_repro, result_dir,
        )
        t_operation = time.time() - t_op_start
        if not output_tifs:
            raise RuntimeError(
                f"_apply_local_workflow lieferte 0 Output-TIFs - die "
                f"Workflow-Operation hat nichts geschrieben."
            )
        print(f"  {len(output_tifs)} Output-TIF(s) in {result_dir.name}/ "
              f"geschrieben ({t_operation:.1f} s)")

        preprocessing_time = (
            s2_download_time + dem_download_time + t_reproject + t_operation
        )
        total_time = preprocessing_time  # kein CDSE-Job

        # Minimale results.json fuer import_run().
        results_payload = {
            "backend_url":         "local",
            "backend_name":        "local_rasterio",
            "process_graph":       f"local_reference_{region}",
            "status":              "success",
            "job_id":              None,
            "submit_time":         None,
            "queue_time":          None,
            "processing_time":     None,
            "job_execution_time":  None,
            "download_time":       None,
            "total_time":          None,
            "timestamp":           datetime.now().isoformat(),
            "error":               None,
            "job_status_history":  {},
            "environment":         _collect_environment(),
        }
        with open(base / "results.json", "w") as f:
            json.dump(results_payload, f, indent=2)

        run_id = import_run(
            str(base),
            crs_strategy="local_reference",
            run_type=run_type,
            preprocessing_time=preprocessing_time,
            dem_download_time=dem_download_time,
            s2_download_time=s2_download_time,
            extent_size=args.extent_size,
            workflow=args.workflow,
            local_resampling=args.local_resampling,
            target_crs=target_crs_str,
        )

        return {
            "strategy":            "local_reference",
            "repeat":              repeat_idx + 1,
            "run_type":            run_type,
            "status":              "success",
            "preprocessing_time":  preprocessing_time,
            "total_time":          total_time,
            "run_id":              run_id,
            "outdir":              str(base),
        }
    except Exception as exc:
        print(f"\n{'!'*60}")
        print(f"  FEHLER in run_strategy_local_reference: "
              f"{type(exc).__name__}: {exc}")
        print(f"{'!'*60}")
        import traceback
        traceback.print_exc()
        return {
            "strategy":            "local_reference",
            "repeat":              repeat_idx + 1,
            "run_type":            run_type,
            "status":              "error",
            "preprocessing_time":  None,
            "total_time":          None,
            "run_id":              None,
            "outdir":              str(base),
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
    verschiedene extent_size-Werte (small/medium/large/xlarge/xxlarge) trotzdem
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
    # 1) local_pp/full_pp: scenario_file heisst {strategy_label}_{region}.json
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
        folder / "step5_main" / "processgraph.json",
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


def _detect_pg_workflow(pg: dict):
    """Workflow-Variante aus einem process_graph erkennen, oder None.

    Schaut auf charakteristische Knoten- bzw. overlap_resolver-Signaturen.
    """
    root = pg.get("process_graph", pg)
    if not isinstance(root, dict):
        return None
    if "applykernel1" in root:
        return "focal"
    if "resamplespatial1" in root or "resamplespatial2" in root:
        return "resample"
    if "filterbbox1" in root:
        return "filter_bbox"
    if "reducedimension1" in root:
        return "aggregation"
    if "mask1" in root:
        return "mask"
    merge = root.get("merge1")
    if isinstance(merge, dict):
        ov = merge.get("arguments", {}).get("overlap_resolver")
        if isinstance(ov, dict):
            for node in ov.get("process_graph", {}).values():
                if not isinstance(node, dict):
                    continue
                pid = node.get("process_id")
                if pid == "subtract":
                    return "subtract"
                if pid == "add":
                    return "merge_add"
    return None


def _detect_folder_workflow(folder: Path):
    """Workflow eines Run-Ordners ueber den gespeicherten Process Graph
    bestimmen, oder None. Nur Graphen mit merge1 (oder applykernel1)
    werden ausgewertet, damit reine S2-/DEM-Download-Szenarien (die in
    full_pp Runs ebenfalls als JSON liegen) ignoriert werden.
    """
    candidates = [
        folder / "step5_main" / "processgraph.json",
        folder / "step3_main" / "processgraph.json",
        folder / "processgraph.json",
        folder / "scenario_onthefly.json",
    ]
    candidates += sorted(folder.glob("*.json"))
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if not cand.exists() or not cand.is_file():
            continue
        try:
            pg = json.loads(cand.read_text())
        except Exception:
            continue
        root = pg.get("process_graph", pg)
        if not isinstance(root, dict):
            continue
        if "merge1" not in root and "applykernel1" not in root:
            continue
        wf = _detect_pg_workflow(pg)
        if wf:
            return wf
    return None


def _find_latest_run_dir(base: str, suffix: str, region: str,
                          extent_size: str = None,
                          workflow: str = None):
    """Neuesten outputs/run_*_{suffix} fuer Region zurueckgeben, oder None.

    Wenn extent_size gesetzt ist, werden nur Ordner beruecksichtigt, deren
    Scenario-JSON exakt diesen Extent enthaelt (Bounding-Box-Vergleich).
    Wenn workflow gesetzt ist, muss zusaetzlich der im Process Graph
    erkannte Workflow uebereinstimmen - das verhindert das versehentliche
    Vergleichen verschiedener Workflow-Varianten der gleichen Region/Extent.
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
        if workflow is not None and _detect_folder_workflow(d) != workflow:
            continue
        candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _compare_tif_pair(ref_tif: Path, test_tif: Path,
                      resampling_method: str = "nearest"):
    """Pro Band MAE/RMSE + Coverage, dann gemittelt.

    Returns (mae, rmse, n_bands, valid_pixels, total_pixels). Bei Fehler
    werden alle Werte None / 0.
    """
    try:
        from accuracy_calculator import align_rasters, calculate_metrics
        import numpy as np
    except Exception as exc:
        print(f"  Import-Fehler fuer accuracy_calculator: {exc}")
        return (None, None, 0, 0, 0)

    try:
        ref_data, test_data, _ = align_rasters(
            str(ref_tif), str(test_tif),
            resampling_method=resampling_method,
        )
        results = calculate_metrics(ref_data, test_data)
    except Exception as exc:
        print(f"  Vergleich fehlgeschlagen ({ref_tif.name}): {exc}")
        return (None, None, 0, 0, 0)

    bands = results.get("bands") or []
    if not bands:
        return (None, None, 0, 0, 0)
    mae = float(np.mean([b["MAE"] for b in bands]))
    rmse = float(np.mean([b["RMSE"] for b in bands]))
    valid_pixels = int(sum(b.get("valid_pixels", 0) for b in bands))
    total_pixels = int(sum(b.get("total_pixels", 0) for b in bands))
    return (mae, rmse, len(bands), valid_pixels, total_pixels)


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


# Mapping: Strategie -> (suffix in run_*_{suffix}, Unterordner mit den TIFs).
# Wird sowohl fuer test_strategy als auch fuer reference_strategy genutzt.
# Achtung: local_reference legt seine Zwischenraster (reprojiziertes S2 / DEM)
# in step3_reprojected ab - die haben dieselben openEO_DATE.tif Dateinamen wie
# die finalen Outputs und wuerden den Accuracy-Match verfaelschen. Deshalb
# liegt der FINAL gemergte Output in einem eigenen step4_result/ Ordner.
_ACCURACY_LAYOUT = {
    "onthefly":            ("onthefly",        ""),
    "local_preprocessing": ("local_pp",        "step3_main"),
    "full_preprocessing":  ("full_pp",         "step5_main"),
    "local_reference":     ("local_reference", "step4_result"),
}
# Backward-Compat-Alias (alter Name, falls extern referenziert).
_ACCURACY_TEST_LAYOUT = _ACCURACY_LAYOUT


def _tif_dir(run_dir: Path, strategy: str) -> Path:
    """Verzeichnis mit den finalen Workflow-TIFs eines Run-Ordners."""
    sub = _ACCURACY_LAYOUT[strategy][1]
    return run_dir / sub if sub else run_dir


# Strikt: nur openEO_YYYY-MM-DD*.tif (CDSE-Output und local_reference-Output
# folgen diesem Pattern). dem.tif aus step3_reprojected oder andere
# Hilfsdateien werden so zuverlaessig ausgefiltert.
_ACCURACY_TIF_RE = re.compile(r"^openEO_\d{4}-\d{2}-\d{2}.*\.tif$",
                              re.IGNORECASE)


def _collect_workflow_tifs(tif_dir: Path) -> dict:
    """Sammle finale Workflow-TIFs aus einem Verzeichnis als {name: Path}.
    Filtert auf das openEO_YYYY-MM-DD*.tif Pattern, damit weder dem.tif noch
    sonstige Zwischenraster matchen."""
    return {p.name: p for p in tif_dir.glob("*.tif")
            if _ACCURACY_TIF_RE.match(p.name)}


def run_accuracy_check(output_base: str, region: str,
                       test_strategy: str = None,
                       test_run_id=None, extent_size: str = None,
                       workflow: str = None,
                       resampling_method: str = "nearest",
                       reference_strategy: str = "onthefly"):
    """Neuesten {reference_strategy}-Run vs neuesten {test_strategy}-Run vergleichen.

    reference_strategy: "onthefly" (Default) oder "local_reference" (lokale
    Ground-Truth). Bei "local_reference" werden alle CDSE-Strategien
    (onthefly, local_preprocessing, full_preprocessing) als gueltige
    test_strategy akzeptiert.

    test_strategy: explizit gesetzt oder per Auto-Detect aus dem
    Filesystem. Auto-Detect-Reihenfolge: full_preprocessing >
    local_preprocessing > (bei reference=local_reference) onthefly.
    test_strategy darf NIE gleich reference_strategy sein.

    Es werden nur Runs verglichen, deren Region, extent_size UND Workflow
    uebereinstimmen - damit nicht versehentlich ein alter Run einer anderen
    Konfiguration verglichen wird.

    resampling_method wird an align_rasters durchgereicht und sollte mit
    --local-resampling uebereinstimmen, damit der Accuracy-Vergleich die
    gleiche Resampling-Methode nutzt wie die zu vergleichende Pipeline.

    Speichert den Median(MAE)/Median(RMSE) ueber alle gemeinsamen Date-TIFs in
    die accuracy-Tabelle (run_id des Test-Runs).
    """
    if reference_strategy not in _ACCURACY_LAYOUT:
        raise ValueError(
            f"reference_strategy='{reference_strategy}' nicht bekannt. "
            f"Erlaubt: {sorted(_ACCURACY_LAYOUT)}"
        )

    print(f"\n{'='*60}")
    extent_info = f"  |  Extent: {extent_size}" if extent_size else ""
    workflow_info = f"  |  Workflow: {workflow}" if workflow else ""
    print(f"  Accuracy-Check vs '{reference_strategy}'"
          f"  |  Region: {region}{extent_info}{workflow_info}")

    ref_suffix, _ = _ACCURACY_LAYOUT[reference_strategy]
    reference_dir = _find_latest_run_dir(output_base, ref_suffix, region,
                                         extent_size=extent_size,
                                         workflow=workflow)

    # test_strategy auto-detecten: bevorzugt full_pp, dann local_pp, dann
    # (wenn reference != onthefly) auch onthefly. reference selbst ist
    # ausgeschlossen.
    if test_strategy is None:
        if reference_strategy == "onthefly":
            candidates = ("full_preprocessing", "local_preprocessing")
        else:
            candidates = ("full_preprocessing", "local_preprocessing", "onthefly")
        for cand in candidates:
            if cand == reference_strategy:
                continue
            suf, _ = _ACCURACY_LAYOUT[cand]
            if _find_latest_run_dir(output_base, suf, region,
                                    extent_size=extent_size,
                                    workflow=workflow) is not None:
                test_strategy = cand
                break

    if test_strategy not in _ACCURACY_LAYOUT:
        print(f"  Skip: keine passende Test-Strategie gefunden.")
        return None
    if test_strategy == reference_strategy:
        print(f"  Skip: test_strategy == reference_strategy "
              f"('{test_strategy}').")
        return None

    test_suffix, _ = _ACCURACY_LAYOUT[test_strategy]
    test_dir = _find_latest_run_dir(output_base, test_suffix, region,
                                    extent_size=extent_size,
                                    workflow=workflow)

    if not reference_dir or not test_dir:
        miss = reference_strategy if not reference_dir else test_strategy
        extent_msg = f" mit extent_size='{extent_size}'" if extent_size else ""
        wf_msg = f" und workflow='{workflow}'" if workflow else ""
        print(f"  Skip: kein {miss}-Run fuer Region '{region}'"
              f"{extent_msg}{wf_msg} gefunden.")
        return None

    reference_tif_dir = _tif_dir(reference_dir, reference_strategy)
    test_tif_dir     = _tif_dir(test_dir, test_strategy)

    print(f"  Referenz ({reference_strategy}): {reference_dir.name}")
    print(f"  Test ({test_strategy}): {test_dir.name}")
    print(f"  Resampling-Methode:       {resampling_method}")

    reference_tifs = _collect_workflow_tifs(reference_tif_dir)
    test_tifs      = _collect_workflow_tifs(test_tif_dir)
    common = sorted(set(reference_tifs) & set(test_tifs))
    if not common:
        print(f"  Skip: keine gemeinsamen TIF-Dateien.")
        print(f"    {reference_strategy} TIFs: {sorted(reference_tifs)}")
        print(f"    {test_strategy} TIFs: {sorted(test_tifs)}")
        return None

    per_mae, per_rmse, n_bands_last = [], [], 0
    per_valid, per_total = [], []
    for name in common:
        mae, rmse, n_bands, valid_px, total_px = _compare_tif_pair(
            reference_tifs[name], test_tifs[name],
            resampling_method=resampling_method,
        )
        if mae is None:
            continue
        per_mae.append(mae)
        per_rmse.append(rmse)
        per_valid.append(valid_px)
        per_total.append(total_px)
        n_bands_last = n_bands
        cov_pct = (100.0 * valid_px / total_px) if total_px else float("nan")
        print(f"    {name}: MAE={mae:.6f}, RMSE={rmse:.6f}  "
              f"({n_bands} Bands, {valid_px:,}/{total_px:,} Pixel = {cov_pct:.1f}%)")

    if not per_mae:
        print("  Skip: kein valider Pixel-Vergleich moeglich.")
        return None

    median_mae = statistics.median(per_mae)
    median_rmse = statistics.median(per_rmse)
    valid_total = sum(per_valid)
    total_total = sum(per_total)
    coverage_pct = (100.0 * valid_total / total_total) if total_total else None

    run_id = test_run_id
    if run_id is None:
        run_id = _lookup_run_id_for_dir(test_tif_dir)
    if run_id is not None:
        _persist_accuracy(run_id, median_mae, median_rmse, str(reference_dir))
    else:
        print(f"  WARNUNG: kein run_id fuer {test_strategy} gefunden, "
              f"nicht in DB geschrieben.")

    cov_str = f"{coverage_pct:.1f}%" if coverage_pct is not None else "n/a"
    print(f"\n  Accuracy-Check: MAE={median_mae:.6f}, RMSE={median_rmse:.6f} "
          f"({len(per_mae)} Dates, {n_bands_last} Bands, "
          f"Coverage {valid_total:,}/{total_total:,} = {cov_str}, "
          f"resampling={resampling_method})")

    return {
        "region": region,
        "reference_strategy": reference_strategy,
        "test_strategy": test_strategy,
        "resampling_method": resampling_method,
        "mae": median_mae,
        "rmse": median_rmse,
        "n_dates": len(per_mae),
        "n_bands": n_bands_last,
        "valid_pixels": valid_total,
        "total_pixels": total_total,
        "coverage_percent": coverage_pct,
        "run_id": run_id,
        "reference_dir": str(reference_dir),
        "test_dir": str(test_dir),
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
                        choices=ALL_STRATEGIES + EXTRA_STRATEGIES + ["all"],
                        help="Strategie(n) ausfuehren. 'all' = onthefly + "
                             "local_preprocessing (full_preprocessing nur "
                             "separat, weil deutlich laenger).")
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
                             "dem neuesten onthefly- und local_pp/full_pp-Output "
                             "fuer die Region ausfuehren. Mit --repeat 0 auch "
                             "standalone auf existierenden Outputs verwendbar.")
    parser.add_argument("--reference-check", action="store_true",
                        help="Vergleicht JEDE in dieser Session gelaufene CDSE-"
                             "Strategie (onthefly, local_preprocessing, "
                             "full_preprocessing) gegen den neuesten "
                             "local_reference-Run der gleichen Region/Workflow/"
                             "Extent. Setzt voraus dass ein local_reference-Run "
                             "existiert (entweder in dieser Session via "
                             "--strategy local_reference oder ein frueherer). "
                             "Unterscheidet sich von --accuracy-check dadurch, "
                             "dass die unabhaengige lokale Pipeline als "
                             "Ground-Truth dient statt onthefly.")
    parser.add_argument("--extent-size", default="medium",
                        choices=("small", "medium", "large", "xlarge", "xxlarge"),
                        help="AOI-Kantenlaenge um das Region-Zentrum: "
                             "small=5km, medium=10km (Default = bisheriger fester "
                             "REGIONS-Extent, rueckwaertskompatibel), large=50km, "
                             "xlarge=100km, xxlarge=200km (ueberschreitet die "
                             "CDSE-Tile-Grenze von 120km und macht die "
                             "Tile-Boundary-Penalty messbar). Wirkt auf "
                             "onthefly, DEM-Download und local_pp Szenarien "
                             "sowie das STAC Item.")
    parser.add_argument("--workflow", default="merge_add",
                        choices=WORKFLOWS,
                        help="openEO Workflow: "
                             "merge_add (Default, B04+DEM via merge_cubes/add), "
                             "subtract (B04-DEM via merge_cubes/subtract), "
                             "mask (B04 mit SCL Cloud-Mask, SCL not in {4,5} "
                             "wird maskiert, dann B04+DEM/add), "
                             "aggregation (B04+DEM/add, dann temporal mean), "
                             "focal (B04+DEM/add, dann 3x3 mean apply_kernel), "
                             "resample (DEM CDSE-seitig nach EPSG:3035@30m und "
                             "zurueck nach Region-UTM@10m, dann B04+DEM/add), "
                             "filter_bbox (B04+DEM/add, dann filter_bbox auf "
                             "die mittleren 50% des Extents).")
    parser.add_argument("--local-resampling", default="nearest",
                        choices=tuple(LOCAL_RESAMPLING.keys()),
                        help="Resampling-Methode fuer die lokale DEM-Reprojektion "
                             "(nur local_preprocessing). nearest (Default) ist "
                             "pixelidentisch zu CDSE - der Accuracy-Check liefert "
                             "dann MAE=RMSE=0. bilinear/cubic weichen vom "
                             "CDSE-Output ab und machen den Accuracy-Check "
                             "aussagekraeftig.")
    parser.add_argument("--dem-layout", default="striped",
                        choices=DEM_LAYOUTS,
                        help="Interne Struktur des reprojizierten DEM-GeoTIFF "
                             "vor dem Upload (nur local_preprocessing). "
                             "striped (Default): gestreift, unkomprimiert, keine "
                             "Overviews (bisheriges Verhalten). "
                             "tiled_uncompressed: gekachelt 128x128, "
                             "unkomprimiert, keine Overviews. "
                             "cog: gekachelt 128x128, deflate, interne Overviews. "
                             "Nur das Schreibprofil aendert sich - die Pixelwerte "
                             "sind ueber alle Varianten identisch.")
    parser.add_argument("--target-crs", default=None,
                        help="Ziel-CRS fuer die lokale DEM-Reprojektion. "
                             "local_preprocessing: Default = UTM-EPSG der Region "
                             "+ 10 m S2-Grid-Snap. full_preprocessing: Default "
                             "= DEM wird exakt auf das S2-Grid (UTM) gesnapped. "
                             "Override z.B. EPSG:3035 (LAEA) oder EPSG:4326 "
                             "(WGS84) erzwingt eine echte CRS-Transformation "
                             "CDSE-seitig beim merge_cubes.")
    parser.add_argument("--reproject-s2", action="store_true",
                        help="Nur fuer full_preprocessing + --target-crs: "
                             "Auch die S2-Raster lokal nach --target-crs "
                             "reprojizieren (Szenario 3: BEIDE Raster im "
                             "Nicht-UTM-Ziel-CRS).")
    parser.add_argument("--job-timeout", type=int, default=3600,
                        help="Maximale Wartezeit in Sekunden fuer einen "
                             "CDSE-Job (Default: 3600 = 1h). Bei xxlarge "
                             "(200km) oder grossen Workflows ggf. hoeher "
                             "setzen, z.B. --job-timeout 7200.")
    parser.add_argument("--host", default=None,
                        help="ssh/scp Ziel fuer Asset-Uploads (z.B. "
                             "root@dima-prox.dima.tu-berlin.de). Default: "
                             "ENV BENCHMARK_HOST oder root@46.224.62.97.")
    parser.add_argument("--web-path", default=None,
                        help="Remote-Pfad fuer das Web-Verzeichnis "
                             "(trailing slash). Default: ENV "
                             "BENCHMARK_WEB_PATH oder /var/www/benchmark-data/.")
    parser.add_argument("--url-base", default=None,
                        help="Oeffentliche URL-Basis fuer Assets/STAC "
                             "(trailing slash). Default: ENV "
                             "BENCHMARK_URL_BASE oder "
                             "http://46.224.62.97/benchmark-data/.")
    parser.add_argument("--include-full-pp", default="auto",
                        choices=("auto", "yes", "no"),
                        help="Steuert ob full_preprocessing bei "
                             "--strategy all mitlaeuft. auto (Default): "
                             "ja bei extent in {small,medium,large}, nein "
                             "bei {xlarge,xxlarge} (zu viele Range-Requests "
                             "-> Timeouts). yes: immer einbeziehen. "
                             "no: nie einbeziehen.")

    args = parser.parse_args()

    global HETZNER_HOST, HETZNER_WEB_PATH, HETZNER_URL_BASE
    if args.host:
        HETZNER_HOST = args.host
    if args.web_path:
        HETZNER_WEB_PATH = _ensure_trailing_slash(args.web_path)
    if args.url_base:
        HETZNER_URL_BASE = _ensure_trailing_slash(args.url_base)

    strategies = ALL_STRATEGIES if args.strategy == "all" else [args.strategy]

    # Safeguard: full_preprocessing bei grossen Extents per Default ausnehmen,
    # weil die Anzahl Range-Requests pro xlarge-Run schon ~1170 erreicht
    # (gemessen in nginx_access_log) und der Backend-Job dadurch regelmaessig
    # timeoutet. --include-full-pp=yes uebersteuert das.
    if args.strategy == "all" and "full_preprocessing" in strategies:
        if args.include_full_pp == "no":
            print(f"\n[--include-full-pp=no] full_preprocessing wird ausgelassen.")
            strategies = [s for s in strategies if s != "full_preprocessing"]
        elif (args.include_full_pp == "auto"
              and args.extent_size in LARGE_EXTENTS_FOR_FULL_PP):
            print(f"\n[WARNUNG] full_preprocessing wird fuer extent="
                  f"{args.extent_size} automatisch uebersprungen: zu viele "
                  f"Range-Requests, der CDSE-Job wuerde timeouten. "
                  f"Mit --include-full-pp=yes erzwingen.")
            strategies = [s for s in strategies if s != "full_preprocessing"]

    print(f"\nBenchmark gestartet: {datetime.now().isoformat()}")
    print(f"API-URL:    {args.api_url}")
    print(f"Region:     {args.region}  (EPSG:{REGIONS[args.region]['epsg']})")
    print(f"Extent:     {args.extent_size}  ({SIZE_KM[args.extent_size]:.0f} km Kantenlaenge)")
    print(f"Workflow:   {args.workflow}")
    print(f"Local-Resampling: {args.local_resampling}")
    if args.target_crs:
        print(f"Target-CRS: {_normalize_crs(args.target_crs)}  (Override)")
    else:
        print(f"Target-CRS: EPSG:{REGIONS[args.region]['epsg']}  (Region-Default UTM)")
    print(f"Strategien: {strategies}")
    print(f"Repeats:    {args.repeat}")
    print(f"Run-Type:   {args.run_type}")

    all_results = []
    runners = {
        "onthefly": run_strategy_onthefly,
        "local_preprocessing": run_strategy_local_pp,
        "full_preprocessing": run_strategy_full_pp,
        "local_reference": run_strategy_local_reference,
    }

    for strategy in strategies:
        for i in range(args.repeat):
            result = runners[strategy](args, i)
            all_results.append(result)

    print_summary(all_results)

    if args.accuracy_check:
        test_strategy = None
        test_run_id = None
        for r in all_results:
            if r.get("status") != "success" or r.get("run_id") is None:
                continue
            if r.get("strategy") in ("local_preprocessing", "local_pp_cached"):
                test_strategy = "local_preprocessing"
                test_run_id = r["run_id"]
            elif r.get("strategy") == "full_preprocessing":
                test_strategy = "full_preprocessing"
                test_run_id = r["run_id"]
        run_accuracy_check(args.output_dir, args.region,
                           test_strategy=test_strategy,
                           test_run_id=test_run_id,
                           extent_size=args.extent_size,
                           workflow=args.workflow,
                           resampling_method=args.local_resampling,
                           reference_strategy="onthefly")

    if args.reference_check:
        # Pro CDSE-Strategie (onthefly, local_pp, full_pp) einen Vergleich
        # gegen den neuesten local_reference-Run derselben Region/Workflow/
        # Extent. test_run_ids werden aus all_results bezogen, falls die
        # Strategie in dieser Session lief; sonst per Disk-Lookup.
        test_run_ids = {}
        for r in all_results:
            if r.get("status") != "success" or r.get("run_id") is None:
                continue
            s = r.get("strategy")
            if s in ("local_pp_cached",):
                s = "local_preprocessing"
            if s in ("onthefly", "local_preprocessing", "full_preprocessing"):
                test_run_ids[s] = r["run_id"]

        candidate_strategies = ("onthefly", "local_preprocessing",
                                "full_preprocessing")
        any_run = False
        for s in candidate_strategies:
            suf, _ = _ACCURACY_LAYOUT[s]
            if _find_latest_run_dir(args.output_dir, suf, args.region,
                                    extent_size=args.extent_size,
                                    workflow=args.workflow) is None:
                continue
            any_run = True
            run_accuracy_check(
                args.output_dir, args.region,
                test_strategy=s,
                test_run_id=test_run_ids.get(s),
                extent_size=args.extent_size,
                workflow=args.workflow,
                resampling_method=args.local_resampling,
                reference_strategy="local_reference",
            )
        if not any_run:
            print("\n[--reference-check] Keine CDSE-Strategie-Runs gefunden "
                  "die gegen local_reference verglichen werden koennten.")


if __name__ == "__main__":
    main()

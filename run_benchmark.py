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
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import re

import rasterio
from rasterio.transform import Affine, array_bounds
from rasterio.warp import (Resampling, calculate_default_transform, reproject,
                           transform_bounds)

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

# Workflows fuer KONTINUIERLICHE Zweitraster (DEM). 'merge_add' = bisheriges
# Verhalten. Rechnen alle arithmetisch auf beiden Cubes.
CONTINUOUS_WORKFLOWS = ("merge_add", "subtract", "mask", "aggregation", "focal",
                        "resample", "filter_bbox")

# Workflows fuer KATEGORIALE Zweitraster (Landbedeckung). Arithmetik ueber
# Klassen-IDs ist bedeutungslos (Klasse 10 + Reflektanz 2742 ist keine
# Groesse), deshalb ein eigener Satz:
#   lc_overlay - merge_cubes bleibt erhalten, damit dieselbe Gitter-
#                Aushandlung zwischen S2- und Zweitcube stattfindet wie im
#                bisherigen Benchmark; der overlap_resolver reicht aber den
#                Zweitcube durch statt zu addieren. Ergebnis ist die
#                KLASSENKARTE auf dem S2-Gitter - jedes abweichende Pixel
#                ist ein Transformationsartefakt, unvermischt. Primaerbeleg.
#   lc_mask    - B04 auf eine Zielklasse maskiert (realistischer
#                Anwendungsfall). Gemessen wird die VALIDITAET (Maske
#                getroffen ja/nein), nicht der B04-Wert: wo beide Laeufe
#                gueltig sind, sind die Werte ohnehin identisch, und ein
#                MAE ueber die gueltigen Pixel wuerde die Maskenkante -
#                also genau das Signal - strukturell ausblenden.
CATEGORICAL_WORKFLOWS = ("lc_overlay", "lc_mask")

WORKFLOWS = CONTINUOUS_WORKFLOWS + CATEGORICAL_WORKFLOWS

# ---------------------------------------------------------------------------
# Datensatz-Paare (--dataset). Das erste Raster ist immer Sentinel-2 B04;
# variabel ist das ZWEITE Raster.
#
# 'dem' ist der historische, fest verdrahtete Fall - die Werte hier
# entsprechen exakt dem, was in scenarios/bench_onthefly_{region}.json
# steht. Deshalb wird bei dataset='dem' KEINE Substitution ausgefuehrt
# (_apply_dataset_to_pg steigt sofort aus) und der Graph bleibt
# byte-identisch.
#
# Namens-Hinweis: viele Funktionen und DB-Spalten heissen historisch
# "dem_*" (dem_layout, dem_format, dem_tiles, _get_or_download_dem, ...).
# Sie meinen seit --dataset generisch DAS ZWEITE RASTER. Bewusst nicht
# umbenannt: das erzeugte einen riesigen Diff und braeche die
# DB-Kompatibilitaet zu allen bisherigen Laeufen, ohne Evidenz zu liefern.
# ---------------------------------------------------------------------------
DATASETS = {
    "dem": {
        "collection": "COPERNICUS_30",
        "band": "DEM",
        # identisch zum Template - siehe Kommentar oben
        "temporal_extent": ["2010-12-12", "2015-01-16"],
        "categorical": False,
        "stac_datetime": "2011-01-06T00:00:00Z",
        "workflows": CONTINUOUS_WORKFLOWS,
        "default_workflow": "merge_add",
        "label": "COPERNICUS_30 (Hoehe, kontinuierlich)",
        # Unveraendert die bisherige Auswahl. 'mode' ist hier NICHT sinnvoll:
        # eine Mehrheitsentscheidung ueber Hoehenwerte verwirft Information,
        # statt zu mitteln.
        "resampling": ("nearest", "bilinear", "cubic"),
    },
    "landcover": {
        "collection": "ESA_WORLDCOVER_10M_2021_V2",
        "band": "MAP",
        "temporal_extent": ["2021-01-01", "2021-12-31"],
        "categorical": True,
        "stac_datetime": "2021-01-01T00:00:00Z",
        "workflows": CATEGORICAL_WORKFLOWS,
        "default_workflow": "lc_overlay",
        "label": "ESA_WORLDCOVER_10M_2021_V2 (Landbedeckung, kategorial)",
        # nearest (jedes Zielpixel uebernimmt genau eine Quellklasse) und
        # mode (haeufigste Klasse im Quellfenster). mode ist beim
        # VERGROEBERN das fachlich richtige Verfahren - nearest greift dort
        # willkuerlich einen einzelnen Quellpixel heraus. bilinear/cubic
        # bleiben ausgeschlossen: sie mitteln Klassen-IDs.
        "resampling": ("nearest", "mode"),
        # uint8, nodata 0, 1 Band, nativ EPSG:4326 @ 8.333e-05 Grad (~10 m).
        # Gegen CDSE verifiziert; beobachtete Klassen im Testausschnitt:
        # 10 Baum, 30 Gras, 40 Acker, 50 bebaut, 60 vegetationsarm, 80 Wasser.
        "dtype": "uint8",
        "nodata": 0,
        # Gueltige ESA-WorldCover-Klassen. Dient der INHALTLICHEN Pruefung
        # des CDSE-Ergebnisses: der erste Serverlauf meldete success und
        # lieferte trotzdem S2-Reflexionswerte. Ein Ergebnis, dessen Werte
        # nicht in dieser Menge liegen, ist keine Klassenkarte.
        "classes": (10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100),
    },
}

DEFAULT_DATASET = "dem"

# Zielklasse fuer lc_mask: 10 = Tree cover (ESA WorldCover). Bewusst eine
# haeufige Klasse - eine seltene liefert zu wenig Maskenkante, um den
# Transformationsfehler sichtbar zu machen.
LC_MASK_CLASS = 10


def _dataset_of(args) -> str:
    """Datensatz-Paar aus den CLI-Args, mit Default und Pruefung."""
    ds = getattr(args, "dataset", None) or DEFAULT_DATASET
    if ds not in DATASETS:
        raise ValueError(
            f"Unbekanntes --dataset: {ds!r}. Erlaubt: {sorted(DATASETS)}")
    return ds


def _is_categorical(dataset: str) -> bool:
    """True, wenn das zweite Raster Klassen statt Messwerte traegt."""
    return bool(DATASETS[dataset]["categorical"])


def _categorical_output(dataset: str, workflow: str) -> bool:
    """True, wenn der ERGEBNIS-Raster des Workflows kategorial zu vergleichen
    ist (Uebereinstimmungsquote statt MAE/RMSE).

    Gilt fuer beide Landcover-Workflows: lc_overlay liefert Klassen-IDs,
    lc_mask liefert zwar B04-Werte, aber die Aussage steckt in der
    Maskenkante - s. Kommentar bei CATEGORICAL_WORKFLOWS.
    """
    return _is_categorical(dataset) and workflow in CATEGORICAL_WORKFLOWS


def _validate_dataset_workflow(dataset: str, workflow: str) -> None:
    """Bricht ab, wenn Datensatz und Workflow nicht zusammenpassen.

    Harte Ablehnung statt stiller Korrektur: ein arithmetischer Workflow auf
    Klassen-IDs (oder umgekehrt) wuerde durchlaufen und plausibel aussehende,
    aber bedeutungslose Zahlen liefern.
    """
    allowed = DATASETS[dataset]["workflows"]
    if workflow not in allowed:
        raise ValueError(
            f"--workflow {workflow!r} ist mit --dataset {dataset!r} nicht "
            f"kombinierbar. Erlaubt: {', '.join(allowed)}. "
            f"(Arithmetik auf Klassen-IDs bzw. kategoriale Workflows auf "
            f"kontinuierlichen Daten ergeben keine sinnvollen Werte.)")


def _validate_dataset_resampling(dataset: str, resampling: str) -> None:
    """Prueft, ob die Resampling-Methode zum Datensatz passt.

    Kategorial: nur 'nearest' und 'mode'. bilinear/cubic mitteln Klassen-IDs
    und erfinden dabei Klassen, die es nicht gibt (zwischen 10 Baum und
    50 bebaut laege 30 Gras). 'mode' waehlt dagegen die haeufigste Klasse im
    Quellfenster und ist beim Vergroebern das fachlich richtige Verfahren.

    Kontinuierlich: unveraendert nearest/bilinear/cubic.

    Harte Ablehnung, nicht stille Korrektur - sonst stuende in der DB eine
    Laufkonfiguration, die nicht gefahren wurde.
    """
    allowed = DATASETS[dataset]["resampling"]
    if resampling not in allowed:
        if _is_categorical(dataset):
            grund = ("kategoriale Daten duerfen nicht interpoliert werden - "
                     "bilinear/cubic mischen Klassen-IDs zu nicht "
                     "existierenden Klassen")
        else:
            grund = ("'mode' ist eine Mehrheitsentscheidung und fuer "
                     "kontinuierliche Messwerte nicht sinnvoll")
        raise ValueError(
            f"--local-resampling {resampling!r} ist mit --dataset "
            f"{dataset!r} nicht zulaessig ({grund}). "
            f"Erlaubt: {', '.join(allowed)}.")


def _apply_dataset_to_pg(pg: dict, dataset: str) -> None:
    """Ersetzt in-place die Zweitraster-Kollektion in loadcollection2.

    Bei dataset='dem' passiert NICHTS - die Templates tragen diesen Fall
    bereits, und ein No-Op garantiert byte-identische Graphen.
    """
    if dataset == DEFAULT_DATASET:
        return
    info = DATASETS[dataset]
    node = pg.get("loadcollection2")
    if not isinstance(node, dict):
        return
    args = node.setdefault("arguments", {})
    args["id"] = info["collection"]
    args["temporal_extent"] = list(info["temporal_extent"])


def verify_lc_overlay_graph(pg: dict, dataset: str = "landcover") -> list:
    """Strukturpruefung fuer lc_overlay: liefert der Graph nachweisbar den
    KLASSEN-Cube als Ergebnis?

    Gibt eine Liste von Beanstandungen zurueck (leer = in Ordnung).

    Hintergrund: der erste Serverlauf meldete success und lieferte trotzdem
    S2-Reflexionswerte. So ein Fehler faellt weder dem Backend noch dem
    Benchmark auf - deshalb hier eine Pruefung, die den Ergebnispfad
    RUECKWAERTS von save_result verfolgt und belegt, dass er auf dem
    Klassenband endet.

    Geprueft wird:
      1. save_result haengt an filter_bands auf dem Klassenband.
      2. dessen Datenquelle ist merge_cubes.
      3. merge1 hat KEINEN overlap_resolver (sonst waere die Band-Auswahl
         wieder von der x/y-Bindung des Resolvers abhaengig).
      4. die Band-Labels der beiden Cubes sind DISJUNKT - der Zweitcube
         wird auf den Klassen-Bandnamen umbenannt, nicht auf B04.
      5. zwischen Klassen-Cube und save_result liegt KEIN rechnender
         Prozess (add/subtract/multiply/divide) - genau dort ist der
         Fehler zuletzt entstanden.
    """
    band = DATASETS[dataset]["band"]
    root = pg.get("process_graph", pg)
    problems = []

    def node_of(ref):
        if isinstance(ref, dict) and "from_node" in ref:
            return ref["from_node"], root.get(ref["from_node"])
        return None, None

    save = next((n for n in root.values()
                 if isinstance(n, dict) and n.get("process_id") == "save_result"),
                None)
    if save is None:
        return ["kein save_result im Graphen"]

    fb_name, fb = node_of(save.get("arguments", {}).get("data"))
    if not fb or fb.get("process_id") != "filter_bands":
        problems.append(
            f"save_result haengt nicht an filter_bands, sondern an "
            f"{fb.get('process_id') if fb else None!r} ({fb_name!r})")
        return problems
    if fb["arguments"].get("bands") != [band]:
        problems.append(
            f"filter_bands waehlt {fb['arguments'].get('bands')!r} "
            f"statt [{band!r}]")

    mg_name, mg = node_of(fb["arguments"].get("data"))
    if not mg or mg.get("process_id") != "merge_cubes":
        problems.append(
            f"filter_bands liest nicht aus merge_cubes, sondern aus "
            f"{mg.get('process_id') if mg else None!r}")
        return problems
    if "overlap_resolver" in mg.get("arguments", {}):
        problems.append(
            "merge_cubes hat einen overlap_resolver - die Bandauswahl "
            "haengt dann wieder an dessen x/y-Bindung")

    # Band-Labels beider Cubes ermitteln: cube1 = S2 (load_collection.bands),
    # cube2 = Zweitcube (rename_labels.target).
    _, c1 = node_of(mg["arguments"].get("cube1"))
    c1_bands = (c1 or {}).get("arguments", {}).get("bands")
    rn = next((n for n in root.values()
               if isinstance(n, dict) and n.get("process_id") == "rename_labels"),
              None)
    c2_bands = (rn or {}).get("arguments", {}).get("target")
    if c2_bands != [band]:
        problems.append(
            f"rename_labels.target ist {c2_bands!r}, erwartet [{band!r}] - "
            f"bei Umbenennung auf einen S2-Bandnamen ueberlappen die Cubes "
            f"wieder")
    if c1_bands and c2_bands and set(c1_bands) & set(c2_bands):
        problems.append(
            f"Band-Labels ueberlappen: cube1={c1_bands!r} cube2={c2_bands!r}")

    # Kein rechnender Prozess auf dem Pfad Klassen-Cube -> save_result.
    arithmetic = {"add", "subtract", "multiply", "divide", "mean", "sum"}
    seen, stack = set(), [fb["arguments"].get("data")]
    while stack:
        name, node = node_of(stack.pop())
        if not node or name in seen:
            continue
        seen.add(name)
        if node.get("process_id") in arithmetic:
            problems.append(
                f"rechnender Prozess {node['process_id']!r} ({name!r}) auf "
                f"dem Ergebnispfad")
        for v in (node.get("arguments") or {}).values():
            if isinstance(v, dict) and "from_node" in v:
                stack.append(v)
    return problems


# Uebliche Nodata-Sentinels. CDSE schreibt das Ergebnis nicht zwingend im
# dtype der Quelle: ein uint8-Klassenraster kommt als int16 mit -32768
# zurueck. Diese Werte sind daher KEINE Fremdwerte, sondern "kein Pixel".
NODATA_SENTINELS = (-32768, -32767, -9999, -999, 0, 255, 65535)


def _ignorable_nodata(values, file_nodata=None) -> set:
    """Menge der Werte, die als Nodata gelten und nicht als Klasse zaehlen.

    Enthaelt den in der Datei deklarierten Nodata-Wert plus die ueblichen
    Sentinels, aber nur soweit sie tatsaechlich im Raster vorkommen - es
    wird nichts pauschal weggeworfen.
    """
    ignorable = set()
    if file_nodata is not None:
        try:
            ignorable.add(int(file_nodata))
        except (TypeError, ValueError):
            pass
    present = {int(v) for v in values}
    ignorable |= {s for s in NODATA_SENTINELS if s in present}
    return ignorable


def verify_categorical_result(result_dir, dataset: str,
                              label: str = "") -> bool:
    """Prueft NACH dem CDSE-Job, ob das Ergebnis wirklich Klassen enthaelt.

    Der erste Landcover-Serverlauf meldete status=success und lieferte
    trotzdem S2-Reflexionswerte (int16, Werte 66..85 und negative statt
    uint8 mit 10/30/40/...). Weder das Backend noch der Benchmark haben das
    bemerkt - erst der Accuracy-Check mit 0,0000 % Uebereinstimmung. Diese
    Pruefung faengt genau das ab, direkt nach dem Job und mit klarer
    Meldung, statt es in eine unerklaerliche Metrik laufen zu lassen.

    UNTERSCHEIDUNGSMERKMAL ist nicht der Wertebereich, sondern (a) ob die
    NICHT-Nodata-Werte alle in der Klassenmenge liegen und (b) wie viele
    verschiedene Werte es ueberhaupt gibt. Ein korrektes Ergebnis darf
    negativ aussehen: CDSE liefert die Klassenkarte als int16 mit -32768
    als Nodata, also z.B. 8 Werte im Bereich -32768..90. Das echte
    Fehlerbild sah dagegen so aus: 106 verschiedene Werte im Bereich
    -20..85, davon fast keiner eine Klasse. Ein Nodata-Sentinel allein
    darf also nie eine Diagnose ausloesen.

    Gibt True zurueck wenn plausibel, sonst False (und meldet laut). Wirft
    NICHT, und der Rueckgabewert wird von den Strategien bewusst NICHT
    ausgewertet: die Diagnose meldet, blockiert aber weder den Lauf noch
    den nachfolgenden Accuracy-Check.
    """
    import numpy as np
    info = DATASETS.get(dataset, {})
    classes = set(info.get("classes") or ())
    if not classes:
        return True
    tifs = sorted(Path(result_dir).glob("*.tif"))
    if not tifs:
        print(f"  [warn] Keine Ergebnis-TIFs in {result_dir} - "
              f"Inhaltspruefung uebersprungen.")
        return True
    ok = True
    for tif in tifs:
        try:
            with rasterio.open(tif) as src:
                arr = src.read(1)
                dt = src.dtypes[0]
                file_nodata = src.nodata
        except Exception as exc:
            print(f"  [warn] {tif.name} nicht lesbar: {exc}")
            continue
        finite = arr[np.isfinite(arr)] if arr.dtype.kind == "f" else arr
        vals = np.unique(finite)
        if vals.size == 0:
            print(f"  [warn] {tif.name}: keine auswertbaren Pixel.")
            continue
        ignorable = _ignorable_nodata(vals, file_nodata)
        rest = [int(v) for v in vals if int(v) not in ignorable]
        fremd = sorted(v for v in rest if v not in classes)
        nod_txt = (f", Nodata {sorted(ignorable)}" if ignorable else "")
        if fremd:
            ok = False
            print(f"\n  [DIAGNOSE] {label}{tif.name}: Ergebnis ist KEINE "
                  f"Klassenkarte.")
            print(f"      dtype={dt}, {len(vals)} verschiedene Werte, "
                  f"Bereich {int(vals.min())}..{int(vals.max())}{nod_txt}")
            print(f"      davon {len(rest)} Nicht-Nodata-Werte, {len(fremd)} "
                  f"ausserhalb der {dataset}-Klassen {sorted(classes)}: "
                  f"{fremd[:12]}{' ...' if len(fremd) > 12 else ''}")
            print(f"      Erwartet: ausschliesslich diese Klassen (plus "
                  f"Nodata). Typische Ursache: der Graph liefert den "
                  f"falschen Cube (s. verify_lc_overlay_graph).")
        else:
            print(f"  Inhaltspruefung {tif.name}: OK ({len(rest)} Klassen "
                  f"{sorted(rest)}, dtype={dt}{nod_txt})")
    return ok


# Ziel-Zellgroesse in Metern. 10 m = Sentinel-2 B04 nativ und damit das
# bisherige, fest verdrahtete Verhalten. Ueber --resolution steuerbar
# (Experimentdimension Zellgroesse -> Laufzeit/Datenvolumen/Genauigkeit).
DEFAULT_RESOLUTION_M = 10.0

# Faktor fuer den Zwischenschritt von workflow=resample: das PG resampelt
# nach EPSG:3035 @ (Faktor x Zielaufloesung) und wieder zurueck. Bei der
# Default-Aufloesung ergibt das exakt die bisherigen 30 m.
RESAMPLE_DETOUR_FACTOR = 3


def _pg_resolution(res: float):
    """Aufloesungswert wie er in einen openEO-Process-Graph geschrieben wird.

    Ganzzahlige Werte werden als int serialisiert - so bleibt der Graph bei
    der Default-Aufloesung BYTE-identisch zu den bisherigen Szenarien
    ("resolution": 10, nicht 10.0).
    """
    return int(res) if float(res).is_integer() else float(res)


def _resolution_of(args) -> float:
    """Ziel-Zellgroesse aus den CLI-Args, mit Default und Plausibilitaets-
    pruefung. Zentral, damit jeder Strategie-Pfad denselben Wert sieht."""
    res = float(getattr(args, "resolution", DEFAULT_RESOLUTION_M)
                or DEFAULT_RESOLUTION_M)
    if res <= 0:
        raise ValueError(f"--resolution muss > 0 sein (ist {res}).")
    return res


def _is_default_resolution(res: float) -> bool:
    """True, wenn die Aufloesung der historischen 10 m entspricht. Nur dann
    bleiben Prozessgraphen unveraendert (kein zusaetzlicher Resample-Knoten).
    """
    return abs(float(res) - DEFAULT_RESOLUTION_M) < 1e-9

# Lokale Resampling-Methoden fuer die Reprojektion des zweiten Rasters.
# 'mode' (Mehrheitsentscheidung) ist das fachlich richtige Verfahren fuer
# KATEGORIALE Daten: es waehlt die haeufigste Klasse im Quellfenster, statt
# wie bilinear/cubic/average Klassen-IDs zu mitteln und dabei nicht
# existierende Klassen zu erfinden. Lokal per GDAL-Warp verifiziert.
LOCAL_RESAMPLING = {
    "nearest":  Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic":    Resampling.cubic,
    "mode":     Resampling.mode,
}

# Abbildung lokaler Methodennamen (rasterio) auf die openEO-Namen, die CDSE
# fuer resample_spatial / resample_cube_spatial akzeptiert (laut
# describe_process: average, bilinear, cubic, cubicspline, lanczos, max, med,
# min, mode, near, q1, q3, rms, sum).
#
# WARUM ueberhaupt: --local-resampling steuerte bisher NUR die lokale
# Reprojektion, waehrend die serverseitigen Resample-Knoten fest auf "near"
# standen. Bei --resolution != 10 vergroebern damit beide Seiten
# UNTERSCHIEDLICH - gemessen berlin/medium/60 m: MAE 402 (nearest) bzw. 322
# (bilinear) gegenueber 0,0014 bei nativer Aufloesung. Der Graph leitet die
# Methode jetzt aus derselben Quelle ab.
#
# 'nearest' -> 'near' ist der einzige Namensunterschied und zugleich der
# Default - dadurch bleiben die Graphen bei Default-Konfiguration
# byte-identisch zum bisherigen Stand.
OPENEO_RESAMPLE_METHOD = {
    "nearest":  "near",
    "bilinear": "bilinear",
    "cubic":    "cubic",
    "mode":     "mode",
}


def _pg_resample_method(resampling: str) -> str:
    """openEO-Methodenname fuer einen lokalen Resampling-Namen.

    Unbekannte Werte fallen auf 'near' zurueck, damit ein Graph niemals mit
    einer vom Backend abgelehnten Methode gebaut wird (die CLI validiert die
    Auswahl ohnehin vorher ueber die choices von --local-resampling).
    """
    return OPENEO_RESAMPLE_METHOD.get(resampling, "near")

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

# ---------------------------------------------------------------------------
# DEM-Format Experiment (Machbarkeit): kann CDSE ein extern per load_stac
# bereitgestelltes DEM auch in Zarr / NetCDF verstehen, nicht nur GeoTIFF?
# Nur local_preprocessing ist betroffen. Der Default 'gtiff' bleibt
# rueckwaertskompatibel - die GeoTIFF-Achse mit --dem-layout ist orthogonal.
#   gtiff  - Standard, siehe --dem-layout
#   zarr   - xarray-Zarr-Verzeichnis-Store (CF-Attribute + spatial_ref)
#   netcdf - xarray-NetCDF-4 Datei (CF-Attribute + spatial_ref)
DEM_FORMATS = ("gtiff", "zarr", "netcdf")

# Media-Types und Datei-Endungen pro Format.
_DEM_FORMAT_MEDIA_TYPE = {
    "gtiff":  "image/tiff; application=geotiff",
    "zarr":   "application/vnd+zarr",
    "netcdf": "application/x-netcdf",
}
_DEM_FORMAT_EXT = {
    "gtiff":  ".tif",
    "zarr":   ".zarr",   # bewusst kein '.', ist ein Verzeichnis-Store
    "netcdf": ".nc",
}


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


def _crs_is_geographic(crs_str: str) -> bool:
    """True wenn das CRS in Grad rechnet (WGS84 & Co). Nur fuer die Warnung,
    dass eine in Metern gemeinte --resolution dort keine Meter sind."""
    from rasterio.crs import CRS as _RIOCRS
    return bool(_RIOCRS.from_user_input(crs_str).is_geographic)

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


RUN_META_FILENAME = "run_meta.json"


def _write_run_meta(run_dir: Path, resolution: float,
                    dataset: str = DEFAULT_DATASET, **extra) -> Path:
    """Schreibt run_meta.json in einen Run-Ordner (Zielaufloesung,
    Datensatz-Paar + optionale Zusatzfelder).

    Bewusst eine EIGENE Datei und kein Feld im Szenario-JSON: der
    Process-Graph muss bei Default-Aufloesung byte-identisch zu den
    bisherigen Szenarien bleiben. Gelesen wird sie von
    _detect_folder_resolution und _detect_folder_dataset, damit der
    Accuracy-Check Referenz und Test nach Aufloesung UND Datensatz-Paar
    paart, statt Gitter unterschiedlicher Zellgroesse oder gar
    Hoehendaten gegen Landbedeckung zu vergleichen.
    """
    meta = {"resolution_m": float(resolution), "dataset": str(dataset)}
    meta.update(extra)
    path = Path(run_dir) / RUN_META_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
    return path


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
                           block: int = _COG_BLOCK_SIZE,
                           categorical: bool = False) -> None:
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

    categorical: bei kategorialen Rastern (Landbedeckungsklassen) werden die
    COG-Overviews per MODUS statt per Mittelwert gebaut. Der Mittelwert ueber
    Klassen-IDs erfindet Klassen, die es nicht gibt (zwischen 10 Baum und
    50 bebaut laege 30 Gras) - und zwar unbemerkt, weil Overviews erst
    backend-seitig gelesen werden. Betrifft nur layout='cog'.
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
            ov_method = Resampling.mode if categorical else Resampling.average
            ov_name = "mode" if categorical else "average"
            with rasterio.open(output_tif, "r+") as dst:
                dst.build_overviews(factors, ov_method)
                dst.update_tags(ns="rio_overview", resampling=ov_name)


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


def _check_dem_format_deps(dem_format: str) -> None:
    """Wirft ImportError mit klarer Installationsanweisung wenn optionale
    Pakete fehlen. Kein Auto-Install - der Nutzer entscheidet.
    """
    if dem_format == "gtiff":
        return
    missing = []
    try:
        import xarray  # noqa: F401
    except ImportError:
        missing.append("xarray")
    if dem_format == "zarr":
        try:
            import zarr  # noqa: F401
        except ImportError:
            missing.append("zarr")
    if dem_format == "netcdf":
        try:
            import netCDF4  # noqa: F401
        except ImportError:
            missing.append("netcdf4")
    if missing:
        pkgs = " ".join(missing)
        raise ImportError(
            f"Fuer --dem-format={dem_format} fehlen: {', '.join(missing)}. "
            f"Installieren mit: pip install {pkgs}"
        )


def _build_xarray_dataset(data, dst_meta):
    """Baut ein xarray.Dataset mit x/y-Koordinaten, DEM-Datenvariable und
    einer 'spatial_ref' Grid-Mapping Variable nach CF-Konventionen.

    - x/y werden aus dst_meta['transform'] als Pixel-Zentren berechnet.
    - CRS wird als WKT2 in spatial_ref.crs_wkt + als PROJ.4-String in
      spatial_ref.spatial_ref abgelegt (CF + GDAL Konvention).
    - Bei mehreren Baendern kommt eine 'band'-Dimension dazu.

    Damit erkennt jeder CF-konforme Reader (xarray + optional rioxarray,
    QGIS, gdal, netCDF-Tools) die Georeferenz.
    """
    import numpy as np
    import xarray as xr
    from rasterio.crs import CRS as RIOCRS

    transform = dst_meta["transform"]
    width = dst_meta["width"]
    height = dst_meta["height"]

    # Pixel-Zentren (nicht -Ecken): x = c + (col + 0.5) * a, y = f + (row + 0.5) * e
    a, _, c = transform.a, transform.b, transform.c
    _, e, f = transform.d, transform.e, transform.f
    xs = c + (np.arange(width) + 0.5) * a
    ys = f + (np.arange(height) + 0.5) * e

    # CRS als WKT2 + PROJ.4 in einer 0-D Grid-Mapping Variable ablegen.
    try:
        crs = RIOCRS.from_user_input(dst_meta["crs"])
        crs_wkt = crs.to_wkt()
        crs_proj4 = crs.to_proj4()
        epsg = crs.to_epsg()
    except Exception:
        crs_wkt = str(dst_meta["crs"])
        crs_proj4 = ""
        epsg = None

    spatial_ref_attrs = {
        "crs_wkt": crs_wkt,
        "spatial_ref": crs_wkt,          # GDAL-Konvention
        "grid_mapping_name": "unknown",  # CF-Platzhalter
        "GeoTransform": (f"{transform.c} {transform.a} {transform.b} "
                         f"{transform.f} {transform.d} {transform.e}"),
    }
    if epsg is not None:
        spatial_ref_attrs["epsg_code"] = int(epsg)
    if crs_proj4:
        spatial_ref_attrs["proj4"] = crs_proj4

    count = data.shape[0]
    coords = {
        "y": ("y", ys),
        "x": ("x", xs),
        "spatial_ref": ((), np.array(0, dtype="int8"), spatial_ref_attrs),
    }
    # WICHTIG: _FillValue gehoert in .encoding, NICHT in .attrs. Steht sie
    # in attrs, wandelt xarray beim Zurueckladen automatisch nach float und
    # maskiert mit NaN - das wuerde die Pixel-Identitaet zerstoeren. Ueber
    # encoding schreiben die zarr/netcdf-Backends den Fill-Wert korrekt in
    # die Datei, der Datentyp bleibt beim Lesen aber int16 (sofern der
    # Reader mask_and_scale=False setzt - CDSE macht das idR selbst).
    var_attrs = {"grid_mapping": "spatial_ref"}
    var_encoding = {}
    nodata = dst_meta.get("nodata")
    if nodata is not None:
        var_encoding["_FillValue"] = nodata

    if count == 1:
        # 2D-Variable ohne band-Achse - typisch fuer DEM.
        da = xr.DataArray(
            data[0], dims=("y", "x"),
            coords={"y": ys, "x": xs},
            attrs=var_attrs, name="DEM",
        )
    else:
        bands = np.arange(1, count + 1, dtype="int32")
        da = xr.DataArray(
            data, dims=("band", "y", "x"),
            coords={"band": bands, "y": ys, "x": xs},
            attrs=var_attrs, name="DEM",
        )
    if var_encoding:
        da.encoding.update(var_encoding)

    ds = da.to_dataset()
    ds = ds.assign_coords({"spatial_ref": ((), np.array(0, dtype="int8"))})
    ds["spatial_ref"].attrs = spatial_ref_attrs
    ds["y"].attrs = {"standard_name": "projection_y_coordinate", "units": "metre"}
    ds["x"].attrs = {"standard_name": "projection_x_coordinate", "units": "metre"}
    ds.attrs = {
        "Conventions": "CF-1.8",
        "title": "Local-preprocessed DEM",
        "source": "reproject_dem_local",
    }
    return ds


def _apply_geozarr_metadata(ds, dst_meta) -> None:
    """Ergaenzt das Dataset in-place um GeoZarr-/GDAL-konforme Georeferenz-
    Attribute, damit ein reiner Zarr-Open (GDAL, CF-Reader) CRS UND Transform
    OHNE begleitendes STAC-Item liefert. Nur fuer den zarr-Writer gedacht -
    netcdf/gtiff bleiben unveraendert. Reine Metadaten: Pixelwerte und
    .encoding (insb. _FillValue) werden nicht angefasst.

    Drei Konventionen redundant nebeneinander, weil unbekannt ist, welchen
    Lesepfad CDSE fuer zarr nutzt:
      1. CF/GeoZarr: vollstaendige grid_mapping-Attribute via
         pyproj.CRS.to_cf() (echter grid_mapping_name + Projektions-
         parameter statt des Platzhalters "unknown") auf spatial_ref,
         plus axis="X"/"Y" auf den Koordinatenvariablen.
      2. GDAL-NetCDF-Konvention: spatial_ref + GeoTransform auf der
         grid_mapping-Variable (kommt bereits aus _build_xarray_dataset).
      3. GDAL-Zarr-Konvention: _CRS-Attribut ({url, wkt, projjson}) direkt
         am DEM-Array - GDALs Zarr-Treiber liest url -> wkt -> projjson;
         Treiber-Versionen ohne CF-Support sehen NUR dieses Attribut.
    """
    from rasterio.crs import CRS as RIOCRS

    try:
        crs = RIOCRS.from_user_input(dst_meta["crs"])
        crs_wkt = crs.to_wkt()
        epsg = crs.to_epsg()
    except Exception:
        crs_wkt = str(dst_meta["crs"])
        epsg = None

    try:
        from pyproj import CRS as PJCRS
        pj = PJCRS.from_user_input(dst_meta["crs"])
    except Exception:
        pj = None

    # (1) CF/GeoZarr grid_mapping. update() statt Ersetzen, damit
    # GeoTransform/spatial_ref aus _build_xarray_dataset erhalten bleiben.
    if pj is not None and "spatial_ref" in ds:
        try:
            ds["spatial_ref"].attrs.update(pj.to_cf())
        except Exception:
            pass
    for name, axis, long_name in (("x", "X", "x coordinate of projection"),
                                  ("y", "Y", "y coordinate of projection")):
        if name in ds:
            ds[name].attrs.setdefault("axis", axis)
            ds[name].attrs.setdefault("long_name", long_name)

    # (3) GDAL-Zarr _CRS am Datenarray. Muss JSON-serialisierbar sein
    # (zarr-Attrs landen 1:1 in .zattrs).
    crs_attr = {"wkt": crs_wkt}
    if epsg is not None:
        crs_attr["url"] = f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"
    if pj is not None:
        try:
            crs_attr["projjson"] = json.loads(pj.to_json())
        except Exception:
            pass
    for var in ds.data_vars.values():
        var.attrs["_CRS"] = crs_attr


def _inject_shape_into_consolidated_zarr_metadata(store_path) -> dict:
    """Ergaenzt in der .zmetadata eines Zarr-v2-Stores jeden Eintrag, der kein
    'shape' hat (die auf ".zgroup"/".zattrs" endenden Schluessel), um genau
    dieses Feld - mit dem Shape des DEM-Arrays. Die Eintraege selbst bleiben
    vollstaendig erhalten, es kommt nur ein Schluessel hinzu.

    WARUM (Versuch 6, hergeleitet aus zwei gemessenen CDSE-Laeufen mit
    identischem Store-Inhalt und nur unterschiedlicher Konsolidierung):
      MIT .zmetadata  (Versuch 4): CDSE sammelt 1 projection metadata entry,
        leitet das target_grid ab und scheitert erst DANACH beim Oeffnen mit
        "Can't parse the zarr array metadata, missing key: 'shape'".
      OHNE .zmetadata (Versuch 5): CDSE sammelt 0 projection metadata entries,
        target_grid=None, Abbruch mit "Unable to derive a spatial extent".
    Daraus folgt: CDSE liest die .zmetadata zwingend - ohne sie sieht es den
    Store gar nicht. Mit ihr sieht es ihn, iteriert dann aber die
    metadata-Map und greift auf jedem Eintrag auf 'shape' zu; ".zgroup" und
    ".zattrs" haben das nicht. Die Meldung traegt die Python-KeyError-
    Signatur ('shape' mit Quotes) und stammt damit aus CDSE-eigenem Code,
    nicht aus GDALs C++-Zarr-Treiber (der meldet "shape missing or not an
    array", zarr_v2_array.cpp).

    Hypothese: hat JEDER Eintrag der Map ein 'shape', findet CDSEs Parser den
    Schluessel ueberall und laeuft nicht mehr in den KeyError. Injizieren
    statt Entfernen, weil die Map dabei ein gueltiges, vollstaendiges
    konsolidiertes Dokument bleibt - ein Reader, der die Georeferenz aus den
    .zattrs zieht, findet sie weiterhin. Der Shape des DEM-Arrays (statt
    eines Dummy-Werts) sorgt dafuer, dass ein daraus abgeleitetes Grid
    konsistent zum Datenarray waere.

    Angefasst wird NUR die konsolidierte Kopie: die einzelnen .zarray-,
    .zattrs- und .zgroup-Dateien im Store bleiben unveraendert. Das Feld
    zarr_consolidated_format bleibt wie geschrieben. Rueckbau = diesen
    Aufruf entfernen, dann steht wieder Versuch 4.

    LOKAL GEMESSEN (GDAL 3.12 ueber /vsicurl gegen einen Range-HTTP-Server):
    Store-Root wie Array-Subpfad ZARR:"...":/DEM oeffnen, liefern
    EPSG:32633 und den korrekten Transform, die Pixel sind bitgenau, und der
    Open laeuft ohne Verzoegerung durch. xarray liest den Store normal -
    konsolidiert wie unkonsolidiert. Die Georeferenz bleibt also lokal voll
    ueberpruefbar; ob CDSE den Store akzeptiert, entscheidet erst der
    Serverlauf.

    Gibt {"injected": [...], "shape": [...]} zurueck (fuer Test/Log).
    """
    zmeta_path = Path(store_path) / ".zmetadata"
    if not zmeta_path.exists():
        return {"injected": [], "shape": None}
    doc = json.loads(zmeta_path.read_text(encoding="utf-8"))
    meta = doc.get("metadata")
    if not isinstance(meta, dict):
        return {"injected": [], "shape": None}
    # Shape des DEM-Arrays; falls es das (wider Erwarten) nicht gibt, den
    # ersten .zarray-Eintrag nehmen. Ohne jeden Shape gibt es nichts zu tun.
    shape = None
    for key in ("DEM/.zarray",):
        if isinstance(meta.get(key), dict) and "shape" in meta[key]:
            shape = meta[key]["shape"]
    if shape is None:
        for key in sorted(meta):
            if key.endswith(".zarray") and "shape" in meta[key]:
                shape = meta[key]["shape"]
                break
    if shape is None:
        return {"injected": [], "shape": None}
    injected = []
    for key, entry in meta.items():
        if isinstance(entry, dict) and "shape" not in entry:
            entry["shape"] = shape
            injected.append(key)
    zmeta_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return {"injected": sorted(injected), "shape": shape}


def _write_dem_as_zarr(data, dst_meta, target_path) -> None:
    """Schreibt data als Zarr-Verzeichnis-Store nach target_path.
    Ueberschreibt einen existierenden Store idempotent.
    """
    _check_dem_format_deps("zarr")
    target = Path(target_path)
    if target.exists():
        # Zarr-Store ist ein Verzeichnis - komplett wegwerfen und neu schreiben.
        import shutil as _sh
        _sh.rmtree(target, ignore_errors=True)
    ds = _build_xarray_dataset(data, dst_meta)
    # Georeferenz zusaetzlich IN den Store schreiben (GeoZarr/CF + GDAL
    # _CRS), damit ein Reader sie auch OHNE das STAC-Item findet. CDSE
    # ignoriert die proj:-Felder des STAC-Items bei application/vnd+zarr
    # ("Collected 0 projection metadata entries"); ob sein Zarr-Lesepfad
    # die Store-Georeferenz auswertet, entscheidet erst ein Serverlauf.
    _apply_geozarr_metadata(ds, dst_meta)
    # Kompression MUSS aus: xarray schreibt per Default blosc-komprimierte
    # Chunks, und GDALs Zarr-Treiber scheitert daran hart ("Decompressor
    # blosc not handled") - lokal belegt, gleicher Fehler ueber /vsicurl/.
    # Ohne blosc oeffnet GDAL den Store als 2D-Raster inkl. CRS aus dem
    # CF-grid_mapping (spatial_ref) und Transform aus den x/y-Koordinaten.
    # Compressor ueber .encoding der Variablen setzen, NICHT ueber
    # to_zarr(encoding=...): das Kwarg ersetzt die Encoding komplett und
    # wuerde die in _build_xarray_dataset gesetzte _FillValue verwerfen.
    for name in ds.variables:
        ds[name].encoding["compressor"] = None
    # Versuch 6: wieder MIT konsolidierten Metadaten schreiben (Versuch 5,
    # consolidated=False, ist damit zurueckgenommen - CDSE sah den Store
    # dann gar nicht mehr: "Collected 0 projection metadata entries" ->
    # "Unable to derive a spatial extent"). Direkt danach in der .zmetadata
    # jedem Eintrag ohne 'shape' eines verpassen; die Herleitung steht in
    # _inject_shape_into_consolidated_zarr_metadata.
    ds.to_zarr(str(target), mode="w", consolidated=True)
    _inject_shape_into_consolidated_zarr_metadata(target)


def _write_dem_as_netcdf(data, dst_meta, target_path) -> None:
    """Schreibt data als NetCDF-4 Datei nach target_path (Endung .nc)."""
    _check_dem_format_deps("netcdf")
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    ds = _build_xarray_dataset(data, dst_meta)
    ds.to_netcdf(str(target), engine="netcdf4", format="NETCDF4")


def _inspect_asset_size(path) -> dict:
    """Groesse eines Assets (Datei oder Zarr-Verzeichnis) rekursiv."""
    p = Path(path)
    if p.is_dir():
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        num_files = sum(1 for f in p.rglob("*") if f.is_file())
        return {"path": str(p), "size_bytes": total,
                "num_files": num_files, "is_directory": True}
    else:
        return {"path": str(p), "size_bytes": p.stat().st_size,
                "num_files": 1, "is_directory": False}


def _reproject_dem_to_array(input_tif: str, dst_crs: str,
                            resampling: str = "nearest",
                            target_resolution: float = DEFAULT_RESOLUTION_M):
    """Reprojiziert ein Quell-GeoTIFF in einen In-Memory Numpy-Puffer.

    Gibt (data, dst_meta) zurueck. data ist shape (count, height, width)
    im ziel-CRS und ziel-Grid. dst_meta ist ein rasterio-meta-Dict mit
    driver='GTiff', dtype, count, crs, transform, width, height, nodata.

    Dies ist der GEMEINSAME Reprojektions-Pfad fuer alle DEM-Formate
    (gtiff/zarr/netcdf) und alle Layouts. Wer danach schreibt, sieht
    dieselben Pixelwerte - garantiert pixel-Identitaet ueber alle
    Formate/Varianten.
    """
    if resampling not in LOCAL_RESAMPLING:
        raise ValueError(f"Unbekannte Resampling-Methode: {resampling}")
    import numpy as np
    method = LOCAL_RESAMPLING[resampling]

    # UTM-Detection: nur dort target_resolution erzwingen + auf das
    # Vielfachen-Raster snappen (S2-Gitter-Semantik gibt es nur in UTM).
    is_utm = False
    try:
        is_utm = _is_utm_epsg(_parse_epsg(dst_crs))
    except (ValueError, AttributeError):
        pass

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
        elif _is_default_resolution(target_resolution):
            # Nicht-UTM (LAEA, WGS84, Web Mercator, ...): native Reprojektions-
            # Aufloesung, kein S2-Snap. CDSE bekommt damit ein "echtes"
            # cross-CRS Resampling-Problem zu loesen.
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds,
            )
        else:
            # Nicht-UTM MIT explizit gesetzter --resolution: die Zellgroesse
            # wird vorgegeben, aber NICHT gesnappt - der Snap auf Vielfache
            # ist S2-Gitter-Semantik und in LAEA/WGS84 bedeutungslos. Ohne
            # diesen Zweig wuerde --resolution bei Nicht-UTM-Zielen still
            # wirkungslos bleiben. In WGS84 ist die Einheit Grad, nicht
            # Meter - dort ist ein Meterwert als Zellgroesse sinnlos, daher
            # die Warnung.
            try:
                if _crs_is_geographic(dst_crs):
                    print(f"  [warn] --resolution {target_resolution:g} wird "
                          f"in {dst_crs} als GRAD interpretiert (geographisches "
                          f"CRS), nicht als Meter.")
            except Exception:
                pass
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds,
                resolution=target_resolution,
            )

        dst_meta = src.meta.copy()
        dst_meta.update({"crs": dst_crs, "transform": transform,
                         "width": width, "height": height})

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
    return data, dst_meta


def _grid_from_dst_meta(dst_meta: dict) -> dict:
    """Grid-Dict (read_s2_grid-Stil) aus dem In-Memory-Ziel-Grid der
    Reprojektion.

    Fuer STAC-proj-Metadaten OHNE Re-Open des geschriebenen Outputs:
    Zarr-Stores und NetCDF lassen sich nicht wie ein GeoTIFF mit rasterio
    oeffnen, das Ziel-Grid ist aber fuer alle Formate identisch, weil alle
    Writer denselben In-Memory-Puffer aus _reproject_dem_to_array schreiben.
    """
    transform = dst_meta["transform"]
    width, height = dst_meta["width"], dst_meta["height"]
    left, bottom, right, top = array_bounds(height, width, transform)
    return {"transform": transform, "width": width, "height": height,
            "bounds": (left, bottom, right, top), "shape": (height, width)}


def _s2_grid_from_extent(extent: dict, epsg: int,
                         resolution: float = DEFAULT_RESOLUTION_M) -> dict:
    """Erwartetes CDSE-Zielgitter aus dem angefragten Extent ableiten
    (--snap-dem-to-s2), OHNE S2-Datei und OHNE CDSE-Aufruf.

    Herleitung: Extent (EPSG:4326) ins UTM-Ziel-CRS projizieren, dann alle
    vier Kanten OUTWARD auf Vielfache von `resolution` snappen (floor fuer
    left/bottom, ceil fuer right/top). Gegen einen realen CDSE-Job
    validiert (berlin/medium, EPSG:32633): Ergebnis-Grid des Jobs war
    exakt left=384470 bottom=5812220 right=394910 top=5823580,
    1044x1136 - identisch mit dieser Rekonstruktion auf allen 4 Kanten.

    Rueckgabe im read_s2_grid-Stil: transform/width/height/bounds/shape.
    """
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", f"EPSG:{epsg}",
        extent["west"], extent["south"], extent["east"], extent["north"],
    )
    res = resolution
    snapped_left   = math.floor(left   / res) * res
    snapped_bottom = math.floor(bottom / res) * res
    snapped_right  = math.ceil(right   / res) * res
    snapped_top    = math.ceil(top     / res) * res
    width = int(round((snapped_right - snapped_left) / res))
    height = int(round((snapped_top - snapped_bottom) / res))
    transform = Affine(res, 0, snapped_left, 0, -res, snapped_top)
    return {"transform": transform, "width": width, "height": height,
            "bounds": (snapped_left, snapped_bottom,
                       snapped_right, snapped_top),
            "shape": (height, width)}


def _crop_to_grid(data, dst_meta: dict, target_grid: dict):
    """Croppt einen reprojizierten Puffer per reinem Array-Slicing auf
    target_grid. Gibt (cropped_data, cropped_meta) zurueck.

    BEWUSST Crop statt zweitem Warp direkt aufs Ziel-Grid: GDALs Warp ist
    nicht frame-invariant - der approximierende Koordinaten-Transformer
    (Default-Toleranz 0.125 px) und die Resample-Kernel-Arithmetik haengen
    vom Grid-Ausschnitt ab. Lokal gemessen (synthetisches DEM, Grids auf
    demselben 10-m-Raster): direkter Warp aufs Snap-Grid weicht vom
    Ausschnitt des Quell-Bounds-Warps ab (bilinear 24.5% der Pixel um +-1,
    nearest 3.6% bis +-10). Der Crop dagegen aendert Werte per Konstruktion
    NICHT - genau die gewuenschte Semantik "Snapping betrifft nur die
    Gittergeometrie". Voraussetzungen (werden geprueft, sonst ValueError):
    gleiche Pixelgroesse, Gitter-Ausrichtung (Origin-Differenz = ganze
    Pixel), target_grid vollstaendig im Puffer enthalten.
    """
    import numpy as np
    t_src = dst_meta["transform"]
    t_dst = target_grid["transform"]
    res = t_src.a
    if (t_dst.a, t_dst.e) != (t_src.a, t_src.e):
        raise ValueError(
            f"_crop_to_grid: Pixelgroesse passt nicht "
            f"({t_src.a},{t_src.e}) vs ({t_dst.a},{t_dst.e})")
    col_off = (t_dst.c - t_src.c) / res
    row_off = (t_src.f - t_dst.f) / res
    if abs(col_off - round(col_off)) > 1e-9 or \
       abs(row_off - round(row_off)) > 1e-9:
        raise ValueError(
            f"_crop_to_grid: Gitter nicht ausgerichtet "
            f"(col_off={col_off}, row_off={row_off}) - Crop wuerde das "
            f"Pixelraster verschieben.")
    col_off, row_off = int(round(col_off)), int(round(row_off))
    height, width = int(target_grid["height"]), int(target_grid["width"])
    if (col_off < 0 or row_off < 0
            or col_off + width > dst_meta["width"]
            or row_off + height > dst_meta["height"]):
        raise ValueError(
            f"_crop_to_grid: Ziel-Grid ragt aus dem reprojizierten DEM "
            f"heraus (col_off={col_off}, row_off={row_off}, "
            f"ziel={height}x{width}, "
            f"puffer={dst_meta['height']}x{dst_meta['width']}). Das DEM "
            f"deckt den angefragten Extent nicht vollstaendig ab - "
            f"DEM-Download/Cache pruefen (passt die extent_size?).")
    cropped = np.ascontiguousarray(
        data[:, row_off:row_off + height, col_off:col_off + width])
    cropped_meta = dict(dst_meta)
    cropped_meta.update({"transform": t_dst, "width": width,
                         "height": height})
    return cropped, cropped_meta


def _tile_grid_layout(n: int) -> tuple:
    """Zerlegt n in (rows, cols) moeglichst quadratisch mit rows*cols == n
    exakt (4 -> 2x2, 6 -> 2x3, Primzahl p -> 1xp). Keine leeren Kacheln,
    keine Reste - jede Kachel existiert und traegt Daten.
    """
    rows = max(1, int(math.isqrt(n)))
    while n % rows != 0:
        rows -= 1
    return rows, n // rows


def _split_dem_into_tiles(data, dst_meta: dict, n: int) -> list:
    """Zerlegt den reprojizierten Puffer in n raeumliche Kacheln
    (row-major, rows x cols aus _tile_grid_layout). Gibt eine Liste von
    (tile_data, tile_meta) zurueck - tile_meta ist ein vollstaendiges
    rasterio-meta-Dict mit der Geotransform des Ausschnitts.

    Reines Array-Slicing ueber _crop_to_grid, KEIN zweiter Warp - dieselbe
    Begruendung wie bei --snap-dem-to-s2: GDALs Warp ist nicht
    frame-invariant, nur der Crop garantiert, dass die Vereinigung der
    Kacheln bitgenau dem Einzel-DEM entspricht. Die Kachelgrenzen sind
    ganzzahlige Pixel-Offsets (i*H//rows bzw. i*W//cols) - luecken- und
    ueberlappungsfrei per Konstruktion, Randkacheln tragen den Rest.
    """
    rows, cols = _tile_grid_layout(n)
    height, width = int(dst_meta["height"]), int(dst_meta["width"])
    if height < rows or width < cols:
        raise ValueError(
            f"_split_dem_into_tiles: DEM ({height}x{width} px) ist kleiner "
            f"als das Kachelraster ({rows}x{cols}) - --dem-tiles verkleinern.")
    t = dst_meta["transform"]
    row_edges = [r * height // rows for r in range(rows + 1)]
    col_edges = [c * width // cols for c in range(cols + 1)]
    tiles = []
    for r in range(rows):
        for c in range(cols):
            row_off, col_off = row_edges[r], col_edges[c]
            tile_grid = {
                "transform": t * Affine.translation(col_off, row_off),
                "width": col_edges[c + 1] - col_off,
                "height": row_edges[r + 1] - row_off,
            }
            tiles.append(_crop_to_grid(data, dst_meta, tile_grid))
    return tiles


def _wgs84_extent_from_meta(meta: dict) -> dict:
    """WGS84-Extent (west/south/east/north) eines Puffers/Kachel-meta -
    fuer geometry/bbox des per-Kachel-STAC-Items (das Region-Extent waere
    fuer eine Einzelkachel falsch)."""
    left, bottom, right, top = array_bounds(
        meta["height"], meta["width"], meta["transform"])
    w, s, e, n = transform_bounds(meta["crs"], "EPSG:4326",
                                  left, bottom, right, top)
    return {"west": w, "south": s, "east": e, "north": n}


def _verify_tile_union_identity(tiles: list, data, dst_meta: dict) -> bool:
    """Pflichttest fuer --dem-tiles: die Vereinigung der Kacheln muss dem
    Einzel-DEM bitgenau entsprechen.

    Prueft (1) lueckenlose, ueberlappungsfreie Abdeckung (Coverage-Zaehler
    pro Pixel == 1), (2) Byte-Identitaet des zusammengesetzten Arrays
    (SHA-unabhaengig via tobytes - NaN-sicher, vgl. crop_identity-
    Fehlalarm: NaN != NaN laesst np.array_equal bei float-Nodata
    fehlschlagen), (3) np.array_equal (equal_nan bei float) und (4) dass
    die Vereinigung der Kachel-Extents exakt den Gesamt-Extent ergibt.
    Die Fenster-Offsets werden unabhaengig aus den Geotransforms
    hergeleitet, nicht aus der Konstruktionsreihenfolge.
    """
    import numpy as np
    t0 = dst_meta["transform"]
    height, width = int(dst_meta["height"]), int(dst_meta["width"])
    assembled = np.zeros_like(data)
    cover = np.zeros((height, width), dtype=np.uint8)
    lefts, bottoms, rights, tops = [], [], [], []
    for tile_data, tile_meta in tiles:
        tt = tile_meta["transform"]
        col_off = int(round((tt.c - t0.c) / t0.a))
        row_off = int(round((tt.f - t0.f) / t0.e))
        th, tw = int(tile_meta["height"]), int(tile_meta["width"])
        assembled[:, row_off:row_off + th, col_off:col_off + tw] = tile_data
        cover[row_off:row_off + th, col_off:col_off + tw] += 1
        l, b, r, tp = array_bounds(th, tw, tt)
        lefts.append(l); bottoms.append(b); rights.append(r); tops.append(tp)
    coverage_ok = bool((cover == 1).all())
    bit_ok = assembled.tobytes() == data.tobytes()
    if np.issubdtype(data.dtype, np.floating):
        eq_ok = np.array_equal(assembled, data, equal_nan=True)
    else:
        eq_ok = np.array_equal(assembled, data)
    full_l, full_b, full_r, full_t = array_bounds(height, width, t0)
    extent_ok = (abs(min(lefts) - full_l) < 1e-6
                 and abs(min(bottoms) - full_b) < 1e-6
                 and abs(max(rights) - full_r) < 1e-6
                 and abs(max(tops) - full_t) < 1e-6)
    shape_sum = sum(tm["height"] * tm["width"] for _, tm in tiles)
    shape_ok = shape_sum == height * width
    print(f"  [Tile-Verifikation] {len(tiles)} Kacheln vs. Einzel-DEM "
          f"({height}x{width} px):")
    print(f"    Abdeckung 1x pro Pixel   {'OK' if coverage_ok else 'MISMATCH'}")
    print(f"    Byte-Identitaet (Union)  {'OK' if bit_ok else 'MISMATCH'}")
    print(f"    np.array_equal           {'OK' if eq_ok else 'MISMATCH'}")
    print(f"    Pixel-Summe der Shapes   {'OK' if shape_ok else 'MISMATCH'} "
          f"({shape_sum} vs {height * width})")
    print(f"    Extent-Vereinigung       {'OK' if extent_ok else 'MISMATCH'}")
    return coverage_ok and bit_ok and eq_ok and shape_ok and extent_ok


def _verify_snap_grid(dst_meta: dict, expected_grid: dict) -> bool:
    """Log-Block: tatsaechliches Grid des reprojizierten Puffers gegen das
    erwartete CDSE-Zielgitter (projizierter Extent, outward auf 10 m).
    Prueft Ursprung, Pixelgroesse, Shape und Extent Feld fuer Feld.
    """
    actual = _grid_from_dst_meta(dst_meta)
    ta, te = actual["transform"], expected_grid["transform"]
    checks = [
        ("Ursprung (left, top)", (ta.c, ta.f), (te.c, te.f)),
        ("Pixelgroesse (a, e)", (ta.a, ta.e), (te.a, te.e)),
        ("Shape (H, W)", actual["shape"], expected_grid["shape"]),
        ("Extent (l,b,r,t)", tuple(actual["bounds"]),
         tuple(expected_grid["bounds"])),
    ]
    all_ok = True
    print("  [Snap-Verifikation] gesnapptes DEM-Grid vs. erwartetes "
          "CDSE-Zielgitter:")
    for label, got, want in checks:
        ok = got == want
        all_ok &= ok
        print(f"    {label:22s} ist={got}  soll={want}  "
              f"[{'OK' if ok else 'MISMATCH'}]")
    return all_ok


def _verify_snap_crop_identity(data_snapped, meta_snapped,
                               data_unsnapped, meta_unsnapped) -> bool:
    """Pflichttest fuer --snap-dem-to-s2: gesnapptes und ungesnapptes DEM
    liegen auf demselben 10-m-Gitter (beide Urspruenge Vielfache der
    Aufloesung), also MUESSEN die Pixelwerte im ueberlappenden Bereich
    bitgenau identisch sein - das Snapping darf nur zuschneiden, nie
    Werte veraendern. Vergleich NaN-bewusst auf der Schnittmenge der
    beiden Extents (beidseitig NaN = Nodata = identisch, NaN-vs-Wert =
    Mismatch).

    Die Fenster werden hier UNABHAENGIG von _crop_to_grid aus den
    Geo-Koordinaten beider Metas hergeleitet - der Test validiert damit
    die Crop-Indexierung end-to-end (Off-by-one in Offset/Window wuerde
    als MISMATCH auffallen), nicht nur eine Tautologie.
    """
    import numpy as np
    ts, tu = meta_snapped["transform"], meta_unsnapped["transform"]
    res = ts.a
    if (tu.a, tu.e) != (ts.a, ts.e):
        print(f"  [Crop-Identitaet] FEHLER: unterschiedliche Pixelgroesse "
              f"({ts.a},{ts.e}) vs ({tu.a},{tu.e}) - kein Vergleich moeglich.")
        return False
    # Gitter-Ausrichtung: Origin-Differenzen muessen ganze Pixel sein.
    dx, dy = ts.c - tu.c, ts.f - tu.f
    if abs(dx / res - round(dx / res)) > 1e-9 or \
       abs(dy / res - round(dy / res)) > 1e-9:
        print(f"  [Crop-Identitaet] FEHLER: Gitter nicht ausgerichtet "
              f"(dx={dx}, dy={dy}, res={res}) - Snapping haette das "
              f"Pixelraster verschoben.")
        return False

    bs = array_bounds(meta_snapped["height"], meta_snapped["width"], ts)
    bu = array_bounds(meta_unsnapped["height"], meta_unsnapped["width"], tu)
    inter_left = max(bs[0], bu[0])
    inter_bottom = max(bs[1], bu[1])
    inter_right = min(bs[2], bu[2])
    inter_top = min(bs[3], bu[3])
    if inter_left >= inter_right or inter_bottom >= inter_top:
        print("  [Crop-Identitaet] FEHLER: Extents ueberlappen nicht.")
        return False

    def _window(t):
        col0 = int(round((inter_left - t.c) / res))
        row0 = int(round((t.f - inter_top) / res))
        ncols = int(round((inter_right - inter_left) / res))
        nrows = int(round((inter_top - inter_bottom) / res))
        return slice(row0, row0 + nrows), slice(col0, col0 + ncols)

    rs, cs = _window(ts)
    ru, cu = _window(tu)
    a = data_snapped[:, rs, cs]
    b = data_unsnapped[:, ru, cu]
    if a.shape != b.shape:
        print(f"  [Crop-Identitaet] FEHLER: Fenster-Shapes ungleich "
              f"({a.shape} vs {b.shape}) - Fenster ragt aus einem der "
              f"Puffer heraus.")
        return False

    # NaN-bewusster Identitaetsvergleich: die DEM-Puffer tragen NaN als
    # Nodata (Warp-Slivers an den Raendern liegen auch IN der
    # Schnittmenge). np.array_equal allein meldet dort MISMATCH, obwohl
    # die Arrays bitgenau identisch sind (NaN != NaN, IEEE 754) - genau
    # dieser Fehlalarm brach reale Snap-Laeufe ab. Beide-NaN zaehlt als
    # identisch; NaN-vs-Wert bleibt ein echter Mismatch.
    diff = a != b
    n_both_nan = 0
    if a.dtype.kind == "f":
        both_nan = np.isnan(a) & np.isnan(b)
        n_both_nan = int(both_nan.sum())
        diff &= ~both_nan
    n_diff = int(diff.sum())
    identical = n_diff == 0
    n_px = (rs.stop - rs.start) * (cs.stop - cs.start)
    detail = ""
    if n_diff:
        d = np.abs(a[diff].astype("float64") - b[diff].astype("float64"))
        finite = d[np.isfinite(d)]
        detail = (f", max|diff|={finite.max()}" if finite.size
                  else ", nur NaN-vs-Wert-Paare")
    print(f"  [Crop-Identitaet] Schnittmenge (l,b,r,t)=({inter_left}, "
          f"{inter_bottom}, {inter_right}, {inter_top}), "
          f"{rs.stop - rs.start}x{cs.stop - cs.start} px "
          f"({n_px} Pixel/Band): {n_diff} Pixel abweichend"
          f"{detail}, {n_both_nan} beidseitig NaN (Nodata) "
          f"[{'OK' if identical else 'MISMATCH'}]")
    return identical


def reproject_dem_local(input_tif: str, output_tif: str,
                        dst_crs: str = "EPSG:32633",
                        resampling: str = "nearest",
                        target_resolution: float = DEFAULT_RESOLUTION_M,
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
    if layout not in DEM_LAYOUTS:
        raise ValueError(
            f"Unbekanntes dem_layout: {layout!r}. Erlaubt: {DEM_LAYOUTS}"
        )
    t0 = time.time()
    data, dst_meta = _reproject_dem_to_array(
        input_tif, dst_crs, resampling=resampling,
        target_resolution=target_resolution,
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


def _utm_zone_for_lon(lon: float) -> int:
    """UTM-Zonennummer (1-60) fuer einen Laengengrad."""
    return int((float(lon) + 180.0) // 6.0) % 60 + 1


def _extent_spans_multiple_utm_zones(extent: dict) -> bool:
    """True wenn der Laengenbereich [west, east] mehr als eine 6-Grad-UTM-Zone
    beruehrt. Die MGRS-Sonderzonen (Norwegen/Svalbard, lat 56-84) liegen
    ausserhalb aller Benchmark-Regionen und werden ignoriert.
    """
    return (_utm_zone_for_lon(extent["west"])
            != _utm_zone_for_lon(extent["east"]))


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


def _build_workflow_pg(template: dict, workflow: str, region: str = None,
                       resolution: float = DEFAULT_RESOLUTION_M,
                       dataset: str = DEFAULT_DATASET,
                       resampling: str = "nearest") -> dict:
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
    # Zweitraster-Kollektion setzen (No-Op bei dataset='dem').
    _apply_dataset_to_pg(pg, dataset)

    # rename_labels: cube2 Bandname auf "B04" -> ueberlappt mit cube1.
    # source ist der Bandname des Zweitrasters ("DEM" fuer COPERNICUS_30,
    # "MAP" fuer ESA_WORLDCOVER); bei load_stac (local_pp / full_pp)
    # ueberschreiben die Builder source=[], weil der vom Backend vergebene
    # Bandname nicht garantiert derselbe ist.
    pg["renamelabels1"] = {
        "arguments": {
            "data": {"from_node": "loadcollection2"},
            "dimension": "bands",
            "target": ["B04"],
            "source": [DATASETS[dataset]["band"]],
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

    if workflow == "lc_overlay":
        # merge_cubes BLEIBT - genau dieselbe Gitter-Aushandlung zwischen
        # S2-Cube und Zweitcube wie bei merge_add (cube1 = S2 ist laut Spec
        # das Resampling-Ziel, die Klassenkarte landet also auf dem
        # S2-Gitter). Die Auswahl des Ergebnis-Cubes laeuft aber NICHT mehr
        # ueber den overlap_resolver.
        #
        # WARUM NICHT (erster Serverlauf, belegt): der frueherer Ansatz war
        # ein Durchreich-Resolver add(x={from_parameter:"y"}, y=0). Laut
        # openEO-Spec ist das korrekt - overlap_resolver.x ist "the
        # overlapping value from the base data cube cube1", y "from the
        # other data cube cube2" -, also haette y+0 die Klassenkarte
        # geliefert. Der Job lief mit status=success durch und lieferte
        # trotzdem S2-Reflexionswerte (int16 statt uint8). Die Log-Zeile
        # "Starting stage: 29 - merge_cubes, B04,B04,add" zeigt, warum:
        # CDSE erkennt den Resolver als bekannten Binaeroperator 'add' und
        # wendet seine native cube1<op>cube2-Implementierung an - die
        # Argumentverdrahtung (from_parameter + Konstante 0) faellt dabei
        # weg. Der Ansatz haengt damit an einer Backend-Interna, nicht an
        # der Spec.
        #
        # STATTDESSEN: die Baender werden gar nicht erst zur Ueberlappung
        # gebracht. Der Zweitcube behaelt seinen eigenen Bandnamen (MAP),
        # S2 heisst B04 -> DISJUNKTE Labels. Laut Spec braucht merge_cubes
        # dann keinen overlap_resolver ("If there is any overlap between
        # the dimension labels, the parameter overlap_resolver must be
        # specified"), sondern konkateniert entlang der Band-Dimension.
        # Danach holt filter_bands genau das Klassenband heraus.
        #
        # Damit haengt das Ergebnis an einem BANDNAMEN statt an der
        # x/y-Bindung eines Resolvers: es kann strukturell nicht mehr
        # versehentlich S2 sein.
        band = DATASETS[dataset]["band"]
        pg["renamelabels1"]["arguments"]["target"] = [band]
        # Kein Overlap -> Resolver entfernen (er waere unbenutzt, und
        # manche Backends beanstanden einen Resolver ohne Ueberlappung).
        pg["merge1"]["arguments"].pop("overlap_resolver", None)
        pg["filterbands_lc"] = {
            "arguments": {"data": {"from_node": "merge1"}, "bands": [band]},
            "process_id": "filter_bands",
        }
        pg["saveresult1"]["arguments"]["data"] = {"from_node": "filterbands_lc"}
        return pg

    if workflow == "lc_mask":
        # B04 auf eine Landbedeckungsklasse maskieren. merge_cubes entfaellt;
        # der Gitterabgleich zwischen beiden Cubes passiert stattdessen im
        # mask-Prozess, der ebenfalls ein gemeinsames Gitter erzwingt.
        #
        # mask() maskiert dort, wo der Mask-Cube WAHR ist -> die Bedingung
        # ist "Klasse != Zielklasse".
        pg["lcmaskbuild1"] = {
            "arguments": {
                "data": {"from_node": "reducedimension_dem"},
                "process": {
                    "process_graph": {
                        "eq1": {
                            "arguments": {"x": {"from_parameter": "x"},
                                          "y": LC_MASK_CLASS},
                            "process_id": "eq",
                        },
                        "not1": {
                            "arguments": {"x": {"from_node": "eq1"}},
                            "process_id": "not",
                            "result": True,
                        },
                    }
                },
            },
            "process_id": "apply",
        }
        pg["lcmask1"] = {
            "arguments": {
                "data": {"from_node": "loadcollection1"},
                "mask": {"from_node": "lcmaskbuild1"},
            },
            "process_id": "mask",
        }
        pg.pop("merge1", None)
        pg["saveresult1"]["arguments"]["data"] = {"from_node": "lcmask1"}
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
        # @ (3x Zielaufloesung) und zurueck nach UTM @ Zielaufloesung
        # resamplen. Reine CDSE-Operation - testet das interne Resampling.
        # Der Umweg skaliert MIT der Zielaufloesung (RESAMPLE_DETOUR_FACTOR),
        # damit er auch bei grober Zellgroesse ein echter Groebungsschritt
        # bleibt und nicht zum Hochsampeln wird; bei 10 m ergibt das exakt
        # die bisherigen 30 m.
        pg["resamplespatial1"] = {
            "arguments": {
                "data": {"from_node": "reducedimension_dem"},
                "projection": 3035,
                "resolution": _pg_resolution(
                    resolution * RESAMPLE_DETOUR_FACTOR),
                "method": _pg_resample_method(resampling),
            },
            "process_id": "resample_spatial",
        }
        pg["resamplespatial2"] = {
            "arguments": {
                "data": {"from_node": "resamplespatial1"},
                "projection": target_epsg,
                "resolution": _pg_resolution(resolution),
                "method": _pg_resample_method(resampling),
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


def _force_onthefly_target_crs(pg: dict, target_epsg: int,
                               resolution: float = DEFAULT_RESOLUTION_M,
                               resampling: str = "nearest") -> None:
    """In-place: haengt resample_spatial(target_epsg, resolution, method)
    hinter loadcollection1 (S2) und biegt alle Verbraucher darauf um.

    Zweck: Ueberspannt der Extent eine UTM-Zonengrenze, liegen die S2-Daten
    in zwei Zonen (z.B. 32632+32633) und CDSE bricht ohne Ziel-CRS-Vorgabe
    beim Bounding-Box-Merge ab ("no target CRS specified, but multiple
    CRSes across input"). Das explizite Ziel-CRS zwingt alle S2-Eingaben in
    die primaere Zone der Region; das DEM (EPSG:4326) folgt danach wie
    bisher implizit dem cube1-Grid im merge_cubes.

    Zweiter Verwendungszweck (--resolution != 10): derselbe Knoten gibt CDSE
    die Zellgroesse explizit vor, sonst liefert das Backend sein natives
    10-m-S2-Gitter und der Aufloesungs-Vergleich waere wirkungslos. Deshalb
    'resolution' als Parameter statt fest verdrahtet.

    Knotenname bewusst NICHT resamplespatial1/2 - diese Namen sind die
    Workflow-Signatur von workflow=resample (_detect_pg_workflow).
    """
    def _retarget(obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                if (isinstance(val, dict)
                        and val.get("from_node") == "loadcollection1"):
                    obj[key] = {"from_node": "resampletargetcrs1"}
                else:
                    _retarget(val)
        elif isinstance(obj, list):
            for item in obj:
                _retarget(item)

    for node in pg.values():
        if isinstance(node, dict):
            _retarget(node.get("arguments"))
    pg["resampletargetcrs1"] = {
        "arguments": {
            "data": {"from_node": "loadcollection1"},
            "projection": target_epsg,
            "resolution": _pg_resolution(resolution),
            "method": _pg_resample_method(resampling),
        },
        "process_id": "resample_spatial",
    }


def build_onthefly_scenario(region: str, target_path: Path,
                            extent_size: str = "medium",
                            workflow: str = "merge_add",
                            force_target_crs: bool = False,
                            resolution: float = DEFAULT_RESOLUTION_M,
                            dataset: str = DEFAULT_DATASET,
                            resampling: str = "nearest") -> Path:
    """Onthefly = Workflow-PG aus bench_onthefly_{region}.json gebaut.

    Ueberspannt der Extent mehrere UTM-Zonen (oder ist force_target_crs
    gesetzt), bekommt der Graph ein explizites Ziel-CRS (primaere UTM-Zone
    der Region, s. _force_onthefly_target_crs). Ein-Zonen-Extents bleiben
    byte-identisch zum bisherigen Graphen.

    Bei --resolution != 10 wird derselbe Knoten gesetzt, dann aber wegen der
    Zellgroesse: ohne ihn liefert CDSE sein natives 10-m-S2-Gitter und die
    Aufloesung waere im Ergebnis nicht wirksam.
    """
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow, region=region,
                            resolution=resolution, dataset=dataset,
                            resampling=resampling)
    extent = template["process_graph"]["loadcollection1"]["arguments"][
        "spatial_extent"]
    spans_zones = _extent_spans_multiple_utm_zones(extent)
    custom_res = not _is_default_resolution(resolution)
    if force_target_crs or spans_zones or custom_res:
        target_epsg = REGIONS[region]["epsg"]
        if spans_zones:
            reason = ("Extent ueberspannt UTM-Zonen "
                      f"{_utm_zone_for_lon(extent['west'])}+"
                      f"{_utm_zone_for_lon(extent['east'])}")
        elif force_target_crs:
            reason = "--force-target-crs"
        else:
            reason = f"--resolution {resolution:g} m"
        print(f"  onthefly: explizites Ziel-CRS EPSG:{target_epsg} "
              f"@ {resolution:g} m ({reason})")
        _force_onthefly_target_crs(pg, target_epsg, resolution=resolution,
                                   resampling=resampling)
    with open(target_path, "w") as f:
        json.dump({"process_graph": pg}, f, indent=2)
    return target_path


def build_dem_download_scenario(region: str, target_path: Path,
                                extent_size: str = "medium",
                                dataset: str = DEFAULT_DATASET) -> Path:
    """Baut ein Szenario das nur das ZWEITE Raster fuer die Region
    herunterlaedt (COPERNICUS_30 bzw. die Kollektion aus --dataset)."""
    template = _load_bench_template(region, extent_size)
    _apply_dataset_to_pg(template["process_graph"], dataset)
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
                            workflow: str = "merge_add",
                            resample_s2_to_dem: bool = False,
                            resolution: float = DEFAULT_RESOLUTION_M,
                            dataset: str = DEFAULT_DATASET,
                            resampling: str = "nearest") -> Path:
    """
    Erzeugt das load_stac Szenario fuer den gewuenschten Workflow:
    Workflow-PG (s. _build_workflow_pg) wird gebaut, dann wird
    loadcollection2 (DEM) durch loadstac1 ersetzt, das auf die
    Hetzner-STAC-Item-URL zeigt.

    resample_s2_to_dem (--resample-s2-to-dem): umgekehrte Gitter-Hoheit.
    Statt dass CDSE das per load_stac geladene DEM beim merge_cubes auf
    sein S2-abgeleitetes Zielgitter zwingt (zweites, serverseitiges
    Resampling), wird S2 VOR dem merge per
    resample_cube_spatial(data=S2, target=DEM) auf das DEM-Gitter
    ausgerichtet. Per openEO-Spec uebernimmt data dabei Aufloesung/CRS/
    Alignment des target-Cubes; NUR S2 wird resampled, das DEM (der
    zeitlose reducedimension_dem-Cube) laeuft durch KEIN Resample.
    method: wird aus --local-resampling abgeleitet (nearest -> near), damit
    die serverseitige Vergroeberung dieselbe ist wie die lokale. Vorher
    stand hier fest 'near', waehrend lokal z.B. bilinear lief - bei
    --resolution != 10 vergroebern beide Seiten dann unterschiedlich, was
    sich als grosser MAE niederschlaegt (berlin/medium/60 m: 402 bzw. 322
    gegenueber 0,0014 bei nativer Aufloesung).
    Ob CDSE das DEM-Gitter dann wirklich uebernimmt, zeigt erst der
    Serverlauf (Ursprung des Ergebnis-Grids).

    resolution != 10: das lokal reprojizierte DEM traegt die Zellgroesse
    bereits, S2 kommt aber weiterhin nativ mit 10 m - und beim merge_cubes
    gibt cube1 (S2) das Gitter vor, wuerde das DEM also wieder auf 10 m
    ziehen. Deshalb bekommt S2 denselben resample_spatial-Knoten wie in
    build_onthefly_scenario (resampletargetcrs1). Mit --resample-s2-to-dem
    ist das unnoetig: dort wird S2 ohnehin per resample_cube_spatial auf das
    DEM-Gitter gezogen, das die Aufloesung schon traegt.
    """
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow, region=region,
                            resolution=resolution, dataset=dataset,
                            resampling=resampling)

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

    if resample_s2_to_dem:
        # S2 auf das DEM-Gitter ausrichten: alle bisherigen Verbraucher
        # von loadcollection1 (merge1.cube1, bei workflow=mask die beiden
        # filter_bands-Knoten) auf den neuen Resample-Knoten umbiegen.
        # Knotenname bewusst NICHT resamplespatial1/2 - das ist die
        # Workflow-Signatur von workflow=resample (_detect_pg_workflow).
        def _retarget_s2(node_args):
            for k, v in list(node_args.items()):
                if isinstance(v, dict):
                    if v.get("from_node") == "loadcollection1":
                        node_args[k] = {"from_node": "resamplecubespatial1"}
                    else:
                        _retarget_s2(v)
        for node in pg.values():
            if isinstance(node, dict) and isinstance(node.get("arguments"), dict):
                _retarget_s2(node["arguments"])
        pg["resamplecubespatial1"] = {
            "arguments": {
                "data": {"from_node": "loadcollection1"},
                "target": {"from_node": "reducedimension_dem"},
                "method": _pg_resample_method(resampling),
            },
            "process_id": "resample_cube_spatial",
        }
    elif not _is_default_resolution(resolution):
        # S2 explizit auf die Zielaufloesung bringen, sonst zwingt das
        # native 10-m-S2-Gitter beim merge_cubes das DEM zurueck auf 10 m.
        target_epsg = REGIONS[region]["epsg"]
        print(f"  local_pp: S2 explizit auf EPSG:{target_epsg} "
              f"@ {resolution:g} m resamplen (--resolution)")
        _force_onthefly_target_crs(pg, target_epsg, resolution=resolution,
                                   resampling=resampling)

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
                       dst_crs: str, resampling: str = "nearest",
                       target_resolution: float = None) -> float:
    """Reprojiziert ein S2-TIF lokal nach dst_crs (Szenario 3: BEIDE Raster
    in Nicht-UTM-CRS). Ohne target_resolution die Default-Aufloesung aus
    calculate_default_transform (bisheriges Verhalten); mit gesetztem Wert
    die vorgegebene Zellgroesse, damit S2 und DEM bei --resolution auf
    derselben Aufloesung landen.
    """
    method = LOCAL_RESAMPLING.get(resampling, Resampling.nearest)
    t0 = time.time()
    with rasterio.open(input_tif) as src:
        extra = ({"resolution": target_resolution}
                 if target_resolution is not None else {})
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds, **extra,
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
                           workflow: str = "merge_add",
                           save_format: str = "GTiff",
                           resolution: float = DEFAULT_RESOLUTION_M,
                           dataset: str = DEFAULT_DATASET,
                           resampling: str = "nearest") -> Path:
    """
    Process Graph fuer full_preprocessing: ZWEI load_stac Aufrufe
    (loadstac1=S2, loadstac2=DEM) + Workflow-Verknuepfung.

    Wir starten von der Workflow-PG und ersetzen
    - loadcollection1 (S2)  -> loadstac1
    - loadcollection2 (DEM) -> loadstac2
    sowie biegen merge1.cube1/cube2 und (workflow=mask) filterbands_b04/_scl
    auf den S2 STAC um.

    save_format: Ausgabeformat des Backend save_result. Default 'GTiff' -
    wie bisher. Alternative 'netCDF' fuer die Diagnose ob die beobachtete
    Output-Korruption GTiff-spezifisch beim CDSE-Writer ist (Schritt 4 der
    Ursachensuche).

    resolution: wirkt hier NICHT ueber einen Resample-Knoten - bei full_pp
    kommen BEIDE Cubes per load_stac von Hetzner und tragen die Zellgroesse
    schon aus der lokalen Reprojektion (s. run_strategy_full_preprocessing).
    Der Parameter geht nur an _build_workflow_pg weiter, damit
    workflow=resample seinen Umweg passend skaliert.
    """
    template = _load_bench_template(region, extent_size)
    pg = _build_workflow_pg(template, workflow, region=region,
                            resolution=resolution, dataset=dataset,
                            resampling=resampling)

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

    # save_result Format ueberschreiben, wenn abweichend vom Template-Default
    # (GTiff). Nur die Format-Angabe wird geaendert - options bleiben leer,
    # damit CDSE ein moeglichst standardkonformes Output-Profil schreibt.
    if save_format != "GTiff" and "saveresult1" in pg:
        pg["saveresult1"]["arguments"]["format"] = save_format
        pg["saveresult1"]["arguments"]["options"] = {}

    scenario = {"process_graph": pg}
    with open(target_path, "w") as f:
        json.dump(scenario, f, indent=2)
    return target_path


def build_stac_item(region: str, asset_href: str, epsg: int,
                    item_id: str, extent: dict = None,
                    dem_format: str = "gtiff",
                    grid: dict = None,
                    dataset: str = DEFAULT_DATASET) -> dict:
    """STAC Item passend zum reprojizierten DEM-Asset.

    `extent` ueberschreibt REGIONS[region]['extent'] (z.B. fuer small/large
    Modi). Default = REGIONS-Extent (medium).

    dem_format:
      gtiff  - Standard, media_type=image/tiff; application=geotiff
      zarr   - Verzeichnis-Store, media_type=application/vnd+zarr, href
               endet auf '/' damit klar ist dass es kein Einzelfile ist.
      netcdf - Einzeldatei .nc, media_type=application/x-netcdf. Der href
               bekommt ein /vsicurl/-Praefix: CDSE baut daraus den GDAL-Pfad
               NETCDF:<href>:DEM ohne Quoting, und mit nacktem http-URL
               deutet GDAL "http" als lokalen Pfad ("File does not exist:
               http", Exception Code 4). Lokal verifiziert:
                 NETCDF:http://HOST/f.nc:DEM           -> FAIL
                 NETCDF:/vsicurl/http://HOST/f.nc:DEM  -> OK
               Die Datei selbst + Upload bleiben unveraendert, nur der
               href-String im Item traegt das Praefix.

    grid (read_s2_grid-Stil: transform/width/height/bounds): liefert
    proj:shape / proj:bbox / proj:transform fuer Item-Properties UND
    data-Asset. Fuer zarr/netcdf ist das de facto Pflicht: proj:epsg
    allein reicht CDSE nicht, um einen raeumlichen Extent abzuleiten
    ("Unable to derive a spatial extent from provided STAC metadata" /
    "Collected 0 projection metadata entries"). GeoTIFF funktionierte nur,
    weil das Backend das File selbst oeffnen kann - zarr/netcdf kann es
    nicht. Die Werte muessen deshalb aus dem In-Memory-Ziel-Grid der
    Reprojektion kommen (_grid_from_dst_meta), nicht aus dem Output-File.
    """
    ext = extent if extent is not None else REGIONS[region]["extent"]
    w, s, e, n = ext["west"], ext["south"], ext["east"], ext["north"]
    media_type = _DEM_FORMAT_MEDIA_TYPE.get(dem_format,
                                            _DEM_FORMAT_MEDIA_TYPE["gtiff"])
    href = asset_href
    if dem_format == "zarr" and not href.endswith("/"):
        href = href + "/"
    if dem_format == "netcdf" and href.startswith("http"):
        href = "/vsicurl/" + href

    # Ohne Band-Metadaten laedt CDSE den Cube ohne Band-Label
    # ("bands_from_stac_item: no band name source found"), renamelabels1
    # hat dann nichts zum Umbenennen und das DEM faellt still aus
    # merge_cubes raus. eo:bands (STAC 1.0 ueblich) + bands (STAC 1.1)
    # parallel, damit jeder Reader-Pfad eine Bandnamen-Quelle findet.
    band_meta = [{"name": DATASETS[dataset]["band"]}]
    asset = {
        "href": href,
        "type": media_type,
        "roles": ["data"],
        "proj:epsg": epsg,
        "eo:bands": band_meta,
        "bands": band_meta,
    }
    properties = {"datetime": DATASETS[dataset]["stac_datetime"],
                  "proj:epsg": epsg}
    if grid is not None:
        t = grid["transform"]
        left, bottom, right, top = grid["bounds"]
        proj_fields = {
            "proj:shape": [int(grid["height"]), int(grid["width"])],
            "proj:bbox": [left, bottom, right, top],
            "proj:transform": [t.a, t.b, t.c, t.d, t.e, t.f, 0.0, 0.0, 1.0],
        }
        asset.update(proj_fields)
        properties.update(proj_fields)

    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v1.1.0/schema.json",
            "https://stac-extensions.github.io/eo/v1.1.0/schema.json",
        ],
        "id": item_id,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        },
        "bbox": [w, s, e, n],
        "properties": properties,
        "assets": {"data": asset},
        "links": [],
    }


def _link_item_into_collection(item: dict, item_url: str,
                               collection_id: str, collection_url: str) -> None:
    """Verlinkt ein Standalone-Item in-place in seine Collection.

    Setzt item['collection'] und ersetzt die (bisher leeren) links durch
    self/root/parent/collection mit ABSOLUTEN URLs - relative hrefs sind
    auf dem statischen Hetzner-Hosting nicht zuverlaessig aufloesbar.
    Inhalt (properties, assets, proj-Felder, eo:bands) bleibt unberuehrt.
    """
    item["collection"] = collection_id
    item["links"] = [
        {"rel": "self", "href": item_url, "type": "application/geo+json"},
        {"rel": "root", "href": collection_url, "type": "application/json"},
        {"rel": "parent", "href": collection_url, "type": "application/json"},
        {"rel": "collection", "href": collection_url,
         "type": "application/json"},
    ]


def build_dem_stac_collection(collection_id: str, collection_url: str,
                              item: dict, item_url: str) -> dict:
    """Minimale valide STAC Collection (1.0.0) um genau EIN DEM-Item.

    Zweck (dem_format=zarr): CDSEs load_stac hat fuer Collection vs.
    einzelnes Item verschiedene Code-Pfade. Ueber ein Item ignoriert das
    Backend beim Medientyp application/vnd+zarr saemtliche proj-Metadaten
    ("Collected 0 projection metadata entries"); ob der Collection-Pfad
    den zarr-Asset anders behandelt, ist unbekannt und wird hiermit
    getestet. Deshalb tragen zusaetzlich zur Item-Verlinkung (rel=item)
    auch item_assets und summaries die proj-/Band-Metadaten - manche
    Backends lesen Asset-Metadaten von der Collection statt vom Item.

    Alle Angaben werden aus dem fertigen Item abgeleitet, damit Item und
    Collection nie divergieren koennen.
    """
    props = item.get("properties", {})
    asset = item.get("assets", {}).get("data", {})
    dt = props.get("datetime")

    item_assets_data = {
        "type": asset.get("type"),
        "roles": asset.get("roles", ["data"]),
    }
    for key in ("proj:epsg", "proj:shape", "proj:bbox", "proj:transform",
                "eo:bands", "bands"):
        if key in asset:
            item_assets_data[key] = asset[key]

    summaries = {}
    for key in ("proj:epsg", "proj:shape", "proj:bbox", "proj:transform"):
        if key in props:
            summaries[key] = [props[key]]
    if "eo:bands" in asset:
        summaries["eo:bands"] = asset["eo:bands"]

    return {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v1.1.0/schema.json",
            "https://stac-extensions.github.io/eo/v1.1.0/schema.json",
            "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json",
        ],
        "id": collection_id,
        "description": ("Locally reprojected DEM (zarr store) hosted on "
                        "Hetzner for openEO load_stac; single-item "
                        "collection wrapper."),
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [list(item["bbox"])]},
            "temporal": {"interval": [[dt, dt]]},
        },
        "item_assets": {"data": item_assets_data},
        "summaries": summaries,
        "links": [
            {"rel": "self", "href": collection_url,
             "type": "application/json"},
            {"rel": "root", "href": collection_url,
             "type": "application/json"},
            {"rel": "item", "href": item_url,
             "type": "application/geo+json"},
        ],
    }


def build_dem_tiles_collection(collection_id: str, collection_url: str,
                               items_with_urls: list) -> dict:
    """STAC Collection (1.0.0) ueber N raeumliche DEM-Kachel-Items
    (--dem-tiles).

    items_with_urls: Liste von (item, item_url) - ein Item pro Kachel,
    jedes mit genau einem data-Asset und EIGENEN proj-Feldern fuer seinen
    Ausschnitt.

    Struktur-Begruendung (Schritt-0-Recherche): CDSEs geopyspark-Treiber
    dedupliziert mehrere Assets gleichen Bandnamens INNERHALB eines Items
    (NoveltyTracker in load_stac.py - nur das erste Asset wird geladen,
    der Rest stumm verworfen); raeumlich mosaikiert wird ausschliesslich
    UEBER Items (per-SpatialKey merge in FileLayerProvider.scala - der
    Sentinel-2-Normalfall: Item = Kachel, Assets = Baender). Deshalb
    Collection mit N Items statt ein Item mit N Assets.

    item_assets/summaries tragen nur die kachel-INVARIANTEN Metadaten
    (media type, Rollen, Band, EPSG). Die per-Kachel-Geometrie
    (proj:shape/bbox/transform) steht NUR in den Items - ein
    Collection-weiter Wert waere fuer jede Kachel falsch.
    """
    first_item = items_with_urls[0][0]
    first_asset = first_item["assets"]["data"]
    dt = first_item["properties"].get("datetime")

    bboxes = [it["bbox"] for it, _ in items_with_urls]
    union_bbox = [min(b[0] for b in bboxes), min(b[1] for b in bboxes),
                  max(b[2] for b in bboxes), max(b[3] for b in bboxes)]

    item_assets_data = {
        "type": first_asset.get("type"),
        "roles": first_asset.get("roles", ["data"]),
    }
    for key in ("proj:epsg", "eo:bands", "bands"):
        if key in first_asset:
            item_assets_data[key] = first_asset[key]

    summaries = {}
    if "proj:epsg" in first_asset:
        summaries["proj:epsg"] = [first_asset["proj:epsg"]]
    if "eo:bands" in first_asset:
        summaries["eo:bands"] = first_asset["eo:bands"]

    links = [
        {"rel": "self", "href": collection_url, "type": "application/json"},
        {"rel": "root", "href": collection_url, "type": "application/json"},
    ]
    for _item, item_url in items_with_urls:
        links.append({"rel": "item", "href": item_url,
                      "type": "application/geo+json"})

    return {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v1.1.0/schema.json",
            "https://stac-extensions.github.io/eo/v1.1.0/schema.json",
            "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json",
        ],
        "id": collection_id,
        "description": ("Locally reprojected DEM split into "
                        f"{len(items_with_urls)} spatial tiles (one item "
                        "per tile), hosted on Hetzner for openEO "
                        "load_stac; --dem-tiles experiment."),
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [union_bbox]},
            "temporal": {"interval": [[dt, dt]]},
        },
        "item_assets": {"data": item_assets_data},
        "summaries": summaries,
        "links": links,
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


def scp_upload_dir(local_dir: str, remote_dirname: str) -> float:
    """scp -r fuer ein Verzeichnis (Zarr-Store) auf Hetzner.

    Zarr-Stores sind Verzeichnisbaeume, kein Einzelfile. Der Rekursiv-Upload
    ist die minimalste Loesung; alternative Ansaetze (rsync, tar+scp+untar)
    waeren robuster gegen Teil-Uebertragungen, aber scp -r reicht fuer den
    Machbarkeitstest.
    """
    remote = f"{HETZNER_HOST}:{HETZNER_WEB_PATH}{remote_dirname}"
    cmd = ["scp", "-r",
           "-o", "StrictHostKeyChecking=no",
           "-o", f"ConnectTimeout={min(SCP_SSH_TIMEOUT, 30)}",
           local_dir, remote]
    print(f"  [scp -r] {' '.join(cmd)}")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=SCP_SSH_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - t0
        raise RuntimeError(
            f"scp -r Timeout nach {elapsed:.1f}s (Limit {SCP_SSH_TIMEOUT}s) "
            f"fuer {local_dir} -> {remote}"
        ) from exc
    elapsed = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(
            f"scp -r fehlgeschlagen ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return elapsed


_REWRITE_PROFILES = ("simple_striped", "tiled_deflate")


def _rewrite_tif_clean(input_tif: str, output_tif: str,
                       profile: str = "simple_striped",
                       blocksize: int = 256,
                       compress: str = "deflate") -> float:
    """Schreibt ein GeoTIFF neu mit einem einfachen, breit dekodierbaren Profil.

    NEUE ERKENNTNIS (Runde 2, gesichert): Der Streaming-Download liefert die
    CDSE-Ergebnisse Byte-fuer-Byte korrekt aus, aber die Ergebnisse bei
    full_preprocessing sind trotzdem defekt (TIFFReadEncodedTile failed,
    MAE ~12000). Die Ursache liegt nicht im Transfer, sondern in der Art,
    wie CDSE die hochgeladenen S2-Eingaben ueber load_stac interpretiert
    oder das Ergebnis schreibt. local_preprocessing liefert korrekte
    Ergebnisse und laed sein DEM als striped, unkomprimiertes GeoTIFF hoch.

    Neuer Default profile='simple_striped': gestreiftes, unkomprimiertes
    GeoTIFF - identisch zu dem, was local_preprocessing fuer das DEM
    verwendet und was CDSE nachweislich sauber liest.

    Alter Default (bis zum Bugfix): profile='tiled_deflate' - tiled 256x256
    mit deflate. War die Reaktion auf einen davor vermuteten Transfer-Bug,
    ist aber der wahrscheinlichere Ausloeser der beobachteten CDSE-Output-
    Korruption bei full_pp und deshalb NICHT mehr der Default.

    CRS, Transform, Dtype, Nodata und Band-Beschreibungen werden 1:1
    uebernommen, sodass STAC-Geometrie und Pixel-Werte unveraendert bleiben.
    Idempotent.
    """
    if profile not in _REWRITE_PROFILES:
        raise ValueError(
            f"Unbekanntes _rewrite_tif_clean profile: {profile!r}. "
            f"Erlaubt: {_REWRITE_PROFILES}"
        )
    t0 = time.time()
    with rasterio.open(input_tif) as src:
        prof = src.profile.copy()
        prof["driver"] = "GTiff"
        prof["BIGTIFF"] = "IF_SAFER"
        prof["interleave"] = "band"
        if profile == "simple_striped":
            # Explizit tiled=False + kein compress. Vom Input geerbte Werte
            # (falls das Source-TIF selbst tiled war) muessen unbedingt
            # entfernt werden, sonst greift rasterio auf die Source-Bloecke
            # zurueck.
            prof["tiled"] = False
            prof.pop("blockxsize", None)
            prof.pop("blockysize", None)
            prof.pop("compress", None)
        else:  # tiled_deflate (Fallback fuer den alten Pfad)
            prof.update({
                "tiled":      True,
                "blockxsize": blocksize,
                "blockysize": blocksize,
                "compress":   compress,
            })
        with rasterio.open(output_tif, "w", **prof) as dst:
            for i in range(1, src.count + 1):
                dst.write(src.read(i), i)
            if src.descriptions and any(src.descriptions):
                dst.descriptions = src.descriptions
            if src.nodata is not None:
                dst.nodata = src.nodata
    return time.time() - t0


def _verify_tif_readable(path: str, label: str = "") -> dict:
    """Oeffnet die Datei mit rasterio, liest ALLE Baender vollstaendig.
    Wirft RuntimeError bei kaputten Kacheln / abgeschnittener Datei.

    Gibt Statistiken zurueck: shape, count, dtype, size_bytes, block_size,
    compression, tiled - damit im Log dokumentiert ist, was tatsaechlich
    hochgeladen wird.
    """
    p = Path(path)
    size = p.stat().st_size if p.exists() else 0
    prefix = f"[verify-read{f' {label}' if label else ''}]"
    try:
        with rasterio.open(str(p)) as src:
            prof = src.profile
            # Alle Baender komplett dekodieren -> zwingt jeden Block-Read
            arr = src.read()
            info = {
                "path": str(p),
                "size_bytes": size,
                "shape": list(arr.shape),
                "count": src.count,
                "dtype": str(arr.dtype),
                "tiled": bool(prof.get("tiled", False)),
                "blockxsize": prof.get("blockxsize"),
                "blockysize": prof.get("blockysize"),
                "compress": prof.get("compress"),
                "interleave": prof.get("interleave"),
            }
    except Exception as exc:
        raise RuntimeError(
            f"{prefix} rasterio konnte {p.name} nicht vollstaendig lesen "
            f"({size:,} Bytes): {type(exc).__name__}: {exc}"
        ) from exc
    print(f"  {prefix} {p.name} OK  ({size:,} Bytes, "
          f"{info['count']}b {info['dtype']}, "
          f"tiled={info['tiled']}"
          + (f" {info['blockxsize']}x{info['blockysize']}" if info["tiled"] else "")
          + f", compress={info['compress'] or 'none'})")
    return info


def _inspect_tif_header_bytes(path: str, nbytes: int = 32) -> dict:
    """Direktes Byte-Level Inspection eines TIFF-Kopfs OHNE rasterio.

    Wird nach einem gescheiterten rasterio-Read aufgerufen um zu klaeren:
      - Ist die Datei ueberhaupt ein TIFF (magic bytes)?
      - Byte-Order (II little-endian oder MM big-endian, BigTIFF)?
      - Wo sitzt das erste IFD, und liegt der Offset im tatsaechlich
        vorhandenen Byte-Bereich (== Datei ist strukturell vollstaendig)?

    Damit wird belegbar, ob die Datei serverseitig defekt ANKAM (strukturell
    truncated: IFD zeigt hinter das Datei-Ende) oder ob sie strukturell in
    Ordnung, aber die Kachel-Daten selbst korrupt sind.
    """
    p = Path(path)
    total = p.stat().st_size if p.exists() else 0
    with open(p, "rb") as f:
        head = f.read(nbytes)

    hex_head = " ".join(f"{b:02x}" for b in head[:16])
    result = {
        "path": str(p),
        "size_bytes": total,
        "first16_hex": hex_head,
        "byte_order": None,
        "is_tiff": False,
        "is_bigtiff": False,
        "first_ifd_offset": None,
        "first_ifd_within_file": None,
        "verdict": "unknown",
    }
    if len(head) < 8:
        result["verdict"] = "too_short"
        return result

    byte_order = head[:2]
    if byte_order == b"II":
        result["byte_order"] = "little_endian"
        endian = "little"
    elif byte_order == b"MM":
        result["byte_order"] = "big_endian"
        endian = "big"
    else:
        result["verdict"] = "not_a_tiff (byte-order bytes falsch)"
        return result

    magic = int.from_bytes(head[2:4], endian)
    if magic == 42:
        result["is_tiff"] = True
        ifd_off = int.from_bytes(head[4:8], endian)
        result["first_ifd_offset"] = ifd_off
        result["first_ifd_within_file"] = (ifd_off < total)
    elif magic == 43:
        # BigTIFF: 2-byte offset size + 2-byte reserved + 8-byte IFD offset
        result["is_tiff"] = True
        result["is_bigtiff"] = True
        if len(head) >= 16:
            ifd_off = int.from_bytes(head[8:16], endian)
            result["first_ifd_offset"] = ifd_off
            result["first_ifd_within_file"] = (ifd_off < total)
    else:
        result["verdict"] = f"not_a_tiff (magic={magic})"
        return result

    if result["is_tiff"] and result["first_ifd_within_file"] is False:
        result["verdict"] = "structurally_truncated_ifd_offset_beyond_eof"
    elif result["is_tiff"]:
        result["verdict"] = "tiff_header_ok_body_may_be_corrupt"
    return result


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
        resolution = _resolution_of(args)
        dataset = _dataset_of(args)
        scenario_path = build_onthefly_scenario(
            args.region, outdir / "scenario_onthefly.json",
            extent_size=args.extent_size,
            workflow=args.workflow,
            force_target_crs=getattr(args, "force_target_crs", False),
            resolution=resolution,
            dataset=dataset,
            resampling=args.local_resampling,
        )
        _write_run_meta(outdir, resolution, dataset=dataset)
        results = run_openeo(args.api_url, str(scenario_path), str(outdir),
                             job_timeout=args.job_timeout)
        # Nur Diagnose: der Rueckgabewert wird bewusst NICHT ausgewertet.
        # Die Meldung darf weder den Lauf abbrechen noch den spaeteren
        # Accuracy-Check verhindern - sonst fehlt bei einem Fehlalarm auch
        # noch die Metrik.
        if _is_categorical(dataset) and results.get("status") == "success":
            verify_categorical_result(outdir, dataset, label="onthefly ")
        total_time = results.get("total_time")
        run_id = import_run(str(outdir), crs_strategy="onthefly",
                            run_type=run_type, extent_size=args.extent_size,
                            workflow=args.workflow,
                            resolution_m=resolution, dataset=dataset)
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
                         use_cache: bool,
                         dataset: str = DEFAULT_DATASET) -> tuple:
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
    # Cache-Key MUSS das Datensatz-Paar enthalten: sonst liefert ein
    # vorhandener DEM-Cache-Eintrag stumm ein Hoehenraster fuer einen
    # Landcover-Lauf. Der Default behaelt den historischen Dateinamen,
    # damit bestehende Caches weiter greifen.
    ds_suffix = "" if dataset == DEFAULT_DATASET else f"_{dataset}"
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"dem_{region}_{extent_size}{ds_suffix}.tif"
        if cached.exists():
            print(f"  Cache-Hit: {cached}  (Download uebersprungen)")
            return str(cached), None

        dl_dir = cache_dir / f"_dl_{region}_{extent_size}{ds_suffix}_{_ts()}"
        dl_dir.mkdir()
        dem_scenario = build_dem_download_scenario(
            region, dl_dir / "scenario_dem_download.json",
            extent_size=extent_size, dataset=dataset,
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
        extent_size=extent_size, dataset=dataset,
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

    dem_format = getattr(args, "dem_format", "gtiff")
    dem_layout = getattr(args, "dem_layout", "striped")
    if dem_format not in DEM_FORMATS:
        raise ValueError(f"Unbekanntes --dem-format: {dem_format!r}. "
                         f"Erlaubt: {DEM_FORMATS}")
    # dem_layout ist GeoTIFF-spezifisch. Bei anderen Formaten hat es keinen
    # Effekt; Warnung fuer den Fall dass jemand versehentlich beides setzt.
    if dem_format != "gtiff" and dem_layout != "striped":
        print(f"  [warn] --dem-layout={dem_layout} wird bei "
              f"--dem-format={dem_format} ignoriert (nur fuer gtiff relevant).")

    # --dem-tiles: DEM in N raeumliche Kacheln zerlegen, je Kachel ein
    # eigenes STAC-Item (ein Asset) in einer Collection. Nur gtiff - die
    # zarr/netcdf-Experimente bleiben unangetastet.
    dem_tiles = int(getattr(args, "dem_tiles", 1) or 1)
    if dem_tiles < 1:
        raise ValueError(f"--dem-tiles muss >= 1 sein (ist {dem_tiles}).")
    if dem_tiles > 1 and dem_format != "gtiff":
        raise ValueError(
            f"--dem-tiles {dem_tiles} ist nur mit --dem-format=gtiff "
            f"kombinierbar (ist {dem_format!r}).")

    # Optionale Pakete FRUEH pruefen - klare Fehlermeldung bevor der DEM-
    # Download laeuft.
    _check_dem_format_deps(dem_format)

    # --snap-dem-to-s2: erwartetes CDSE-Zielgitter aus dem angefragten
    # Extent ableiten; nach der (unveraenderten) Reprojektion wird der
    # Puffer auf dieses Grid gecroppt. Nur bei UTM-Ziel sinnvoll - die
    # S2-10-m-Grid-Semantik existiert nur dort (gleiche Regel wie der
    # bestehende 10-m-Snap in _reproject_dem_to_array).
    resolution = _resolution_of(args)
    dataset = _dataset_of(args)
    categorical = _is_categorical(dataset)
    snap_to_s2 = getattr(args, "snap_dem_to_s2", False)
    snap_grid = None
    if snap_to_s2:
        if _is_utm_epsg(target_epsg):
            snap_grid = _s2_grid_from_extent(
                _compute_extent(region, args.extent_size), target_epsg,
                resolution=resolution,
            )
        else:
            print(f"  [warn] --snap-dem-to-s2 wird bei Nicht-UTM-Ziel-CRS "
                  f"{dst_crs} ignoriert (S2-10-m-Grid nur in UTM definiert).")
            snap_to_s2 = False

    asset_ext = _DEM_FORMAT_EXT[dem_format]
    local_asset_path = base / f"step2_reprojected{asset_ext}"
    remote_asset_name = f"dem_reprojected_{region}_{run_ts}{asset_ext}"
    remote_stac_name = f"stac_item_{region}_{run_ts}.json"
    asset_url = f"{HETZNER_URL_BASE}{remote_asset_name}"
    stac_url = f"{HETZNER_URL_BASE}{remote_stac_name}"
    # zarr: Item wird zusaetzlich in eine minimale STAC COLLECTION
    # eingebettet und load_stac zeigt auf die Collection statt aufs Item
    # (CDSE-Codepfad Collection vs. Item ist verschieden; Versuch 4 gegen
    # "Collected 0 projection metadata entries", s. build_dem_stac_collection).
    # --dem-tiles>1: Collection ueber N Kachel-Items (nur gtiff, daher
    # kein Namenskonflikt mit dem zarr-Fall).
    remote_coll_name = f"stac_collection_{region}_{run_ts}.json"
    collection_url = f"{HETZNER_URL_BASE}{remote_coll_name}"
    # Pro-Kachel-Namen (nur bei --dem-tiles>1 benutzt); row-major Index.
    tile_asset_names = [
        f"dem_reprojected_{region}_{run_ts}_tile{i}{asset_ext}"
        for i in range(dem_tiles)]
    tile_item_names = [
        f"stac_item_{region}_{run_ts}_tile{i}.json"
        for i in range(dem_tiles)]
    cache_dir = Path(args.output_dir) / "dem_cache"
    strategy_label = "local_pp_cached" if args.dem_cache else "local_preprocessing"

    print(f"\n{'='*60}")
    print(f"  Strategie: {strategy_label}  |  Region: {region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  Ziel-CRS: {dst_crs}  |  Aufloesung: "
          f"{resolution:g} m  |  DEM-Format: {dem_format}"
          + (f"  |  Layout: {dem_layout}" if dem_format == "gtiff" else "")
          + (f"  |  Tiles: {dem_tiles} "
             f"({'x'.join(map(str, _tile_grid_layout(dem_tiles)))})"
             if dem_tiles > 1 else "")
          + ("  |  Snap: s2" if snap_to_s2 else "")
          + ("  |  Gitter-Hoheit: DEM (resample_cube_spatial S2->DEM)"
             if getattr(args, "resample_s2_to_dem", False) else ""))

    try:
        # Schritt 1: DEM aus Cache laden oder herunterladen (Download NICHT in preprocessing_time)
        cache_mode = "Cache aktiv" if args.dem_cache else "Cache deaktiviert (frischer Download)"
        print(f"\n  [Schritt 1/5] DEM bereitstellen ({region}, {cache_mode})...")
        dem_tif, t_download = _get_or_download_dem(
            args, region, base, cache_dir, use_cache=args.dem_cache,
            dataset=dataset,
        )

        # Schritt 2: Lokal reprojizieren + im Ziel-Format schreiben.
        # In-Memory-Reprojektion garantiert dass Pixelwerte identisch sind,
        # egal ob GeoTIFF/Zarr/NetCDF - nur der Write unterscheidet sich.
        if snap_to_s2:
            grid_info = f"{resolution:g} m, Extent-Snap auf S2/CDSE-Zielgitter"
        elif _is_utm_epsg(target_epsg):
            grid_info = f"{resolution:g} m, S2-snap"
        elif _is_default_resolution(resolution):
            grid_info = "native res"
        else:
            grid_info = f"{resolution:g} m, kein Snap (Nicht-UTM)"
        print(f"\n  [Schritt 2/5] Lokal reprojizieren nach {dst_crs} "
              f"({args.local_resampling}, {grid_info}, dem_format={dem_format})...")
        t_reproj_start = time.time()
        # Reprojektion IMMER wie bisher (Grid aus den Quell-Bounds) - bei
        # --snap-dem-to-s2 wird danach nur aufs Snap-Grid GECROPPT (reines
        # Slicing, beide Grids liegen auf demselben 10-m-Raster). Kein
        # zweiter Warp: GDALs Warp ist nicht frame-invariant, ein direkter
        # Warp aufs Snap-Grid haette andere Pixelwerte als der Ausschnitt
        # (Details in _crop_to_grid). So ist garantiert, dass MIT und OHNE
        # Flag exakt dieselben Werte hochgeladen werden - nur der Extent
        # unterscheidet sich.
        data, dst_meta = _reproject_dem_to_array(
            dem_tif, dst_crs, resampling=args.local_resampling,
            target_resolution=resolution,
        )
        if snap_to_s2:
            data_full, meta_full = data, dst_meta
            data, dst_meta = _crop_to_grid(data, dst_meta, snap_grid)
            print(f"  Snap-Crop: {meta_full['height']}x{meta_full['width']} "
                  f"-> {dst_meta['height']}x{dst_meta['width']} px "
                  f"(origin {meta_full['transform'].c},"
                  f"{meta_full['transform'].f} -> "
                  f"{dst_meta['transform'].c},{dst_meta['transform'].f})")
        tiles = None
        tile_local_paths = []
        if dem_format == "gtiff" and dem_tiles > 1:
            # Zerlegung NACH Reprojektion (und ggf. Snap-Crop): reines
            # Slicing desselben Puffers, kein zweiter Warp. Jede Kachel
            # wird mit demselben Layout-Profil geschrieben wie sonst das
            # Einzel-DEM.
            tiles = _split_dem_into_tiles(data, dst_meta, dem_tiles)
            for i, (tile_data, tile_meta) in enumerate(tiles):
                tile_path = base / f"step2_reprojected_tile{i}{asset_ext}"
                _write_dem_with_layout(tile_data, tile_meta, str(tile_path),
                                       layout=dem_layout,
                                       categorical=categorical)
                _log_tif_layout(_inspect_tif_layout(str(tile_path)))
                tile_local_paths.append(tile_path)
        elif dem_format == "gtiff":
            _write_dem_with_layout(data, dst_meta, str(local_asset_path),
                                   layout=dem_layout,
                                   categorical=categorical)
            _log_tif_layout(_inspect_tif_layout(str(local_asset_path)))
        elif dem_format == "zarr":
            _write_dem_as_zarr(data, dst_meta, str(local_asset_path))
            info = _inspect_asset_size(str(local_asset_path))
            print(f"  Zarr-Store: {info['num_files']} Dateien, "
                  f"{info['size_bytes'] / (1024**2):.2f} MB")
        elif dem_format == "netcdf":
            _write_dem_as_netcdf(data, dst_meta, str(local_asset_path))
            info = _inspect_asset_size(str(local_asset_path))
            print(f"  NetCDF: {info['size_bytes'] / (1024**2):.2f} MB")
        t_reproject = time.time() - t_reproj_start
        write_target = (f"{len(tile_local_paths)} Kacheln in {base}"
                        if tiles is not None else str(local_asset_path))
        print(f"  Reprojektion + Write abgeschlossen: {write_target}  "
              f"({t_reproject:.2f} s)")

        # --snap-dem-to-s2 Pflicht-Verifikation (NICHT in preprocessing_time:
        # laeuft nach dem gemessenen Reprojektions-/Write-Block und dient nur
        # dem lokalen Beweis, nicht der Pipeline):
        #   1. Grid-Check: sitzt der gesnappte Puffer exakt auf dem
        #      erwarteten CDSE-Zielgitter (projizierter Extent, outward
        #      auf 10 m)?
        #   2. Crop-Identitaet: gesnappter Puffer == bitgenauer Ausschnitt
        #      des ungesnappten Puffers auf der Extent-Schnittmenge
        #      (np.array_equal, Fenster unabhaengig aus den Geo-Koordinaten
        #      hergeleitet). Beweist lokal, dass das Snapping nur
        #      zugeschnitten und keine Werte veraendert hat.
        # Bei Verletzung wird abgebrochen: ein Lauf, dessen lokaler Beweis
        # scheitert, ist fuer den MIT/OHNE-Vergleich wertlos und wuerde nur
        # CDSE-Zeit verbrennen.
        if snap_to_s2:
            grid_ok = _verify_snap_grid(dst_meta, snap_grid)
            crop_ok = _verify_snap_crop_identity(data, dst_meta,
                                                 data_full, meta_full)
            if not (grid_ok and crop_ok):
                raise RuntimeError(
                    "--snap-dem-to-s2 Verifikation fehlgeschlagen "
                    f"(grid_ok={grid_ok}, crop_identity_ok={crop_ok}) - "
                    "Abbruch vor Upload/CDSE."
                )

        # --dem-tiles Pflicht-Verifikation (NICHT in preprocessing_time):
        # die Vereinigung der Kacheln muss bitgenau dem Einzel-DEM
        # entsprechen (Abdeckung, Byte-Identitaet, Extent-/Shape-Summe).
        # Bei Verletzung Abbruch vor Upload - ein Lauf mit fehlerhafter
        # Zerlegung wuerde nur CDSE-Zeit verbrennen.
        if tiles is not None:
            if not _verify_tile_union_identity(tiles, data, dst_meta):
                raise RuntimeError(
                    "--dem-tiles Verifikation fehlgeschlagen (Vereinigung "
                    "der Kacheln != Einzel-DEM) - Abbruch vor Upload/CDSE."
                )

        # Schritt 3: Asset(s) nach Hetzner hochladen (Datei oder Verzeichnis)
        if tiles is not None:
            print(f"\n  [Schritt 3/5] {dem_tiles} Kachel-Assets auf Hetzner "
                  f"hochladen...")
            t_scp_asset = 0.0
            for tile_path, tile_name in zip(tile_local_paths,
                                            tile_asset_names):
                t_scp_asset += scp_upload(str(tile_path), tile_name)
            print(f"  Asset Upload fertig: {dem_tiles} Kacheln -> "
                  f"{HETZNER_URL_BASE}dem_reprojected_{region}_{run_ts}_"
                  f"tile*{asset_ext}  ({t_scp_asset:.2f} s)")
        else:
            print(f"\n  [Schritt 3/5] Asset auf Hetzner hochladen -> {remote_asset_name}...")
            if dem_format == "zarr":
                t_scp_asset = scp_upload_dir(str(local_asset_path), remote_asset_name)
            else:
                t_scp_asset = scp_upload(str(local_asset_path), remote_asset_name)
            print(f"  Asset Upload fertig: {asset_url}  ({t_scp_asset:.2f} s)")

        # Schritt 4: STAC Item(s) generieren + hochladen (media_type haengt
        # am dem_format). Bei --dem-tiles>1: ein Item PRO KACHEL (eigene
        # proj-Felder + eigener WGS84-Extent je Ausschnitt) in einer
        # Collection - der Treiber mosaikiert nur ueber Items, mehrere
        # Assets gleichen Bandnamens in EINEM Item wuerden bis auf das
        # erste stumm verworfen (s. build_dem_tiles_collection).
        if tiles is not None:
            print(f"\n  [Schritt 4/5] {dem_tiles} STAC Kachel-Items + "
                  f"Collection generieren + hochladen...")
            t_stac_start = time.time()
            collection_id = f"dem_collection_{region}_{run_ts}"
            items_with_urls = []
            for i, (_tile_data, tile_meta) in enumerate(tiles):
                item_url_i = f"{HETZNER_URL_BASE}{tile_item_names[i]}"
                tile_item = build_stac_item(
                    region=region,
                    asset_href=f"{HETZNER_URL_BASE}{tile_asset_names[i]}",
                    epsg=target_epsg,
                    item_id=f"dem_reprojected_{region}_{run_ts}_tile{i}",
                    extent=_wgs84_extent_from_meta(tile_meta),
                    dem_format=dem_format,
                    grid=_grid_from_dst_meta(tile_meta),
                    dataset=dataset,
                )
                _link_item_into_collection(tile_item, item_url_i,
                                           collection_id, collection_url)
                items_with_urls.append((tile_item, item_url_i))
                _a = tile_item["assets"]["data"]
                print(f"  Kachel {i}: href={_a['href']}")
                print(f"            proj:shape={_a.get('proj:shape')}  "
                      f"proj:bbox={_a.get('proj:bbox')}")
            stac_collection = build_dem_tiles_collection(
                collection_id, collection_url, items_with_urls)
            t_stac_build = time.time() - t_stac_start
            t_scp_stac = 0.0
            for (tile_item, _u), item_name in zip(items_with_urls,
                                                  tile_item_names):
                local_item_path = str(base / item_name)
                with open(local_item_path, "w") as f:
                    json.dump(tile_item, f, indent=2)
                t_scp_stac += scp_upload(local_item_path, item_name)
            local_coll_path = str(base / remote_coll_name)
            with open(local_coll_path, "w") as f:
                json.dump(stac_collection, f, indent=2)
            t_scp_stac += scp_upload(local_coll_path, remote_coll_name)
            t_stac = t_stac_build + t_scp_stac
            print(f"  STAC Upload fertig: {dem_tiles} Items + Collection "
                  f"{collection_url}  ({t_stac:.2f} s)")
        else:
            print(f"\n  [Schritt 4/5] STAC Item generieren + hochladen "
                  f"(media_type={_DEM_FORMAT_MEDIA_TYPE[dem_format]})...")
            t_stac_start = time.time()
            stac_item = build_stac_item(
                region=region,
                asset_href=asset_url,
                epsg=target_epsg,
                item_id=f"dem_reprojected_{region}_{run_ts}",
                extent=_compute_extent(region, args.extent_size),
                dem_format=dem_format,
                grid=_grid_from_dst_meta(dst_meta),
                dataset=dataset,
            )
            _stac_asset = stac_item["assets"]["data"]
            print(f"  STAC data-Asset: href={_stac_asset['href']}")
            print(f"                   proj:epsg={_stac_asset['proj:epsg']}  "
                  f"proj:shape={_stac_asset.get('proj:shape')}  "
                  f"proj:bbox={_stac_asset.get('proj:bbox')}")
            print(f"                   proj:transform={_stac_asset.get('proj:transform')}  "
                  f"eo:bands={_stac_asset.get('eo:bands')}")
            stac_collection = None
            if dem_format == "zarr":
                collection_id = f"dem_collection_{region}_{run_ts}"
                _link_item_into_collection(stac_item, stac_url,
                                           collection_id, collection_url)
                stac_collection = build_dem_stac_collection(
                    collection_id, collection_url, stac_item, stac_url)
            local_stac_path = str(base / remote_stac_name)
            with open(local_stac_path, "w") as f:
                json.dump(stac_item, f, indent=2)
            t_stac_build = time.time() - t_stac_start
            t_scp_stac = scp_upload(local_stac_path, remote_stac_name)
            if stac_collection is not None:
                local_coll_path = str(base / remote_coll_name)
                with open(local_coll_path, "w") as f:
                    json.dump(stac_collection, f, indent=2)
                t_scp_stac += scp_upload(local_coll_path, remote_coll_name)
            t_stac = t_stac_build + t_scp_stac
            print(f"  STAC Item Upload fertig: {stac_url}  ({t_stac:.2f} s)")
            if stac_collection is not None:
                print(f"  STAC Collection Upload fertig: {collection_url}  "
                      f"(Item via rel=item verlinkt, item_assets+summaries "
                      f"tragen proj/eo:bands)")

        # preprocessing_time = Reprojektion + SCP Upload + STAC (OHNE DEM Download)
        preprocessing_time = t_reproject + t_scp_asset + t_stac
        print(f"  Pre-Processing-Zeit (ohne DEM Download): {preprocessing_time:.2f} s")
        if t_download is not None and t_download > 0.0:
            print(f"  (DEM Download {t_download:.1f} s separat, nicht in preprocessing_time)")

        # Schritt 5: load_stac Szenario ausfuehren. Bei zarr und bei
        # --dem-tiles>1 zeigt load_stac auf die Collection-URL,
        # gtiff-Einzeldatei/netcdf unveraendert direkt auf die Item-URL.
        #
        # --zarr-via-item kehrt das NUR fuer zarr um (Versuch 7). Grund:
        # seit der shape-Injektion in die .zmetadata scheitert der Lauf
        # nicht mehr am Zarr-Parser ("missing key: 'shape'" ist weg),
        # sondern frueher im COLLECTION-Lesepfad:
        #   construct_item_collection: static Catalog ..., band_names=['DEM']
        #   ItemCollection.from_stac_catalog ... elapsed 0:00:00.046
        #   post_dry_run failed: 'NoneType' object has no attribute 'crs'
        #   Collected 0 projection metadata entries from 1 items
        # Das Item wird also gezaehlt, seine Assets aber nicht ausgewertet.
        # Der S2-Cube laeuft im selben Job ueber from_stac_api und sammelt
        # 1184 Eintraege - der statische Katalogpfad ist damit der
        # Verdaechtige, nicht das Format. netcdf funktioniert ueber die
        # ITEM-URL, deshalb hier dieselbe Einbindung fuer zarr.
        # Item + Injektion ist die einzige noch nie getestete Kombination.
        # --dem-tiles>1 bleibt IMMER Collection: dort ist die Collection
        # die Mosaik-Struktur selbst, nicht bloss ein Wrapper.
        zarr_via_item = bool(getattr(args, "zarr_via_item", False))
        use_collection = tiles is not None or (
            dem_format == "zarr" and not zarr_via_item)
        load_stac_url = collection_url if use_collection else stac_url
        via = "Collection" if use_collection else "Item"
        if dem_format == "zarr" and zarr_via_item:
            via += " (--zarr-via-item; Collection wurde trotzdem erzeugt "
            via += "und hochgeladen)"
        print(f"\n  [Schritt 5/5] load_stac Szenario auf CDSE ausfuehren "
              f"(url={via})...")
        scenario_filename = f"{strategy_label}_{region}.json"
        local_pp_scenario = build_local_pp_scenario(
            region, load_stac_url, base / scenario_filename,
            extent_size=args.extent_size,
            workflow=args.workflow,
            resample_s2_to_dem=getattr(args, "resample_s2_to_dem", False),
            resolution=resolution,
            dataset=dataset,
            resampling=args.local_resampling,
        )
        _write_run_meta(base, resolution, dataset=dataset)
        results_step5 = run_openeo(args.api_url, str(local_pp_scenario), str(step3_dir),
                                   job_timeout=args.job_timeout)
        # Nur Diagnose: der Rueckgabewert wird bewusst NICHT ausgewertet.
        # Die Meldung darf weder den Lauf abbrechen noch den spaeteren
        # Accuracy-Check verhindern - sonst fehlt bei einem Fehlalarm auch
        # noch die Metrik.
        if _is_categorical(dataset) and results_step5.get("status") == "success":
            verify_categorical_result(step3_dir, dataset, label="local_pp ")
        t_main = results_step5.get("total_time") or 0.0
        total_time = preprocessing_time + t_main

        # Diagnose: CDSE-Fehler koennen bedeuten dass das Format nicht
        # akzeptiert wurde. Kein stiller Fehlschlag - klare Meldung.
        cdse_status = results_step5.get("status", "unknown")
        cdse_error = str(results_step5.get("error") or "").lower()
        if dem_format != "gtiff" and cdse_status != "success":
            hints = ("load_stac", "format", "media type", "unsupported",
                     "cannot read", "invalid asset", "type")
            format_related = any(h in cdse_error for h in hints)
            if format_related:
                print(
                    f"\n  [DIAGNOSE] CDSE lehnt --dem-format={dem_format} "
                    f"ueber load_stac wahrscheinlich AB. Fehler: {cdse_error[:400]}"
                )
            else:
                print(
                    f"\n  [DIAGNOSE] CDSE-Job mit dem_format={dem_format} "
                    f"fehlgeschlagen (status={cdse_status}). Kein eindeutiger "
                    f"Hinweis auf Format-Ablehnung - bitte Fehlermeldung pruefen: "
                    f"{cdse_error[:400] or '(kein error-Feld)'}"
                )

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
            dem_format=dem_format,
            dem_snap="s2" if snap_to_s2 else None,
            dem_tiles=dem_tiles,
            resolution_m=resolution,
            dataset=dataset,
        )

        # Nginx Access-Logs vom Hetzner-Server holen (CDSE Zugriffe auf
        # Asset(s) + STAC). Bei --dem-tiles>1 alle Kachel-Assets + -Items
        # + Collection - die ZEITLICHE VERTEILUNG der Range-Requests ueber
        # die Kacheln ist dort die Messgroesse fuer Parallelitaet.
        if tiles is not None:
            log_filenames = (tile_asset_names + tile_item_names
                             + [remote_coll_name])
        else:
            log_filenames = [remote_asset_name, remote_stac_name]
        print(f"\n  [Logs] Hole nginx Access-Logs vom Hetzner-Server...")
        try:
            import_nginx_access_log(
                run_id, filenames=log_filenames,
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
    resolution = _resolution_of(args)
    dataset = _dataset_of(args)
    print(f"  Strategie: full_preprocessing  |  Region: {region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  {target_info}  |  Aufloesung: {resolution:g} m")
    _write_run_meta(base, resolution, dataset=dataset)

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
            extent_size=args.extent_size, dataset=dataset,
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
            # --resolution != 10: die von CDSE gelieferten S2-TIFs sind
            # nativ 10 m. Das S2-Grid gibt hier aber die Zellgroesse des
            # ganzen Runs vor (das DEM wird gleich exakt darauf gezogen),
            # also muss ZUERST S2 lokal auf die Zielaufloesung gebracht
            # werden - sonst bliebe --resolution in full_pp wirkungslos.
            if not _is_default_resolution(resolution):
                print(f"\n  [Schritt 3a/7] S2 lokal auf {resolution:g} m "
                      f"resamplen ({len(s2_tifs)} TIFs, --resolution)...")
                s2_res_dir = base / "step3a_s2_resampled"
                s2_res_dir.mkdir(exist_ok=True)
                resampled = []
                for stif in s2_tifs:
                    out = s2_res_dir / stif.name
                    with rasterio.open(stif) as _src:
                        _s2_crs = _src.crs.to_string()
                    t_s2_reproject_res = reproject_dem_local(
                        str(stif), str(out), dst_crs=_s2_crs,
                        resampling=args.local_resampling,
                        target_resolution=resolution,
                    )
                    resampled.append(out)
                s2_tifs = resampled
                print(f"  S2 auf {resolution:g} m gebracht "
                      f"({len(s2_tifs)} TIFs)")
            print(f"\n  [Schritt 3/7] DEM auf S2-Grid reprojizieren (lese Grid aus {s2_tifs[0].name})...")
            s2_grid = read_s2_grid(str(s2_tifs[0]))
            print(f"    S2-Grid: EPSG:{s2_grid['epsg']}, shape={s2_grid['shape']}, transform={s2_grid['transform']}")
            t_dem_reproject = reproject_dem_to_grid(
                dem_tif, dem_repro_tif, s2_grid,
                resampling=args.local_resampling,
            )
            dem_epsg = s2_grid["epsg"]
        else:
            print(f"\n  [Schritt 3/7] DEM nach {target_crs_str} reprojizieren "
                  f"({args.local_resampling}, {resolution:g} m)...")
            t_dem_reproject = reproject_dem_local(
                dem_tif, dem_repro_tif, dst_crs=target_crs_str,
                resampling=args.local_resampling,
                target_resolution=resolution,
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
                    target_resolution=(
                        None if _is_default_resolution(resolution)
                        else resolution),
                )
                new_tifs.append(out)
            s2_for_upload = new_tifs
            print(f"  S2 Reprojektion fertig  ({t_s2_reproject:.2f} s)")

        # Schritt 3c: ALLE TIFs mit einfachem, breit dekodierbarem Profil
        # neu schreiben (S2 + DEM). NEUER Default 'simple_striped' (identisch
        # zu dem was local_pp fuer das DEM benutzt und was CDSE nachweislich
        # sauber verarbeitet). Sofort danach lokal per rasterio komplett
        # dekodieren - so ist belegbar, dass die hochgeladenen Dateien SELBST
        # lesbar sind und CDSE keine korrupte Eingabe bekommt.
        upload_profile = getattr(args, "fullpp_upload_profile", "simple_striped")
        clean_dir = base / "step3c_clean"
        clean_dir.mkdir(exist_ok=True)
        print(f"\n  [Schritt 3c/7] S2 + DEM neu schreiben "
              f"(profile={upload_profile}) und LOKAL auf Lesbarkeit "
              f"pruefen...")
        t_clean_start = time.time()
        clean_s2 = []
        for stif in s2_for_upload:
            out = clean_dir / stif.name
            _rewrite_tif_clean(str(stif), str(out), profile=upload_profile)
            _verify_tif_readable(str(out), label=f"S2 {stif.name}")
            clean_s2.append(out)
        s2_for_upload = clean_s2
        clean_dem_tif = str(clean_dir / "dem.tif")
        _rewrite_tif_clean(dem_repro_tif, clean_dem_tif, profile=upload_profile)
        _verify_tif_readable(clean_dem_tif, label="DEM")
        dem_for_upload = clean_dem_tif
        t_clean = time.time() - t_clean_start
        print(f"  {len(clean_s2)} S2 + 1 DEM neu geschrieben + lokal "
              f"vollstaendig dekodiert  ({t_clean:.2f} s)")

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
        # Grid aus dem reprojizierten DEM-GeoTIFF lesen (full_pp ist immer
        # gtiff; das Clean-Rewrite aendert nur das Layout, nie das Grid).
        dem_item = build_stac_item(
            region=region, asset_href=dem_asset_url, epsg=dem_epsg,
            item_id=f"full_pp_dem_{region}_{run_ts}", extent=geo_extent,
            grid=read_s2_grid(dem_repro_tif), dataset=dataset,
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
        fullpp_save_format = getattr(args, "fullpp_save_format", "GTiff")
        if fullpp_save_format != "GTiff":
            print(f"\n  [Diagnose] full_pp save_result Format ueberschrieben "
                  f"auf {fullpp_save_format} (Schritt 4 der Ursachensuche).")
        scenario_path = build_full_pp_scenario(
            region, collection_url, dem_stac_url,
            base / f"full_preprocessing_{region}.json",
            extent_size=args.extent_size, workflow=args.workflow,
            save_format=fullpp_save_format,
            resolution=resolution,
            dataset=dataset,
            resampling=args.local_resampling,
        )
        results_main = run_openeo(args.api_url, str(scenario_path), str(main_dir),
                                  job_timeout=args.job_timeout)
        # Nur Diagnose: der Rueckgabewert wird bewusst NICHT ausgewertet.
        # Die Meldung darf weder den Lauf abbrechen noch den spaeteren
        # Accuracy-Check verhindern - sonst fehlt bei einem Fehlalarm auch
        # noch die Metrik.
        if _is_categorical(dataset) and results_main.get("status") == "success":
            verify_categorical_result(main_dir, dataset, label="full_pp ")
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
            resolution_m=resolution,
            dataset=dataset,
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
      lc_overlay           -> die Klassenkarte auf dem S2-Gitter (das lokale
                              Gegenstueck zum durchreichenden overlap_resolver)
      lc_mask              -> S2[B04], maskiert auf LC_MASK_CLASS

    Schreibt openEO_*.tif unter denselben Dateinamen wie die S2-Eingaben in
    out_dir und gibt deren Pfade zurueck.

    dtype des Outputs: float32 fuer alle rechnenden Workflows; bei
    lc_overlay bleibt das Raster im Quell-dtype (uint8), weil Klassen-IDs
    keine Fliesskommazahlen sind - sonst wuerden spaeter Klassen ueber
    float-Gleichheit verglichen.
    """
    import numpy as np

    with rasterio.open(str(dem_tif)) as dem_src:
        dem_raw = dem_src.read(1)
        dem_data = dem_raw.astype(np.float64)
        ref_meta = dem_src.meta.copy()
        ref_transform = dem_src.transform

    def _write_single(out_path: Path, data, meta=None):
        m = (meta if meta is not None else ref_meta).copy()
        m.update({"count": 1, "dtype": "float32"})
        with rasterio.open(out_path, "w", **m) as dst:
            dst.write(data.astype(np.float32), 1)

    def _write_categorical(out_path: Path, data, meta=None):
        """Klassenraster im Quell-dtype schreiben (kein float-Cast)."""
        m = (meta if meta is not None else ref_meta).copy()
        m.update({"count": 1, "dtype": str(dem_raw.dtype)})
        with rasterio.open(out_path, "w", **m) as dst:
            dst.write(data.astype(dem_raw.dtype), 1)

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

            if workflow == "lc_overlay":
                # Gegenstueck zum durchreichenden overlap_resolver: das
                # Ergebnis IST die Klassenkarte auf dem gemeinsamen Gitter.
                # S2 wird nur gelesen, um den Shape-Check zu fahren.
                _write_categorical(out_dir / s2_tif.name, dem_raw)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow == "lc_mask":
                # B04 behalten, wo die Klasse getroffen ist, sonst NaN.
                # Gegenstueck zu mask(data=B04, mask=NOT(klasse==ziel)).
                keep = (dem_raw == LC_MASK_CLASS)
                result = np.where(keep, s2_data[0], np.nan)
                _write_single(out_dir / s2_tif.name, result)
                output_tifs.append(out_dir / s2_tif.name)

            elif workflow in ("merge_add", "resample"):
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
    resolution = _resolution_of(args)
    dataset = _dataset_of(args)
    marker_scenario_path = base / f"local_reference_{region}.json"
    template = _load_bench_template(region, args.extent_size)
    marker_pg = _build_workflow_pg(template, args.workflow, region=region,
                                   resolution=resolution, dataset=dataset,
                                   resampling=args.local_resampling)
    with open(marker_scenario_path, "w") as f:
        json.dump({
            "process_graph": marker_pg,
            "_local_reference": {
                "target_crs": target_crs_str,
                "resampling": args.local_resampling,
                "target_resolution_m": resolution,
                "workflow": args.workflow,
                "dataset": dataset,
            },
        }, f, indent=2)
    _write_run_meta(base, resolution, dataset=dataset)

    print(f"\n{'='*60}")
    print(f"  Strategie: local_reference  |  Region: {region}  |  Extent: {args.extent_size}  |  Workflow: {args.workflow}  |  Run {repeat_idx+1}/{args.repeat}  |  {run_type}")
    print(f"  Output: {base}  |  Target-CRS: {target_crs_str}  |  "
          f"Aufloesung: {resolution:g} m  |  Resampling: {args.local_resampling}")

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
            extent_size=args.extent_size, dataset=dataset,
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
              f"{args.local_resampling}, {resolution:g} m, {target_crs_str})...")
        repro_dir = base / "step3_reprojected"
        repro_dir.mkdir()
        t_repro_start = time.time()

        s2_repro_tifs = []
        for s2_tif in s2_tifs:
            out = repro_dir / s2_tif.name
            reproject_dem_local(
                str(s2_tif), str(out),
                dst_crs=target_crs_str, resampling=args.local_resampling,
                target_resolution=resolution,
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
            resolution_m=resolution,
            dataset=dataset,
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
    # lc_mask ZUERST: der Graph hat kein merge1, dafuer die eigenen Knoten.
    if "lcmask1" in root or "lcmaskbuild1" in root:
        return "lc_mask"
    # lc_overlay: merge1 ohne overlap_resolver + filter_bands aufs Klassenband.
    if "filterbands_lc" in root:
        return "lc_overlay"
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
            resolver = ov.get("process_graph", {})
            for node in resolver.values():
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
    bestimmen, oder None. Nur Graphen mit merge1 (oder applykernel1 bzw.
    dem lc_mask-Knoten) werden ausgewertet, damit reine S2-/DEM-Download-
    Szenarien (die in full_pp Runs ebenfalls als JSON liegen) ignoriert
    werden.
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
        if ("merge1" not in root and "applykernel1" not in root
                and "lcmask1" not in root):
            continue
        wf = _detect_pg_workflow(pg)
        if wf:
            return wf
    return None


def _detect_folder_resolution(folder: Path):
    """Ziel-Zellgroesse eines Run-Ordners in Metern, oder None.

    Reihenfolge:
      1. run_meta.json (von allen Runs seit --resolution geschrieben)
      2. _local_reference.target_resolution_m im Marker-Szenario
      3. Pixelgroesse des ersten gefundenen Ergebnis-TIF (Fallback fuer
         Runs, die vor --resolution entstanden sind - dort ist die
         tatsaechliche Zellgroesse die verlaesslichste Quelle)
    None heisst "unbekannt", nicht "10".
    """
    meta_path = folder / RUN_META_FILENAME
    if meta_path.is_file():
        try:
            val = json.loads(meta_path.read_text()).get("resolution_m")
            if val is not None:
                return float(val)
        except Exception:
            pass
    for cand in sorted(folder.glob("*.json")):
        try:
            doc = json.loads(cand.read_text())
        except Exception:
            continue
        if isinstance(doc, dict):
            ref = doc.get("_local_reference")
            if isinstance(ref, dict) and ref.get("target_resolution_m"):
                try:
                    return float(ref["target_resolution_m"])
                except (TypeError, ValueError):
                    pass
    for tif in sorted(folder.rglob("*.tif")):
        try:
            with rasterio.open(tif) as src:
                return abs(float(src.transform.a))
        except Exception:
            continue
    return None


def _detect_folder_dataset(folder: Path):
    """Datensatz-Paar eines Run-Ordners, oder None.

    Reihenfolge:
      1. run_meta.json (von allen Runs seit --dataset geschrieben)
      2. die Kollektions-ID in loadcollection2 eines Szenario-JSON
         (Fallback fuer Ordner ohne run_meta.json)
    None heisst "unbekannt", nicht "dem".
    """
    meta_path = folder / RUN_META_FILENAME
    if meta_path.is_file():
        try:
            val = json.loads(meta_path.read_text()).get("dataset")
            if val in DATASETS:
                return val
        except Exception:
            pass
    by_collection = {info["collection"]: name
                     for name, info in DATASETS.items()}
    for cand in sorted(folder.glob("*.json")):
        try:
            doc = json.loads(cand.read_text())
        except Exception:
            continue
        root = doc.get("process_graph", doc)
        if not isinstance(root, dict):
            continue
        for node in root.values():
            if not isinstance(node, dict):
                continue
            if node.get("process_id") != "load_collection":
                continue
            cid = (node.get("arguments") or {}).get("id")
            if cid in by_collection:
                return by_collection[cid]
    return None


def _folder_matches_dataset(folder: Path, dataset: str) -> bool:
    """Passt der Run-Ordner zum geforderten Datensatz-Paar?

    Unbekannt (weder run_meta.json noch erkennbare Kollektion) wird NUR fuer
    das historische Default-Paar akzeptiert - alle Laeufe vor dieser
    Experimentdimension liefen gegen COPERNICUS_30. Bei abweichender
    Anfrage lieber kein Kandidat als der falsche: ein Landcover-Lauf gegen
    eine DEM-Referenz verglichen liefert stumm Unsinn.
    """
    detected = _detect_folder_dataset(folder)
    if detected is None:
        return dataset == DEFAULT_DATASET
    return detected == dataset


def _folder_matches_resolution(folder: Path, resolution: float) -> bool:
    """Passt der Run-Ordner zur geforderten Zellgroesse?

    Unbekannte Aufloesung (weder run_meta.json noch Marker noch lesbares
    TIF) wird NUR fuer die historische Default-Aufloesung akzeptiert: alle
    Laeufe vor dieser Experimentdimension liefen mit 10 m. Bei einer
    abweichenden Anfrage lieber keinen Kandidaten als den falschen - sonst
    vergleicht der Accuracy-Check zwei verschiedene Gitter und die
    MAE-Werte waeren wertlos.
    """
    detected = _detect_folder_resolution(folder)
    if detected is None:
        return _is_default_resolution(resolution)
    return abs(detected - float(resolution)) < 1e-6


def _find_latest_run_dir(base: str, suffix: str, region: str,
                          extent_size: str = None,
                          workflow: str = None,
                          resolution: float = None,
                          dataset: str = None):
    """Neuesten outputs/run_*_{suffix} fuer Region zurueckgeben, oder None.

    Wenn extent_size gesetzt ist, werden nur Ordner beruecksichtigt, deren
    Scenario-JSON exakt diesen Extent enthaelt (Bounding-Box-Vergleich).
    Wenn workflow gesetzt ist, muss zusaetzlich der im Process Graph
    erkannte Workflow uebereinstimmen - das verhindert das versehentliche
    Vergleichen verschiedener Workflow-Varianten der gleichen Region/Extent.
    Wenn resolution gesetzt ist, muss auch die Zellgroesse uebereinstimmen -
    ohne das wuerde ein 60-m-Run gegen eine 10-m-Referenz verglichen.
    Wenn dataset gesetzt ist, muss zusaetzlich das Datensatz-Paar passen -
    sonst wuerde ein Landcover-Lauf gegen eine DEM-Referenz verglichen.
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
        if resolution is not None and not _folder_matches_resolution(d, resolution):
            continue
        if dataset is not None and not _folder_matches_dataset(d, dataset):
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


def _compare_tif_pair_categorical(ref_tif: Path, test_tif: Path,
                                  validity_only: bool = False,
                                  nodata=None):
    """Kategorialer Vergleich eines TIF-Paars.

    Gibt das Dict von calculate_categorical_metrics zurueck (oder None bei
    Fehler). Das Alignment laeuft IMMER mit nearest - alles andere wuerde
    Klassen-IDs mischen.

    Die Nodata-Werte werden aus BEIDEN Dateien gelesen und zusammen mit dem
    uebergebenen Registry-Wert ausgeschlossen. Nur den Registry-Wert zu
    nehmen reicht nicht: dort steht 0, die CDSE-Datei traegt aber -32768.
    Ohne das zaehlen Nodata-Pixel als perfekt uebereinstimmendes
    Klassenpaar mit und -32768 landet als eigene "Klasse" in der
    Verwechslungsmatrix.
    """
    try:
        from accuracy_calculator import (align_rasters,
                                         calculate_categorical_metrics)
    except Exception as exc:
        print(f"  Import-Fehler fuer accuracy_calculator: {exc}")
        return None

    nodata_values = set()
    if isinstance(nodata, (list, tuple, set, frozenset)):
        nodata_values |= {v for v in nodata if v is not None}
    elif nodata is not None:
        nodata_values.add(nodata)
    for path in (ref_tif, test_tif):
        try:
            with rasterio.open(path) as src:
                if src.nodata is not None:
                    nodata_values.add(src.nodata)
        except Exception:
            pass

    try:
        ref_data, test_data, _ = align_rasters(
            str(ref_tif), str(test_tif), resampling_method="nearest",
        )
        return calculate_categorical_metrics(
            ref_data, test_data, nodata=sorted(nodata_values) or None,
            validity_only=validity_only)
    except Exception as exc:
        print(f"  Vergleich fehlgeschlagen ({ref_tif.name}): {exc}")
        return None


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


def _ensure_accuracy_reference_column(conn) -> None:
    """Idempotente Migration fuer reference_run_id in aelteren DBs.

    Wird vor jedem INSERT in accuracy aufgerufen damit die Insert-Liste
    stabil bleibt selbst wenn create_database() nicht lief.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('accuracy')").fetchall()}
    if "reference_run_id" not in cols:
        conn.execute("ALTER TABLE accuracy ADD COLUMN reference_run_id INTEGER")
    # Kategoriale Metriken in EIGENEN Spalten. Sie duerfen NICHT in
    # rmse/mae landen: analyze.fetch_accuracy mittelt blind ueber run_id,
    # eine Uebereinstimmungsquote in der mae-Spalte wuerde alle bisherigen
    # Auswertungen still verfaelschen.
    if "agreement_pct" not in cols:
        conn.execute("ALTER TABLE accuracy ADD COLUMN agreement_pct DOUBLE")
    if "kappa" not in cols:
        conn.execute("ALTER TABLE accuracy ADD COLUMN kappa DOUBLE")
    if "confusion_json" not in cols:
        conn.execute("ALTER TABLE accuracy ADD COLUMN confusion_json TEXT")
    if "metric_kind" not in cols:
        conn.execute("ALTER TABLE accuracy ADD COLUMN metric_kind TEXT")


def _persist_accuracy(run_id: int, mae: float, rmse: float,
                      reference_file: str,
                      reference_run_id: int = None,
                      agreement_pct: float = None,
                      kappa: float = None,
                      confusion_json: str = None,
                      metric_kind: str = "continuous") -> None:
    """Accuracy-Metriken in die accuracy-Tabelle schreiben.

    metric_kind='continuous': mae/rmse gesetzt, kategoriale Spalten NULL.
    metric_kind='categorical'/'categorical_validity': agreement_pct/kappa/
    confusion_json gesetzt, mae/rmse bleiben NULL - ueber Klassen-IDs sind
    sie bedeutungslos, und ein Wert dort wuerde in bestehende Auswertungen
    einlaufen.

    reference_run_id: run_id des Ground-Truth-Runs (onthefly oder
    local_reference). Wird fuer die Cleanup-Abhaengigkeitsanalyse gebraucht:
    solange ein CDSE-Run seinen accuracy-Eintrag noch nicht hat, darf die
    zugehoerige Referenz nicht geloescht werden.
    """
    try:
        import duckdb
        from database import DB_PATH
        conn = duckdb.connect(DB_PATH)
        _ensure_accuracy_reference_column(conn)
        next_id = conn.execute(
            "SELECT COALESCE(MAX(accuracy_id), 0) + 1 FROM accuracy"
        ).fetchone()[0]
        conn.execute(
            '''INSERT INTO accuracy
               (accuracy_id, run_id, reference_file, reference_run_id,
                rmse, mae, agreement_pct, kappa, confusion_json, metric_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (next_id, run_id, reference_file, reference_run_id, rmse, mae,
             agreement_pct, kappa, confusion_json, metric_kind),
        )
        conn.commit()
        conn.close()
        ref_str = f", reference_run_id={reference_run_id}" if reference_run_id else ""
        print(f"  Accuracy gespeichert (accuracy_id={next_id}, "
              f"run_id={run_id}, metric_kind={metric_kind}{ref_str})")
    except Exception as exc:
        print(f"  WARNUNG: Accuracy nicht in DB geschrieben: {exc}")


# ---------------------------------------------------------------------------
# Plattenplatz-Schutz + Cleanup (verhindert das 100%-Fuell-Fiasko)
# ---------------------------------------------------------------------------

def _free_gb(path) -> float:
    """Freier Platz in GB im Filesystem von path. path wird auf existierende
    Elternebene reduziert falls es selbst noch nicht existiert."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    if not p.exists():
        p = Path(".").resolve()
    usage = shutil.disk_usage(str(p))
    return usage.free / (1024 ** 3)


def check_disk_space(output_dir: str, min_free_gb: float,
                     context: str = "") -> None:
    """Bricht mit RuntimeError ab wenn der freie Platz unter min_free_gb faellt.

    context: kurzes Label fuer die Fehlermeldung, z.B. "Strategie=local_pp,
    Run 1/3". Wird bei jedem einzelnen Run vor dem Start aufgerufen, damit
    ein anlaufender Batch nicht spaeter mitten drin die Platte volllaeuft.
    """
    if min_free_gb is None or min_free_gb <= 0:
        return
    free = _free_gb(output_dir)
    print(f"  [disk-check] Freier Platz: {free:.1f} GB "
          f"(Schwelle {min_free_gb:.1f} GB)  {context}")
    if free < min_free_gb:
        raise RuntimeError(
            f"Zu wenig freier Speicher fuer '{output_dir}': {free:.1f} GB "
            f"frei, aber --min-free-gb={min_free_gb:.1f}. "
            f"Run abgebrochen bevor er startet ({context})."
        )


def _run_has_accuracy(run_id: int) -> bool:
    """True wenn fuer run_id mindestens eine accuracy-Zeile existiert.

    _persist_accuracy schreibt nur bei erfolgreichem Vergleich - jede Zeile
    ist damit gleichbedeutend mit 'Accuracy erfolgreich gemessen'.
    """
    if run_id is None:
        return False
    try:
        import duckdb
        from database import DB_PATH
        conn = duckdb.connect(DB_PATH, read_only=True)
        row = conn.execute(
            "SELECT 1 FROM accuracy WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as exc:
        print(f"  WARNUNG: accuracy-Lookup fuer run_id={run_id} fehlgeschlagen: {exc}")
        return False


def _accuracy_test_run_ids_for_reference(reference_run_id: int) -> set:
    """Alle test-run_ids die diese Referenz erfolgreich verwendet haben.

    Fuer die Entscheidung ob eine local_reference geloescht werden darf:
    solange ein erwarteter Abhaengiger noch nicht in dieser Menge steht,
    bleibt die Referenz erhalten.
    """
    if reference_run_id is None:
        return set()
    try:
        import duckdb
        from database import DB_PATH
        conn = duckdb.connect(DB_PATH, read_only=True)
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM accuracy WHERE reference_run_id = ?",
            (reference_run_id,),
        ).fetchall()
        conn.close()
        return {r[0] for r in rows if r[0] is not None}
    except Exception as exc:
        print(f"  WARNUNG: accuracy-Deps-Lookup fuer reference_run_id="
              f"{reference_run_id} fehlgeschlagen: {exc}")
        return set()


def _list_run_tifs(run_dir: Path) -> list:
    """Alle *.tif rekursiv unter run_dir. Case-insensitive.

    Werden ausschliesslich die grossen Raster geloescht - results.json,
    scenario_*.json, STAC Items und sonstige Metadaten bleiben unberuehrt.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return []
    tifs = []
    for pat in ("*.tif", "*.TIF", "*.tiff", "*.TIFF"):
        tifs.extend(run_dir.rglob(pat))
    # dedupe (case-insensitive glob ueberlappt auf case-insensitive FS)
    return sorted({p.resolve() for p in tifs if p.is_file()})


def delete_run_tifs(run_dir: Path, run_id, output_dir: str = "outputs",
                    dry_run: bool = False, label: str = "") -> dict:
    """Loescht alle *.tif eines Run-Ordners. Loggt run_id, Ordner, Anzahl,
    freien Platz danach. Gibt Statistik-Dict zurueck.

    dry_run: nichts wirklich loeschen, nur berichten was WUERDE geloescht.
    """
    run_dir = Path(run_dir)
    tifs = _list_run_tifs(run_dir)
    total_bytes = sum(p.stat().st_size for p in tifs if p.exists())
    label_str = f" [{label}]" if label else ""
    print(f"\n  [cleanup{label_str}] run_id={run_id}  dir={run_dir}")
    print(f"    Kandidaten: {len(tifs)} TIF-Dateien, "
          f"{total_bytes / (1024**2):.1f} MB gesamt")

    deleted = 0
    freed = 0
    for p in tifs:
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if dry_run:
            print(f"    [dry-run] wuerde loeschen: {p}  ({sz / 1024:.1f} KB)")
            continue
        try:
            p.unlink()
            deleted += 1
            freed += sz
        except OSError as exc:
            print(f"    FEHLER beim Loeschen von {p}: {exc}")

    if dry_run:
        print(f"    [dry-run] Summe: {len(tifs)} Dateien, "
              f"{total_bytes / (1024**2):.1f} MB wuerden freigegeben")
    else:
        free_gb_after = _free_gb(output_dir)
        print(f"    Geloescht: {deleted}/{len(tifs)} Dateien, "
              f"{freed / (1024**2):.1f} MB freigegeben. "
              f"Freier Platz jetzt: {free_gb_after:.1f} GB")

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "candidates": len(tifs),
        "deleted": deleted,
        "freed_bytes": freed if not dry_run else 0,
        "dry_run": dry_run,
    }


# Strategien die eine Referenz verwenden (also von Accuracy abhaengen).
_CDSE_TEST_STRATEGIES = ("onthefly", "local_preprocessing",
                         "local_pp_cached", "full_preprocessing")


def cleanup_after_accuracy(session_results: list, output_dir: str,
                           dry_run: bool = False) -> None:
    """Orchestriert das Aufraeumen NACH allen Accuracy-Checks.

    Zwingende Reihenfolge:
      1. CDSE-Strategie-Runs (onthefly / local_pp / full_pp) werden geloescht
         wenn ihr eigener Accuracy-Eintrag in der DB steht.
      2. local_reference wird ERST geloescht wenn jeder erwartete
         Abhaengige (gleiche Region + Extent + Workflow, aus dieser Session)
         seinen Accuracy-Eintrag mit reference_run_id=local_reference.run_id
         hat.

    Damit ist ausgeschlossen, dass die Referenz vor den auf sie
    verweisenden CDSE-Runs verschwindet.
    """
    if not session_results:
        return

    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"\n{'='*60}")
    print(f"  Cleanup nach Accuracy-Check  [{mode}]")

    # Referenzen und CDSE-Runs in dieser Session identifizieren.
    ref_runs = []       # local_reference-Runs
    test_runs = []      # CDSE-Runs
    for r in session_results:
        if r.get("run_id") is None or r.get("status") != "success":
            continue
        s = r.get("strategy")
        if s == "local_reference":
            ref_runs.append(r)
        elif s in _CDSE_TEST_STRATEGIES:
            test_runs.append(r)

    print(f"  Session: {len(test_runs)} CDSE-Runs, {len(ref_runs)} local_reference-Runs")

    # 1) CDSE-Runs
    for r in test_runs:
        run_id = r["run_id"]
        run_dir = Path(r["outdir"])
        if not _run_has_accuracy(run_id):
            print(f"\n  [skip] run_id={run_id} strategy={r.get('strategy')} - "
                  f"kein Accuracy-Eintrag in DB. TIFs bleiben erhalten.")
            continue
        delete_run_tifs(run_dir, run_id, output_dir=output_dir,
                        dry_run=dry_run, label=r.get("strategy"))

    # 2) local_reference-Runs mit Abhaengigkeits-Pruefung
    for r in ref_runs:
        ref_id = r["run_id"]
        run_dir = Path(r["outdir"])
        region = r.get("region")
        extent_size = r.get("extent_size")
        workflow = r.get("workflow")

        # Erwartete Abhaengige aus dieser Session: alle CDSE-Runs mit
        # gleicher Region + Extent + Workflow.
        expected_deps = {
            t["run_id"] for t in test_runs
            if t.get("region") == region
            and t.get("extent_size") == extent_size
            and t.get("workflow") == workflow
        }
        actual_deps = _accuracy_test_run_ids_for_reference(ref_id)
        missing = expected_deps - actual_deps

        if missing:
            print(f"\n  [skip] local_reference run_id={ref_id} - "
                  f"noch {len(missing)} abhaengige Run(s) ohne "
                  f"Accuracy-Eintrag (run_ids={sorted(missing)}). "
                  f"Referenz bleibt vollstaendig erhalten.")
            continue
        if not expected_deps:
            print(f"\n  [skip] local_reference run_id={ref_id} - "
                  f"in dieser Session keine passenden CDSE-Runs "
                  f"(Region={region}, Extent={extent_size}, "
                  f"Workflow={workflow}). Referenz bleibt erhalten "
                  f"(koennte spaeter noch gebraucht werden).")
            continue

        print(f"\n  [ok] local_reference run_id={ref_id}: alle "
              f"{len(expected_deps)} Abhaengigen (run_ids="
              f"{sorted(expected_deps)}) haben Accuracy-Eintrag "
              f"-> Referenz kann aufgeraeumt werden.")
        delete_run_tifs(run_dir, ref_id, output_dir=output_dir,
                        dry_run=dry_run, label="local_reference")

    print(f"\n  Cleanup abgeschlossen  [{mode}]")


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
                       reference_strategy: str = "onthefly",
                       resolution: float = None,
                       dataset: str = None):
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

    Bei kategorialen Datensatz-/Workflow-Kombinationen (--dataset landcover)
    wird stattdessen die Uebereinstimmungsquote + Cohen's Kappa berechnet und
    in die eigenen Spalten geschrieben; mae/rmse bleiben NULL. Aggregiert
    wird dort PIXELGEWICHTET ueber alle Date-TIFs, nicht per Median: ein
    Median von Quoten ueber unterschiedlich grosse Bilder verzerrt.
    """
    if reference_strategy not in _ACCURACY_LAYOUT:
        raise ValueError(
            f"reference_strategy='{reference_strategy}' nicht bekannt. "
            f"Erlaubt: {sorted(_ACCURACY_LAYOUT)}"
        )

    print(f"\n{'='*60}")
    extent_info = f"  |  Extent: {extent_size}" if extent_size else ""
    workflow_info = f"  |  Workflow: {workflow}" if workflow else ""
    res_info = f"  |  Aufloesung: {resolution:g} m" if resolution else ""
    ds_info = f"  |  Datensatz: {dataset}" if dataset else ""
    print(f"  Accuracy-Check vs '{reference_strategy}'"
          f"  |  Region: {region}{extent_info}{workflow_info}{res_info}"
          f"{ds_info}")

    ref_suffix, _ = _ACCURACY_LAYOUT[reference_strategy]
    reference_dir = _find_latest_run_dir(output_base, ref_suffix, region,
                                         extent_size=extent_size,
                                         workflow=workflow,
                                         resolution=resolution,
                                         dataset=dataset)

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
                                    workflow=workflow,
                                    resolution=resolution,
                                    dataset=dataset) is not None:
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
                                    workflow=workflow,
                                    resolution=resolution,
                                    dataset=dataset)

    if not reference_dir or not test_dir:
        miss = reference_strategy if not reference_dir else test_strategy
        extent_msg = f" mit extent_size='{extent_size}'" if extent_size else ""
        wf_msg = f" und workflow='{workflow}'" if workflow else ""
        res_msg = f" und Aufloesung {resolution:g} m" if resolution else ""
        print(f"  Skip: kein {miss}-Run fuer Region '{region}'"
              f"{extent_msg}{wf_msg}{res_msg} gefunden.")
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

    # Kategorialer Zweig: Uebereinstimmungsquote statt MAE/RMSE.
    categorical = bool(dataset and workflow
                       and _categorical_output(dataset, workflow))
    if categorical:
        return _run_accuracy_check_categorical(
            common, reference_tifs, test_tifs, dataset, workflow,
            region, reference_strategy, test_strategy,
            reference_dir, test_dir, test_tif_dir, test_run_id)

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

    # reference_run_id: fuer die Cleanup-Abhaengigkeitsanalyse. results.json
    # der Referenz liegt sowohl bei onthefly als auch bei local_reference im
    # Run-Root - _lookup_run_id_for_dir liest exakt das.
    reference_run_id = _lookup_run_id_for_dir(reference_dir)

    if run_id is not None:
        _persist_accuracy(run_id, median_mae, median_rmse,
                          str(reference_dir),
                          reference_run_id=reference_run_id)
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
        "reference_run_id": reference_run_id,
        "reference_dir": str(reference_dir),
        "test_dir": str(test_dir),
    }


def _run_accuracy_check_categorical(common, reference_tifs, test_tifs,
                                    dataset, workflow, region,
                                    reference_strategy, test_strategy,
                                    reference_dir, test_dir, test_tif_dir,
                                    test_run_id):
    """Kategorialer Teil von run_accuracy_check.

    Aggregation PIXELGEWICHTET ueber alle Date-TIFs (Summe der
    uebereinstimmenden Pixel / Summe der gueltigen), nicht per Median: der
    Median von Quoten ueber unterschiedlich grosse Bilder verzerrt. Kappa
    wird aus der aufsummierten Verwechslungsmatrix neu berechnet, nicht
    ueber Einzel-Kappas gemittelt.
    """
    # lc_mask liefert maskiertes B04 - dort steckt die Aussage in der
    # Maskenkante, nicht im Wert (s. CATEGORICAL_WORKFLOWS).
    validity_only = (workflow == "lc_mask")
    nodata = DATASETS[dataset].get("nodata") if not validity_only else None
    metric_kind = "categorical_validity" if validity_only else "categorical"

    total_agree = 0
    total_valid = 0
    total_px = 0
    merged_confusion = {}
    n_files = 0
    for name in common:
        res = _compare_tif_pair_categorical(
            reference_tifs[name], test_tifs[name],
            validity_only=validity_only, nodata=nodata)
        if not res or res.get("overall_accuracy") is None:
            continue
        n_files += 1
        total_agree += res["agreeing_pixels"]
        total_valid += res["valid_pixels"]
        total_px += res["total_pixels"]
        for ref_c, row in res["confusion"].items():
            dst = merged_confusion.setdefault(int(ref_c), {})
            for test_c, n in row.items():
                dst[int(test_c)] = dst.get(int(test_c), 0) + int(n)
        print(f"    {name}: Uebereinstimmung="
              f"{100.0 * res['overall_accuracy']:.4f}%  "
              f"kappa={res['kappa'] if res['kappa'] is None else round(res['kappa'], 6)}  "
              f"({res['valid_pixels']:,}/{res['total_pixels']:,} Pixel)")

    if not n_files or not total_valid:
        print(f"  Skip: kein valider Pixel-Vergleich moeglich "
              f"({n_files} vergleichbare Datei(en), {total_valid} gueltige "
              f"Pixel nach Nodata-Ausschluss) - es wird KEINE Zeile in die "
              f"accuracy-Tabelle geschrieben.")
        return None

    overall = total_agree / total_valid
    # Kappa aus der aggregierten Matrix.
    classes = set(merged_confusion)
    for row in merged_confusion.values():
        classes.update(row)
    p_e = 0.0
    for c in classes:
        n_ref = sum(merged_confusion.get(c, {}).values())
        n_test = sum(row.get(c, 0) for row in merged_confusion.values())
        p_e += (n_ref / total_valid) * (n_test / total_valid)
    kappa = ((overall - p_e) / (1.0 - p_e)) if (1.0 - p_e) > 1e-12 else None

    run_id = test_run_id
    if run_id is None:
        run_id = _lookup_run_id_for_dir(test_tif_dir)
    reference_run_id = _lookup_run_id_for_dir(reference_dir)

    confusion_json = json.dumps(
        {str(k): {str(k2): v2 for k2, v2 in v.items()}
         for k, v in sorted(merged_confusion.items())})

    if run_id is not None:
        # mae/rmse bleiben bewusst NULL - s. _persist_accuracy.
        _persist_accuracy(run_id, None, None, str(reference_dir),
                          reference_run_id=reference_run_id,
                          agreement_pct=100.0 * overall,
                          kappa=kappa,
                          confusion_json=confusion_json,
                          metric_kind=metric_kind)
    else:
        print(f"  WARNUNG: kein run_id fuer {test_strategy} gefunden - es "
              f"wird KEINE Zeile in die accuracy-Tabelle geschrieben "
              f"(die Metriken unten sind trotzdem gueltig).")

    kappa_str = f"{kappa:.6f}" if kappa is not None else "n/a"
    print(f"\n  Accuracy-Check ({metric_kind}): "
          f"Uebereinstimmung={100.0 * overall:.4f}%  kappa={kappa_str}  "
          f"({n_files} Dates, {total_agree:,}/{total_valid:,} Pixel gleich)")
    if merged_confusion:
        print(f"  Verwechslungen (Referenzklasse -> Testklasse, nur "
              f"abweichende):")
        for ref_c in sorted(merged_confusion):
            for test_c in sorted(merged_confusion[ref_c]):
                if test_c != ref_c:
                    print(f"    {ref_c:>4} -> {test_c:<4} "
                          f"{merged_confusion[ref_c][test_c]:,} px")

    return {
        "region": region,
        "reference_strategy": reference_strategy,
        "test_strategy": test_strategy,
        "metric_kind": metric_kind,
        "dataset": dataset,
        "workflow": workflow,
        "agreement_percent": 100.0 * overall,
        "kappa": kappa,
        "confusion": merged_confusion,
        "n_dates": n_files,
        "agreeing_pixels": total_agree,
        "valid_pixels": total_valid,
        "total_pixels": total_px,
        "run_id": run_id,
        "reference_run_id": reference_run_id,
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
    # default=None statt "merge_add": so ist unterscheidbar, ob der Nutzer
    # den Workflow gesetzt hat. Nicht gesetzt -> Default des Datensatzes
    # (merge_add fuer dem, lc_overlay fuer landcover), aufgeloest direkt
    # nach parse_args. Fuer --dataset dem aendert das nichts.
    parser.add_argument("--workflow", default=None,
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
                             "die mittleren 50%% des Extents). "
                             "NUR mit --dataset landcover: "
                             "lc_overlay (Default dort - merge_cubes bleibt, "
                             "aber der overlap_resolver reicht die "
                             "Klassenkarte durch statt zu addieren; Ergebnis "
                             "ist die Klassenkarte auf dem S2-Gitter, jedes "
                             "abweichende Pixel ist ein "
                             "Transformationsartefakt), "
                             "lc_mask (B04 auf eine Landbedeckungsklasse "
                             "maskiert; gemessen wird die Maskenkante). "
                             "Ohne Angabe wird der Default des Datensatzes "
                             "verwendet.")
    parser.add_argument("--local-resampling", default="nearest",
                        choices=tuple(LOCAL_RESAMPLING.keys()),
                        help="Resampling-Methode fuer die Reprojektion des "
                             "zweiten Rasters. nearest (Default) ist "
                             "pixelidentisch zu CDSE - der Accuracy-Check liefert "
                             "dann MAE=RMSE=0. bilinear/cubic weichen vom "
                             "CDSE-Output ab und machen den Accuracy-Check "
                             "aussagekraeftig. mode (Mehrheitsentscheidung) "
                             "nur mit --dataset landcover: das fachlich "
                             "richtige Vergroeberungsverfahren fuer Klassen, "
                             "weil es keine Klassen-IDs mittelt. "
                             "WIRKT AUF BEIDE SEITEN: der Wert bestimmt "
                             "seit dieser Aenderung auch die method der "
                             "serverseitigen resample_spatial-/"
                             "resample_cube_spatial-Knoten (nearest -> near). "
                             "Vorher stand dort fest 'near', wodurch lokale "
                             "und CDSE-seitige Vergroeberung bei "
                             "--resolution != 10 auseinanderliefen.")
    parser.add_argument("--resolution", type=float, default=DEFAULT_RESOLUTION_M,
                        help=f"Ziel-Zellgroesse in METERN fuer ALLE Pfade "
                             f"(Default {DEFAULT_RESOLUTION_M:g} = Sentinel-2 "
                             f"B04 nativ, bisheriges Verhalten byte-identisch). "
                             f"Steuert die lokale Reprojektion (Pixelgroesse + "
                             f"outward-Snap auf Vielfache dieses Werts), das "
                             f"aus dem Extent rekonstruierte Zielgitter "
                             f"(--snap-dem-to-s2) und die local_reference-"
                             f"Pipeline. Bei Werten != "
                             f"{DEFAULT_RESOLUTION_M:g} bekommt der openEO-"
                             f"Graph zusaetzlich ein explizites "
                             f"resample_spatial(projection, resolution) hinter "
                             f"loadcollection1, damit CDSE nicht sein natives "
                             f"10-m-Gitter erzwingt. Experimentdimension fuer "
                             f"den Einfluss der Zellgroesse auf Laufzeit, "
                             f"Datenvolumen und Genauigkeit; wird als "
                             f"resolution_m in die DB geschrieben. Referenz- "
                             f"und Test-Run muessen dieselbe Aufloesung haben - "
                             f"der Accuracy-Check waehlt die Referenz danach "
                             f"aus.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET,
                        choices=sorted(DATASETS),
                        help=f"Datensatz-Paar. Das erste Raster ist immer "
                             f"Sentinel-2 B04; variabel ist das ZWEITE. "
                             f"dem (Default): {DATASETS['dem']['label']} - "
                             f"bisheriges Verhalten, byte-identisch. "
                             f"landcover: {DATASETS['landcover']['label']}. "
                             f"Zweck: die bisherigen Genauigkeitsbefunde "
                             f"beruhen ausschliesslich auf kontinuierlichen "
                             f"Hoehendaten; ein kategorialer Datensatz macht "
                             f"sie belastbarer. Bei landcover gelten eigene "
                             f"Workflows (lc_overlay, lc_mask), nur "
                             f"--local-resampling=nearest, und der "
                             f"Accuracy-Check rechnet Uebereinstimmungsquote "
                             f"+ Cohen's Kappa statt MAE/RMSE. Referenz- und "
                             f"Test-Run muessen dasselbe Paar haben - der "
                             f"Accuracy-Check waehlt die Referenz danach aus.")
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
                             "sind ueber alle Varianten identisch. "
                             "Wird bei --dem-format!=gtiff ignoriert.")
    parser.add_argument("--dem-format", default="gtiff",
                        choices=DEM_FORMATS,
                        help="DATEIFORMAT des reprojizierten DEM-Assets "
                             "(nur local_preprocessing). gtiff (Default): "
                             "GeoTIFF wie bisher, kombinierbar mit --dem-layout. "
                             "zarr: xarray-Zarr-Verzeichnis-Store (braucht "
                             "'pip install xarray zarr'). "
                             "netcdf: xarray-NetCDF-4 Datei (braucht "
                             "'pip install xarray netcdf4'). "
                             "Machbarkeitstest ob CDSE ueber load_stac andere "
                             "Formate als GeoTIFF akzeptiert - kann vom Backend "
                             "abgelehnt werden.")
    parser.add_argument("--dem-tiles", type=int, default=1,
                        help="(nur local_preprocessing, nur "
                             "--dem-format=gtiff) Anzahl raeumlicher "
                             "Kacheln, in die das reprojizierte DEM zerlegt "
                             "wird (Default 1 = bisheriges Verhalten, 4 = "
                             "2x2-Raster). Bei N>1 wird jede Kachel als "
                             "eigene GeoTIFF-Datei hochgeladen und als "
                             "EIGENES STAC-Item (je ein Asset, eigene "
                             "proj-Felder) in einer Collection verlinkt; "
                             "load_stac zeigt auf die Collection. Mehrere "
                             "Assets in EINEM Item gehen nicht: der "
                             "geopyspark-Treiber laedt pro Item und "
                             "Bandnamen nur das erste Asset, mosaikiert "
                             "wird nur ueber Items. Die Kacheln stammen "
                             "aus demselben reprojizierten Puffer (reines "
                             "Slicing, kein zweiter Warp), ihre "
                             "Vereinigung ist bitgenau das Einzel-DEM "
                             "(lokale Pflicht-Verifikation vor Upload). "
                             "Experiment: laedt CDSE die Kacheln parallel? "
                             "Das Datenvolumen sinkt durch die Zerlegung "
                             "NICHT - nur Parallelitaet kann Zeit sparen.")
    parser.add_argument("--zarr-via-item", action="store_true",
                        help="Nur --dem-format=zarr: load_stac zeigt auf die "
                             "STAC-ITEM-URL statt auf die Collection-URL. "
                             "Die Collection wird weiterhin erzeugt und "
                             "hochgeladen, sie wird nur nicht mehr "
                             "referenziert. Hintergrund: seit der "
                             "shape-Injektion in die .zmetadata scheitert "
                             "der zarr-Lauf nicht mehr am Zarr-Parser, "
                             "sondern im Collection-Lesepfad "
                             "(from_stac_catalog liefert 'Collected 0 "
                             "projection metadata entries from 1 items', "
                             "danach 'NoneType' object has no attribute "
                             "'crs'). netcdf laeuft ueber die Item-URL "
                             "erfolgreich - dieses Flag testet, ob der "
                             "Collection-Pfad der Blocker ist und nicht das "
                             "Format. Auf --dem-tiles>1 hat das Flag keine "
                             "Wirkung: dort IST die Collection die "
                             "Mosaik-Struktur. Default AUS (bisheriges "
                             "Verhalten).")
    parser.add_argument("--snap-dem-to-s2", action="store_true",
                        help="(nur local_preprocessing) DEM pixelgenau auf "
                             "das erwartete CDSE/S2-Zielgitter bringen: "
                             "Ziel-Grid wird aus dem angefragten Extent "
                             "abgeleitet (nach UTM projiziert, Kanten "
                             "outward auf 10 m); die Reprojektion laeuft "
                             "unveraendert, danach wird der Puffer per "
                             "reinem Slicing auf dieses Grid GECROPPT - "
                             "die hochgeladenen Pixelwerte sind bitgenau "
                             "dieselben wie ohne Flag, nur der Extent "
                             "unterscheidet sich. Eliminiert die Extent-"
                             "Differenz zum CDSE-Ergebnis-Grid, sodass "
                             "load_stac nichts mehr zuschneiden/resampeln "
                             "muss. Default AUS (bisheriges Verhalten), "
                             "damit Laeufe MIT und OHNE Snapping "
                             "vergleichbar sind. Inklusive lokaler Pflicht-"
                             "Verifikation (Grid-Check + Crop-Identitaet). "
                             "Wird bei Nicht-UTM --target-crs ignoriert.")
    parser.add_argument("--target-crs", default=None,
                        help="Ziel-CRS fuer die lokale DEM-Reprojektion. "
                             "local_preprocessing: Default = UTM-EPSG der Region "
                             "+ 10 m S2-Grid-Snap. full_preprocessing: Default "
                             "= DEM wird exakt auf das S2-Grid (UTM) gesnapped. "
                             "Override z.B. EPSG:3035 (LAEA) oder EPSG:4326 "
                             "(WGS84) erzwingt eine echte CRS-Transformation "
                             "CDSE-seitig beim merge_cubes.")
    parser.add_argument("--resample-s2-to-dem", action="store_true",
                        help="Nur local_preprocessing: umgekehrte Gitter-"
                             "Hoheit. S2 wird VOR merge_cubes per "
                             "resample_cube_spatial(data=S2, target=DEM, "
                             "method=near) auf das Gitter des per load_stac "
                             "geladenen DEM ausgerichtet, statt dass CDSE "
                             "das DEM auf sein S2-abgeleitetes Zielgitter "
                             "zwingt (zweites serverseitiges Resampling). "
                             "NUR S2 wird resampled, das DEM laeuft durch "
                             "kein Resample. Ob CDSE das DEM-Gitter wirklich "
                             "uebernimmt, zeigt der Ursprung des Ergebnis-"
                             "Grids im Serverlauf. Default AUS.")
    parser.add_argument("--force-target-crs", action="store_true",
                        help="Nur onthefly: explizites Ziel-CRS (primaere "
                             "UTM-Zone der Region) auch dann in den Graphen "
                             "setzen, wenn der Extent nur EINE UTM-Zone "
                             "beruehrt. Bei Extents ueber einer Zonengrenze "
                             "(z.B. berlin xxlarge) passiert das automatisch, "
                             "weil CDSE sonst am Multi-CRS-Input scheitert "
                             "('no target CRS specified, but multiple CRSes "
                             "across input'). Achtung: die erzwungene "
                             "Projektion ueber die Zonengrenze verzerrt "
                             "zonenfremde Daten zunehmend mit dem Abstand "
                             "zur Zielzone - das ist der Messgegenstand, "
                             "kein Neutralum.")
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
    parser.add_argument("--fullpp-upload-profile", default="simple_striped",
                        choices=_REWRITE_PROFILES,
                        help="Schreibprofil fuer die S2- und DEM-Uploads bei "
                             "full_preprocessing. simple_striped (Default, "
                             "NEUER Bugfix): gestreiftes, unkomprimiertes "
                             "GeoTIFF - identisch zu dem was local_pp fuer "
                             "das DEM benutzt und was CDSE nachweislich sauber "
                             "liest. tiled_deflate (alter Default): tiled "
                             "256x256 mit deflate, wahrscheinlichere Ursache "
                             "der beobachteten CDSE-Output-Korruption bei "
                             "full_pp. Nur zur Regressions-Diagnose.")
    parser.add_argument("--fullpp-save-format", default="GTiff",
                        choices=("GTiff", "netCDF"),
                        help="save_result Format des CDSE-Jobs bei "
                             "full_preprocessing. Default GTiff. netCDF ist "
                             "als Diagnose-Alternative gedacht, um zu pruefen "
                             "ob die beobachtete Output-Korruption GTiff-"
                             "spezifisch beim CDSE-Writer ist.")
    parser.add_argument("--include-full-pp", default="auto",
                        choices=("auto", "yes", "no"),
                        help="Steuert ob full_preprocessing bei "
                             "--strategy all mitlaeuft. auto (Default): "
                             "ja bei extent in {small,medium,large}, nein "
                             "bei {xlarge,xxlarge} (zu viele Range-Requests "
                             "-> Timeouts). yes: immer einbeziehen. "
                             "no: nie einbeziehen.")
    parser.add_argument("--min-free-gb", type=float, default=20.0,
                        help="Minimaler freier Plattenplatz (in GB) im "
                             "--output-dir, unterhalb dessen ein Run gar nicht "
                             "erst startet. Default 20 GB. 0 deaktiviert die "
                             "Pruefung. Faengt das '100%%-Fuell-Fiasko' ab.")
    parser.add_argument("--cleanup-after-accuracy", action="store_true",
                        help="Nach jedem erfolgreich verbuchten Accuracy-Check "
                             "die TIF-Ausgaben des Runs loeschen. "
                             "results.json, Prozessgraphen und Metadaten "
                             "bleiben erhalten. Reihenfolge ist zwingend: "
                             "erst Run, dann Accuracy-Eintrag, dann Loeschen. "
                             "local_reference wird erst geloescht wenn alle "
                             "abhaengigen CDSE-Runs (gleiche Region/Extent/"
                             "Workflow) einen Accuracy-Eintrag haben. "
                             "Default: aus (bisheriges Verhalten).")
    parser.add_argument("--dry-run-cleanup", action="store_true",
                        help="Zeigt beim Aufraeumen nur an was geloescht "
                             "WUERDE, ohne wirklich zu loeschen. Impliziert "
                             "--cleanup-after-accuracy nicht - beides muss "
                             "explizit gesetzt sein. Nuetzlich um vor dem "
                             "ersten Live-Lauf die Loeschliste zu pruefen.")

    args = parser.parse_args()

    # Datensatz-Paar aufloesen und pruefen, BEVOR irgendetwas laeuft.
    # Workflow-Default kommt vom Datensatz, damit '--dataset landcover'
    # allein schon funktioniert; bei 'dem' ist das Ergebnis merge_add,
    # also unveraendert.
    dataset = _dataset_of(args)
    if args.workflow is None:
        args.workflow = DATASETS[dataset]["default_workflow"]
    try:
        _validate_dataset_workflow(dataset, args.workflow)
        _validate_dataset_resampling(dataset, args.local_resampling)
    except ValueError as exc:
        parser.error(str(exc))

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
            ctx = (f"Strategie={strategy}, Run {i+1}/{args.repeat}, "
                   f"Region={args.region}, Extent={args.extent_size}, "
                   f"Workflow={args.workflow}")
            try:
                check_disk_space(args.output_dir, args.min_free_gb, context=ctx)
            except RuntimeError as exc:
                print(f"\n  FEHLER: {exc}")
                all_results.append({
                    "strategy": strategy,
                    "repeat": i + 1,
                    "run_type": _run_type_for(i, args.run_type),
                    "status": "aborted_disk_full",
                    "preprocessing_time": None,
                    "total_time": None,
                    "run_id": None,
                    "outdir": None,
                    "region": args.region,
                    "extent_size": args.extent_size,
                    "workflow": args.workflow,
                    "error": str(exc),
                })
                # Kein weiterer Run wenn die Platte schon jetzt zu voll ist.
                break
            result = runners[strategy](args, i)
            # Cleanup-Orchestrator braucht Region/Extent/Workflow um die
            # local_reference-Abhaengigkeiten aufzuloesen.
            result.setdefault("region", args.region)
            result.setdefault("extent_size", args.extent_size)
            result.setdefault("workflow", args.workflow)
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
                           reference_strategy="onthefly",
                           resolution=_resolution_of(args),
                           dataset=dataset)

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
        resolution = _resolution_of(args)
        for s in candidate_strategies:
            suf, _ = _ACCURACY_LAYOUT[s]
            if _find_latest_run_dir(args.output_dir, suf, args.region,
                                    extent_size=args.extent_size,
                                    workflow=args.workflow,
                                    resolution=resolution,
                                    dataset=dataset) is None:
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
                resolution=resolution,
                dataset=dataset,
            )
        if not any_run:
            print("\n[--reference-check] Keine CDSE-Strategie-Runs gefunden "
                  "die gegen local_reference verglichen werden koennten.")

    # Aufraeumen erst NACH allen Accuracy-Checks - sonst waeren die TIFs
    # bereits geloescht bevor die Accuracy sie gelesen hat.
    if args.cleanup_after_accuracy or args.dry_run_cleanup:
        cleanup_after_accuracy(all_results, output_dir=args.output_dir,
                               dry_run=args.dry_run_cleanup)


if __name__ == "__main__":
    main()

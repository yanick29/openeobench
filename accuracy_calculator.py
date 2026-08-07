"""
CRS Transformation Accuracy Calculator
======================================
Calculates RMSE and MAE between reference and roundtrip-transformed rasters.

Usage:
    python accuracy_calculator.py <reference.tif> <test.tif> [--output results.json]

Example:
    python accuracy_calculator.py outputs/reference/openEO.tif outputs/roundtrip/openEO.tif
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb
import numpy as np

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject, calculate_default_transform
except ImportError:
    print("Error: rasterio is required. Install with: pip install rasterio")
    sys.exit(1)

DEFAULT_DB_PATH = "benchmark_results.duckdb"


def load_raster(filepath):
    """Load raster and return data array and metadata."""
    with rasterio.open(filepath) as src:
        data = src.read()
        profile = src.profile
        bounds = src.bounds
        crs = src.crs
        transform = src.transform
    return data, profile, bounds, crs, transform


RESAMPLING_METHODS = {
    "nearest":  Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic":    Resampling.cubic,
}


def _resolve_resampling(resampling_method):
    """Akzeptiert einen String oder ein Resampling-Enum und liefert Enum zurueck."""
    if isinstance(resampling_method, str):
        if resampling_method not in RESAMPLING_METHODS:
            raise ValueError(
                f"Unbekannte resampling_method '{resampling_method}'. "
                f"Erlaubt: {sorted(RESAMPLING_METHODS)}"
            )
        return RESAMPLING_METHODS[resampling_method]
    return resampling_method


def align_rasters(ref_path, test_path, resampling_method="nearest"):
    """
    Align test raster to reference raster grid.

    resampling_method: 'nearest' (Default), 'bilinear' oder 'cubic' — oder ein
    rasterio Resampling-Enum. Die Methode wird genau dann verwendet, wenn
    tatsaechlich resamplet werden muss. Wenn beide Raster bereits
    pixelidentisch sind (gleiche Shape, CRS und Transform), wird ein
    Early-Exit ausgefuehrt: die Test-Daten werden 1:1 zurueckgegeben, ohne
    jegliches Resampling. Dadurch entstehen keine Snap-Artefakte, die einen
    echten Pixel-Unterschied "wegresamplen" koennten.

    Returns aligned numpy arrays for both rasters und das Referenz-Profile.
    """
    resampling = _resolve_resampling(resampling_method)

    with rasterio.open(ref_path) as ref_src:
        ref_data = ref_src.read()
        ref_profile = ref_src.profile
        ref_crs = ref_src.crs
        ref_transform = ref_src.transform

    with rasterio.open(test_path) as test_src:
        test_crs = test_src.crs
        test_transform = test_src.transform
        test_shape = (test_src.count, test_src.height, test_src.width)

        ref_count = ref_data.shape[0]
        n_bands = min(ref_count, test_src.count)
        if ref_count != test_src.count:
            print(f"Warning: Bandanzahl unterschiedlich "
                  f"(reference={ref_count}, test={test_src.count}). "
                  f"Vergleiche nur die ersten {n_bands} Band(s).")

        # Early-Exit: identische Grids -> kein Resampling, exakte Werte.
        identical_grid = (
            test_crs == ref_crs
            and test_shape[1] == ref_data.shape[1]
            and test_shape[2] == ref_data.shape[2]
            and test_transform == ref_transform
        )
        if identical_grid:
            test_aligned = test_src.read(
                indexes=list(range(1, n_bands + 1))
            ).astype(np.float64)
            if n_bands < ref_count:
                pad = np.zeros(
                    (ref_count - n_bands,) + test_aligned.shape[1:],
                    dtype=np.float64,
                )
                test_aligned = np.concatenate([test_aligned, pad], axis=0)
            return ref_data, test_aligned, ref_profile

        # Sonst: auf Referenz-Grid reprojizieren mit der gewaehlten Methode.
        test_aligned = np.zeros(ref_data.shape, dtype=np.float64)
        for band_idx in range(n_bands):
            reproject(
                source=rasterio.band(test_src, band_idx + 1),
                destination=test_aligned[band_idx],
                src_transform=test_transform,
                src_crs=test_crs,
                dst_transform=ref_transform,
                dst_crs=ref_crs,
                resampling=resampling,
            )

    return ref_data, test_aligned, ref_profile


def calculate_metrics(reference, test, nodata=None, exclude_zeros=False):
    """
    Calculate accuracy metrics between reference and test arrays.

    `exclude_zeros=True` behandelt zusaetzlich Pixel mit Wert 0 als nodata
    (typisch fuer Sentinel-2). Default False -> 0-Pixel zaehlen mit.

    Returns:
        dict with RMSE, MAE, and other statistics per band
    """
    results = {
        "bands": [],
        "overall": {}
    }
    
    all_diffs = []
    
    for band_idx in range(reference.shape[0]):
        ref_band = reference[band_idx].astype(np.float64)
        test_band = test[band_idx].astype(np.float64)
        
        # Create valid mask (exclude nodata and zeros)
        if nodata is not None:
            valid_mask = (ref_band != nodata) & (test_band != nodata)
        else:
            valid_mask = np.isfinite(ref_band) & np.isfinite(test_band)
        
        # Optional: zero values als nodata behandeln (typisch fuer Sentinel-2)
        if exclude_zeros:
            valid_mask = valid_mask & (ref_band != 0) & (test_band != 0)
        
        if valid_mask.sum() == 0:
            print(f"Warning: Band {band_idx + 1} has no valid pixels for comparison")
            continue
        
        ref_valid = ref_band[valid_mask]
        test_valid = test_band[valid_mask]
        
        # Calculate differences
        diff = test_valid - ref_valid
        all_diffs.extend(diff.tolist())
        
        # Calculate metrics
        rmse = np.sqrt(np.mean(diff ** 2))
        mae = np.mean(np.abs(diff))
        me = np.mean(diff)  # Mean Error (bias)
        
        # Relative metrics (as percentage of reference mean)
        ref_mean = np.mean(ref_valid)
        rmse_rel = (rmse / ref_mean) * 100 if ref_mean != 0 else np.nan
        mae_rel = (mae / ref_mean) * 100 if ref_mean != 0 else np.nan
        
        # Correlation
        if np.std(ref_valid) > 0 and np.std(test_valid) > 0:
            correlation = np.corrcoef(ref_valid, test_valid)[0, 1]
        else:
            correlation = np.nan
        
        band_result = {
            "band": band_idx + 1,
            "valid_pixels": int(valid_mask.sum()),
            "total_pixels": int(valid_mask.size),
            "coverage_percent": float(valid_mask.sum() / valid_mask.size * 100),
            "reference_mean": float(ref_mean),
            "reference_std": float(np.std(ref_valid)),
            "test_mean": float(np.mean(test_valid)),
            "test_std": float(np.std(test_valid)),
            "RMSE": float(rmse),
            "RMSE_relative_percent": float(rmse_rel),
            "MAE": float(mae),
            "MAE_relative_percent": float(mae_rel),
            "ME_bias": float(me),
            "correlation": float(correlation),
            "min_diff": float(np.min(diff)),
            "max_diff": float(np.max(diff))
        }
        
        results["bands"].append(band_result)
    
    # Calculate overall metrics
    if all_diffs:
        all_diffs = np.array(all_diffs)
        results["overall"] = {
            "total_valid_pixels": sum(b["valid_pixels"] for b in results["bands"]),
            "RMSE": float(np.sqrt(np.mean(all_diffs ** 2))),
            "MAE": float(np.mean(np.abs(all_diffs))),
            "ME_bias": float(np.mean(all_diffs))
        }
    
    return results


def calculate_categorical_metrics(reference, test, nodata=None,
                                  validity_only=False):
    """Vergleichsmetriken fuer KATEGORIALE Raster (Klassen-IDs).

    MAE/RMSE sind hier bedeutungslos: der Abstand zwischen Klasse 10 (Baum)
    und Klasse 50 (bebaut) ist keine 40. Stattdessen:

      overall_accuracy - Anteil pixelweise uebereinstimmender Klassen. Die
                         Kernzahl.
      kappa            - Cohen's Kappa, korrigiert um zufaellige
                         Uebereinstimmung. Noetig, weil Landbedeckung immer
                         stark ungleich verteilt ist: besteht ein Ausschnitt
                         zu 95% aus einer Klasse, sieht die reine Quote auch
                         dann gut aus, wenn jede Klassengrenze verschoben
                         ist.
      confusion        - vollstaendige Verwechslungsmatrix
                         {ref_klasse: {test_klasse: n}} - zeigt, WELCHE
                         Klassen ineinander laufen (typisch: benachbarte
                         Klassen an den Kanten).
      per_class        - je Referenzklasse Anzahl und Trefferquote.

    validity_only=True: nicht die Klassen selbst werden verglichen, sondern
    nur GUELTIG vs NODATA (2 Klassen). Fuer lc_mask - dort steckt die
    Aussage in der Maskenkante, die Werte innerhalb der Maske sind ohnehin
    identisch, und ein Vergleich der Werte wuerde die Kante ausblenden.
    """
    ref = np.asarray(reference)
    tst = np.asarray(test)
    if ref.ndim == 3:
        ref = ref[0]
    if tst.ndim == 3:
        tst = tst[0]

    if validity_only:
        # NaN und (falls angegeben) nodata gelten als ungueltig.
        ref_valid = np.isfinite(ref)
        tst_valid = np.isfinite(tst)
        if nodata is not None:
            ref_valid &= (ref != nodata)
            tst_valid &= (tst != nodata)
        ref_cls = ref_valid.astype(np.int64)
        tst_cls = tst_valid.astype(np.int64)
        mask = np.ones(ref_cls.shape, dtype=bool)
    else:
        ref_cls = ref.astype(np.int64)
        tst_cls = tst.astype(np.int64)
        mask = np.isfinite(ref.astype(np.float64)) & np.isfinite(
            tst.astype(np.float64))
        if nodata is not None:
            mask &= (ref_cls != nodata) & (tst_cls != nodata)

    total_px = int(mask.size)
    valid_px = int(mask.sum())
    if valid_px == 0:
        return {
            "overall_accuracy": None, "kappa": None, "confusion": {},
            "per_class": {}, "valid_pixels": 0, "total_pixels": total_px,
            "agreeing_pixels": 0,
        }

    r = ref_cls[mask]
    t = tst_cls[mask]
    agree = int((r == t).sum())
    overall = agree / valid_px

    classes = np.union1d(np.unique(r), np.unique(t))
    confusion, per_class = {}, {}
    for c in classes:
        sel = (r == c)
        n_c = int(sel.sum())
        if n_c:
            hits = int((t[sel] == c).sum())
            per_class[int(c)] = {
                "n_reference": n_c,
                "n_correct": hits,
                "accuracy": hits / n_c,
            }
            row = {}
            for c2 in np.unique(t[sel]):
                row[int(c2)] = int((t[sel] == c2).sum())
            confusion[int(c)] = row

    # Cohen's Kappa: (p_o - p_e) / (1 - p_e)
    p_e = 0.0
    for c in classes:
        p_e += (float((r == c).sum()) / valid_px) * \
               (float((t == c).sum()) / valid_px)
    kappa = (overall - p_e) / (1.0 - p_e) if (1.0 - p_e) > 1e-12 else None

    return {
        "overall_accuracy": float(overall),
        "kappa": float(kappa) if kappa is not None else None,
        "confusion": confusion,
        "per_class": per_class,
        "valid_pixels": valid_px,
        "total_pixels": total_px,
        "agreeing_pixels": agree,
    }


def print_results(results, ref_path, test_path):
    """Pretty print the results."""
    print("\n" + "=" * 60)
    print("CRS TRANSFORMATION ACCURACY RESULTS")
    print("=" * 60)
    print(f"Reference: {ref_path}")
    print(f"Test:      {test_path}")
    print("-" * 60)
    
    for band in results["bands"]:
        print(f"\nBand {band['band']}:")
        print(f"  Valid pixels:     {band['valid_pixels']:,} ({band['coverage_percent']:.1f}%)")
        print(f"  Reference mean:   {band['reference_mean']:.2f}")
        print(f"  Test mean:        {band['test_mean']:.2f}")
        print(f"  RMSE:             {band['RMSE']:.4f} ({band['RMSE_relative_percent']:.2f}%)")
        print(f"  MAE:              {band['MAE']:.4f} ({band['MAE_relative_percent']:.2f}%)")
        print(f"  Bias (ME):        {band['ME_bias']:.4f}")
        print(f"  Correlation:      {band['correlation']:.6f}")
    
    if results["overall"]:
        print("\n" + "-" * 60)
        print("OVERALL:")
        print(f"  Total pixels:     {results['overall']['total_valid_pixels']:,}")
        print(f"  RMSE:             {results['overall']['RMSE']:.4f}")
        print(f"  MAE:              {results['overall']['MAE']:.4f}")
        print(f"  Bias (ME):        {results['overall']['ME_bias']:.4f}")
    
    print("=" * 60 + "\n")


def _ensure_accuracy_schema(conn):
    """Stellt sicher, dass die accuracy-Tabelle existiert und die neuen Spalten hat."""
    conn.execute('''CREATE TABLE IF NOT EXISTS accuracy (
        accuracy_id INTEGER PRIMARY KEY,
        run_id INTEGER,
        reference_file TEXT,
        rmse DOUBLE,
        max_diff DOUBLE,
        mean_diff DOUBLE,
        mae DOUBLE,
        correlation DOUBLE
    )''')
    existing = {r[1] for r in conn.execute("PRAGMA table_info('accuracy')").fetchall()}
    for col in ("mae", "correlation"):
        if col not in existing:
            conn.execute(f"ALTER TABLE accuracy ADD COLUMN {col} DOUBLE")


def save_to_db(results, run_id, db_path=DEFAULT_DB_PATH, reference_file=None):
    """Schreibt die Overall-Accuracy-Metriken in die accuracy-Tabelle."""
    overall = results.get("overall") or {}
    if not overall:
        print("Warning: No overall metrics to save (no valid pixels?).")
        return None

    # Korrelation und max_diff aus den Bands aggregieren (overall hat sie nicht)
    bands = results.get("bands") or []
    correlations = [b["correlation"] for b in bands
                    if b.get("correlation") is not None and np.isfinite(b["correlation"])]
    correlation = float(np.mean(correlations)) if correlations else None
    max_diffs = [b["max_diff"] for b in bands if b.get("max_diff") is not None]
    max_diff = float(max(max_diffs, key=abs)) if max_diffs else None

    conn = duckdb.connect(db_path)
    try:
        _ensure_accuracy_schema(conn)
        next_id = conn.execute(
            "SELECT COALESCE(MAX(accuracy_id), 0) + 1 FROM accuracy"
        ).fetchone()[0]

        conn.execute(
            '''INSERT INTO accuracy
               (accuracy_id, run_id, reference_file, rmse, max_diff, mean_diff, mae, correlation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                next_id,
                run_id,
                str(reference_file) if reference_file is not None else None,
                overall.get("RMSE"),
                max_diff,
                overall.get("ME_bias"),
                overall.get("MAE"),
                correlation,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Accuracy saved to DB (accuracy_id={next_id}, run_id={run_id})")
    return next_id


def main():
    parser = argparse.ArgumentParser(
        description="Calculate RMSE and MAE between reference and test rasters"
    )
    parser.add_argument("reference", help="Path to reference raster (original)")
    parser.add_argument("test", help="Path to test raster (roundtrip transformed)")
    parser.add_argument("--output", "-o", help="Output JSON file for results")
    parser.add_argument("--nodata", type=float, help="NoData value to exclude")
    parser.add_argument("--exclude-zeros", action="store_true",
                        help="Pixel mit Wert 0 als nodata behandeln "
                             "(typisch fuer Sentinel-2). Default: False.")
    parser.add_argument("--run-id", type=int, default=None,
                        help="Run-ID in der benchmark DB (Pflicht bei --save-db)")
    parser.add_argument("--save-db", action="store_true",
                        help="Ergebnisse in die accuracy-Tabelle der DuckDB schreiben")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"Pfad zur DuckDB (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--resampling-method", default="nearest",
                        choices=tuple(RESAMPLING_METHODS.keys()),
                        help="Resampling-Methode fuer das Test->Referenz "
                             "Alignment (nur wenn Grids nicht bereits "
                             "identisch sind). Default: nearest.")

    args = parser.parse_args()

    if args.save_db and args.run_id is None:
        parser.error("--save-db erfordert --run-id")
    
    # Check files exist
    ref_path = Path(args.reference)
    test_path = Path(args.test)
    
    if not ref_path.exists():
        print(f"Error: Reference file not found: {ref_path}")
        sys.exit(1)
    
    if not test_path.exists():
        print(f"Error: Test file not found: {test_path}")
        sys.exit(1)
    
    print(f"Loading and aligning rasters...")
    
    # Align rasters
    ref_data, test_data, profile = align_rasters(
        ref_path, test_path, resampling_method=args.resampling_method,
    )
    
    print(f"Reference shape: {ref_data.shape}")
    print(f"Test shape:      {test_data.shape}")
    
    # Calculate metrics
    print("Calculating accuracy metrics...")
    results = calculate_metrics(ref_data, test_data, args.nodata,
                                exclude_zeros=args.exclude_zeros)
    
    # Add metadata
    results["metadata"] = {
        "reference_file": str(ref_path),
        "test_file": str(test_path),
        "reference_shape": list(ref_data.shape),
        "test_shape": list(test_data.shape)
    }
    
    # Print results
    print_results(results, ref_path, test_path)
    
    # Save to JSON if requested
    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to: {output_path}")

    if args.save_db:
        save_to_db(results, args.run_id, db_path=args.db, reference_file=ref_path)

    return results


if __name__ == "__main__":
    main()

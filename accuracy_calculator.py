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

import numpy as np

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject, calculate_default_transform
except ImportError:
    print("Error: rasterio is required. Install with: pip install rasterio")
    sys.exit(1)


def load_raster(filepath):
    """Load raster and return data array and metadata."""
    with rasterio.open(filepath) as src:
        data = src.read()
        profile = src.profile
        bounds = src.bounds
        crs = src.crs
        transform = src.transform
    return data, profile, bounds, crs, transform


def align_rasters(ref_path, test_path):
    """
    Align test raster to reference raster grid.
    Returns aligned numpy arrays for both.
    """
    with rasterio.open(ref_path) as ref_src:
        ref_data = ref_src.read()
        ref_profile = ref_src.profile
        ref_crs = ref_src.crs
        ref_transform = ref_src.transform
        ref_width = ref_src.width
        ref_height = ref_src.height
        ref_bounds = ref_src.bounds
    
    with rasterio.open(test_path) as test_src:
        test_crs = test_src.crs
        
        # If CRS matches, reproject test to reference grid
        if test_crs == ref_crs:
            # Same CRS - just need to align grids
            test_aligned = np.zeros_like(ref_data)
            
            for band_idx in range(test_src.count):
                reproject(
                    source=rasterio.band(test_src, band_idx + 1),
                    destination=test_aligned[band_idx],
                    src_transform=test_src.transform,
                    src_crs=test_crs,
                    dst_transform=ref_transform,
                    dst_crs=ref_crs,
                    resampling=Resampling.nearest
                )
        else:
            # Different CRS - need full reprojection
            test_aligned = np.zeros_like(ref_data)
            
            for band_idx in range(test_src.count):
                reproject(
                    source=rasterio.band(test_src, band_idx + 1),
                    destination=test_aligned[band_idx],
                    src_transform=test_src.transform,
                    src_crs=test_crs,
                    dst_transform=ref_transform,
                    dst_crs=ref_crs,
                    resampling=Resampling.nearest
                )
    
    return ref_data, test_aligned, ref_profile


def calculate_metrics(reference, test, nodata=None):
    """
    Calculate accuracy metrics between reference and test arrays.
    
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
        
        # Also exclude zero values (often nodata in Sentinel-2)
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


def main():
    parser = argparse.ArgumentParser(
        description="Calculate RMSE and MAE between reference and test rasters"
    )
    parser.add_argument("reference", help="Path to reference raster (original)")
    parser.add_argument("test", help="Path to test raster (roundtrip transformed)")
    parser.add_argument("--output", "-o", help="Output JSON file for results")
    parser.add_argument("--nodata", type=float, help="NoData value to exclude")
    
    args = parser.parse_args()
    
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
    ref_data, test_data, profile = align_rasters(ref_path, test_path)
    
    print(f"Reference shape: {ref_data.shape}")
    print(f"Test shape:      {test_data.shape}")
    
    # Calculate metrics
    print("Calculating accuracy metrics...")
    results = calculate_metrics(ref_data, test_data, args.nodata)
    
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
    
    return results


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
test_dem_layout.py - Standalone-Verifikation fuer das --dem-layout Experiment.

Erzeugt ein synthetisches Ausgangs-DEM (int16-Gradient in EPSG:4326),
reprojiziert es via run_benchmark.reproject_dem_local in alle drei Layout-
Varianten (striped / tiled_uncompressed / cog) mit ansonsten identischen
Parametern und prueft:

  1. Jede Datei hat das erwartete Layout (tiled, blocksize, compression,
     overviews) - via rasterio.
  2. Die Rohpixelwerte sind pixel-identisch ueber alle drei Varianten
     (np.array_equal + SHA-256 des Rohpuffers).

Aufruf:
    venv312\\Scripts\\python.exe test_dem_layout.py
"""
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

sys.path.insert(0, str(Path(__file__).parent))
from run_benchmark import (
    DEM_LAYOUTS,
    reproject_dem_local,
    _inspect_tif_layout,
    _log_tif_layout,
)


def _make_synthetic_dem(path: Path) -> None:
    """Deterministisches int16 GeoTIFF in EPSG:4326 (grob Berlin)."""
    width, height = 1500, 1500
    ys = np.arange(height, dtype=np.int32).reshape(-1, 1)
    xs = np.arange(width, dtype=np.int32).reshape(1, -1)
    data = ((xs + 2 * ys) % 32000).astype(np.int16)
    transform = from_bounds(13.30, 52.45, 13.45, 52.55, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "int16",
        "count": 1,
        "width": width,
        "height": height,
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -32768,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _expected_layout(layout: str) -> dict:
    if layout == "striped":
        return {"tiled": False, "blockxsize_expected": None,
                "compress_expected": None, "overviews_expected": 0}
    if layout == "tiled_uncompressed":
        return {"tiled": True, "blockxsize_expected": 128,
                "compress_expected": None, "overviews_expected": 0}
    if layout == "cog":
        return {"tiled": True, "blockxsize_expected": 128,
                "compress_expected": "deflate", "overviews_expected_min": 1}
    raise ValueError(layout)


def _read_pixel_array(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        return src.read()


def _sha256_of_array(arr: np.ndarray) -> str:
    contig = np.ascontiguousarray(arr)
    return hashlib.sha256(contig.tobytes()).hexdigest()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dem_layout_test_"))
    print(f"[test] tmp dir: {tmp}")
    src_tif = tmp / "src_dem.tif"
    _make_synthetic_dem(src_tif)
    print(f"[test] synthetisches DEM erzeugt: {src_tif} "
          f"({src_tif.stat().st_size / 1024:.1f} KB)")

    outputs = {}
    for layout in DEM_LAYOUTS:
        out = tmp / f"reproj_{layout}.tif"
        elapsed = reproject_dem_local(
            str(src_tif), str(out),
            dst_crs="EPSG:32633",
            resampling="nearest",
            layout=layout,
        )
        outputs[layout] = out
        info = _inspect_tif_layout(str(out))
        print()
        print(f"[test] layout='{layout}'  (reproject_dem_local: {elapsed:.3f} s)")
        _log_tif_layout(info)

        exp = _expected_layout(layout)
        assert info["tiled"] == exp["tiled"], (
            f"{layout}: tiled={info['tiled']}, erwartet {exp['tiled']}"
        )
        if exp["tiled"]:
            assert info["blockxsize"] == exp["blockxsize_expected"], (
                f"{layout}: blockxsize={info['blockxsize']}, "
                f"erwartet {exp['blockxsize_expected']}"
            )
            assert info["blockysize"] == exp["blockxsize_expected"], (
                f"{layout}: blockysize={info['blockysize']}"
            )
        expected_compress = exp["compress_expected"]
        actual_compress = (info["compress"] or None)
        if isinstance(actual_compress, str):
            actual_compress = actual_compress.lower()
        assert actual_compress == expected_compress, (
            f"{layout}: compress={actual_compress}, erwartet {expected_compress}"
        )
        if "overviews_expected" in exp:
            assert info["num_overviews"] == exp["overviews_expected"], (
                f"{layout}: {info['num_overviews']} overviews, "
                f"erwartet {exp['overviews_expected']}"
            )
        else:
            assert info["num_overviews"] >= exp["overviews_expected_min"], (
                f"{layout}: {info['num_overviews']} overviews, "
                f"erwartet >= {exp['overviews_expected_min']}"
            )

    print()
    print("[test] Pixel-Gleichheit ueber alle Layouts")
    ref_layout = DEM_LAYOUTS[0]
    ref_array = _read_pixel_array(outputs[ref_layout])
    ref_hash = _sha256_of_array(ref_array)
    print(f"  {ref_layout:20s}  sha256={ref_hash}  shape={ref_array.shape}")

    all_equal = True
    for layout in DEM_LAYOUTS[1:]:
        arr = _read_pixel_array(outputs[layout])
        h = _sha256_of_array(arr)
        equal = np.array_equal(arr, ref_array)
        marker = "OK" if equal else "MISMATCH"
        print(f"  {layout:20s}  sha256={h}  array_equal={equal}  [{marker}]")
        if not equal:
            all_equal = False

    print()
    if all_equal:
        print("[test] Alle drei Layout-Varianten pixel-identisch.")
        print(f"[test] Layout-Verifikation OK.")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
    else:
        print("[test] FEHLER: mindestens eine Variante weicht ab.")
        print(f"[test] Debug-Dateien bleiben in {tmp}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
test_dem_format.py

Standalone-Verifikation fuer das --dem-format Experiment (nur lokal, ohne
CDSE). Reprojiziert ein synthetisches DEM in einen In-Memory Puffer und
schreibt daraus DREI Assets:

  1. GeoTIFF (Layout striped)          .tif
  2. Zarr-Verzeichnis-Store (xarray)   .zarr/
  3. NetCDF-4 Datei (xarray)           .nc

Danach werden die drei Assets EINZELN wieder eingelesen und verglichen:
  - Pixelwerte per np.array_equal
  - SHA-256 des Rohpuffers
  - Georeferenz (CRS-Rekonstruktion + affine Transform aus Zarr/NetCDF)

Zeigt zusaetzlich die STAC-Item media_types die build_stac_item pro Format
liefert (damit im Log dokumentiert ist welchen Typ CDSE spaeter zu sehen
bekommt).
"""
from __future__ import annotations

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
    DEM_FORMATS,
    _DEM_FORMAT_EXT,
    _DEM_FORMAT_MEDIA_TYPE,
    _check_dem_format_deps,
    _reproject_dem_to_array,
    _write_dem_with_layout,
    _write_dem_as_zarr,
    _write_dem_as_netcdf,
    _inspect_asset_size,
    build_stac_item,
)


def _make_synthetic_dem(path: Path) -> None:
    """Deterministisches int16 GeoTIFF (EPSG:4326, ca. Berlin)."""
    width, height = 1200, 1200
    ys = np.arange(height, dtype=np.int32).reshape(-1, 1)
    xs = np.arange(width, dtype=np.int32).reshape(1, -1)
    data = ((xs * 3 + ys * 7) % 32000).astype(np.int16)
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


def _sha256(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def _read_gtiff(path: Path) -> tuple:
    with rasterio.open(path) as src:
        data = src.read()
        crs = src.crs
        transform = src.transform
    return data, crs, transform


def _read_xarray_asset(path: Path) -> tuple:
    """Liest Zarr-Store oder NetCDF via xarray zurueck. Gibt (data_3d, crs_str,
    transform_tuple) zurueck. data_3d hat immer eine band-Achse (count, y, x)
    fuer den direkten Vergleich mit rasterio.read().

    mask_and_scale=False damit xarray den Fill-Wert nicht in NaN wandelt
    und den Datentyp nicht nach float upcasted - wir wollen die rohen
    int16-Werte fuer den Byte-Vergleich."""
    import xarray as xr
    if path.is_dir():
        ds = xr.open_zarr(str(path), mask_and_scale=False)
    else:
        ds = xr.open_dataset(str(path), engine="netcdf4",
                             mask_and_scale=False)
    try:
        da = ds["DEM"]
        arr = da.values
        if arr.ndim == 2:
            arr = arr[np.newaxis, :, :]
        # Grid-Mapping Variable spatial_ref lesen
        sr_attrs = ds["spatial_ref"].attrs if "spatial_ref" in ds else {}
        crs = sr_attrs.get("crs_wkt") or sr_attrs.get("spatial_ref") or ""
        geot_str = sr_attrs.get("GeoTransform", "")
        transform = None
        if geot_str:
            parts = [float(v) for v in geot_str.split()]
            if len(parts) == 6:
                # GDAL GeoTransform: c a b f d e (rasterio order)
                c, a, b, f, d, e = parts
                transform = (a, b, c, d, e, f)  # rasterio.Affine argument order
    finally:
        ds.close()
    return arr, crs, transform


def _log_stac_media_types() -> None:
    print("\n[stac] media_types die build_stac_item pro Format liefert:")
    for fmt in DEM_FORMATS:
        item = build_stac_item(
            region="berlin", asset_href=f"http://example.org/asset{_DEM_FORMAT_EXT[fmt]}",
            epsg=32633,
            item_id=f"test_{fmt}",
            dem_format=fmt,
        )
        asset = item["assets"]["data"]
        print(f"  {fmt:8s}  media_type={asset['type']:36s}  href={asset['href']}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dem_format_test_"))
    print(f"[test] tmp dir: {tmp}")

    # 0. Optionale Deps pruefen
    for fmt in ("zarr", "netcdf"):
        try:
            _check_dem_format_deps(fmt)
        except ImportError as exc:
            print(f"[test] FEHLER: {exc}")
            print(f"[test] Bitte oben genannte Pakete installieren und "
                  f"Test erneut ausfuehren.")
            return 1
    print("[test] Optionale Pakete (xarray, zarr, netcdf4) OK.")

    # 1. Synthetisches DEM
    src_tif = tmp / "src_dem.tif"
    _make_synthetic_dem(src_tif)
    print(f"[test] synthetisches DEM erzeugt: {src_tif} "
          f"({src_tif.stat().st_size / 1024:.1f} KB)")

    # 2. EINE Reprojektion, Puffer wird von allen 3 Writern geteilt.
    print("\n[test] EINE Reprojektion in In-Memory-Puffer "
          "(EPSG:4326 -> EPSG:32633, nearest, 10 m Snap)")
    data, dst_meta = _reproject_dem_to_array(
        str(src_tif), "EPSG:32633",
        resampling="nearest", target_resolution=10.0,
    )
    print(f"  Puffer: shape={data.shape}, dtype={data.dtype}, "
          f"crs={dst_meta['crs']}, "
          f"transform=({dst_meta['transform'].a:.1f}, {dst_meta['transform'].e:.1f})")

    ref_hash = _sha256(data)
    print(f"  In-Memory-SHA256: {ref_hash}")

    # 3. Drei Writer, drei Asset-Pfade
    paths = {}
    for fmt in DEM_FORMATS:
        target = tmp / f"asset{_DEM_FORMAT_EXT[fmt]}"
        if fmt == "gtiff":
            _write_dem_with_layout(data, dst_meta, str(target), layout="striped")
        elif fmt == "zarr":
            _write_dem_as_zarr(data, dst_meta, str(target))
        elif fmt == "netcdf":
            _write_dem_as_netcdf(data, dst_meta, str(target))
        info = _inspect_asset_size(str(target))
        marker = "dir" if info["is_directory"] else "file"
        print(f"\n[test] {fmt:8s} geschrieben: {target}  "
              f"({marker}, {info['size_bytes'] / 1024:.1f} KB, "
              f"{info['num_files']} Dateien)")
        paths[fmt] = target

    # 4. Wieder einlesen + vergleichen
    print("\n[test] Wieder einlesen + Pixel-Vergleich gegen In-Memory-Puffer")
    all_ok = True
    for fmt, path in paths.items():
        if fmt == "gtiff":
            arr, crs, transform = _read_gtiff(path)
            crs_str = str(crs) if crs else ""
        else:
            arr, crs_str, transform = _read_xarray_asset(path)
        arr_hash = _sha256(arr)
        equal = np.array_equal(arr, data)
        marker = "OK" if equal and arr_hash == ref_hash else "MISMATCH"
        print(f"  {fmt:8s}  sha256={arr_hash}  array_equal={equal}  "
              f"crs={(crs_str or '(none)')[:40]}...  transform_present={transform is not None}  "
              f"[{marker}]")
        if not equal or arr_hash != ref_hash:
            all_ok = False

    # 5. Georeferenz-Rueckwaerts-Check (Zarr/NetCDF): Transform-Parameter
    print("\n[test] Georeferenz-Vergleich (Zarr/NetCDF -> rasterio)")
    src_a = dst_meta["transform"].a
    src_e = dst_meta["transform"].e
    src_c = dst_meta["transform"].c
    src_f = dst_meta["transform"].f
    print(f"  In-Memory: a={src_a}, e={src_e}, origin=({src_c}, {src_f})")
    for fmt in ("zarr", "netcdf"):
        _, crs_str, tr = _read_xarray_asset(paths[fmt])
        if tr is None:
            print(f"  {fmt:8s}  KEINE GeoTransform in spatial_ref!")
            all_ok = False
            continue
        a, b, c, d, e, f = tr
        matches = (abs(a - src_a) < 1e-6 and abs(e - src_e) < 1e-6
                   and abs(c - src_c) < 1e-6 and abs(f - src_f) < 1e-6)
        marker = "OK" if matches else "MISMATCH"
        print(f"  {fmt:8s}  a={a}, e={e}, origin=({c}, {f})  crs_wkt={(crs_str or '')[:30]}...  [{marker}]")
        if not matches:
            all_ok = False

    # 6. STAC-Media-Types
    _log_stac_media_types()

    print()
    if all_ok:
        print("[test] ALLE FORMATE PIXEL-IDENTISCH + GEOREFERENZ ERHALTEN.")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
    print("[test] FEHLER: mindestens ein Format weicht ab.")
    print(f"[test] Debug-Ordner bleibt: {tmp}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

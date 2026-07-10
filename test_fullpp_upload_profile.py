#!/usr/bin/env python3
"""
test_fullpp_upload_profile.py

Verifiziert die Aenderungen zur full_pp-Ursachensuche OHNE CDSE:
  1. _rewrite_tif_clean default schreibt gestreiftes, unkomprimiertes
     GeoTIFF (== was local_pp fuer sein DEM benutzt), das rasterio
     komplett dekodiert.
  2. _verify_tif_readable ist streng: OK auf sauberer Datei, RuntimeError
     bei abgeschnittener Datei (letzte 20% der Bytes weggeschnitten).
  3. _inspect_tif_header_bytes klassifiziert korrekt:
       - saubere Datei     -> is_tiff=True, verdict='tiff_header_ok_body_may_be_corrupt'
       - abgeschnittene Datei (IFD-Offset > EOF) -> 'structurally_truncated_ifd_offset_beyond_eof'
       - reines Muell-4-Byte -> 'too_short' oder 'not_a_tiff'
  4. build_full_pp_scenario mit save_format='netCDF' aendert das
     saveresult1 Format tatsaechlich im Prozessgraphen.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

sys.path.insert(0, str(Path(__file__).parent))

from run_benchmark import (
    _rewrite_tif_clean,
    _verify_tif_readable,
    _inspect_tif_header_bytes,
    build_full_pp_scenario,
)


def _make_source_tif(path: Path, tiled: bool = True) -> None:
    """Erzeugt ein GeoTIFF mit tiled+deflate Profil (aehnelt CDSE-S2-Output)."""
    width, height = 512, 512
    data = np.arange(width * height, dtype=np.int16).reshape(height, width) % 32000
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
    if tiled:
        profile.update({"tiled": True, "blockxsize": 128, "blockysize": 128,
                        "compress": "deflate"})
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def test_rewrite_default_simple_striped(tmp: Path) -> None:
    print("\n=== TEST 1: _rewrite_tif_clean default = simple_striped ===")
    src = tmp / "src_tiled.tif"
    _make_source_tif(src, tiled=True)

    dst = tmp / "out_default.tif"
    _rewrite_tif_clean(str(src), str(dst))

    with rasterio.open(dst) as r:
        prof = r.profile
    assert prof.get("tiled", False) is False, \
        f"Default sollte tiled=False sein, war {prof.get('tiled')}"
    assert prof.get("compress") in (None, ""), \
        f"Default sollte KEINE Kompression sein, war {prof.get('compress')}"
    print(f"  OK  tiled={prof.get('tiled')}, compress={prof.get('compress')}, "
          f"size={dst.stat().st_size:,} B")

    # tiled_deflate Fallback funktioniert weiterhin
    dst2 = tmp / "out_tiled_deflate.tif"
    _rewrite_tif_clean(str(src), str(dst2), profile="tiled_deflate")
    with rasterio.open(dst2) as r:
        prof2 = r.profile
    assert prof2.get("tiled") is True
    assert prof2.get("compress") == "deflate"
    print(f"  OK  tiled_deflate: tiled={prof2['tiled']}, "
          f"blocksize={prof2['blockxsize']}x{prof2['blockysize']}, "
          f"compress={prof2['compress']}")


def test_verify_tif_readable(tmp: Path) -> None:
    print("\n=== TEST 2: _verify_tif_readable ===")
    src = tmp / "src_healthy.tif"
    _make_source_tif(src, tiled=False)

    # Gesund -> OK
    info = _verify_tif_readable(str(src), label="healthy")
    assert info["count"] == 1
    assert info["dtype"] == "int16"
    print(f"  OK  healthy verifiziert: shape={info['shape']}")

    # Truncated: 20% abschneiden
    trunc = tmp / "src_trunc.tif"
    data = src.read_bytes()
    trunc.write_bytes(data[: int(len(data) * 0.8)])
    try:
        _verify_tif_readable(str(trunc), label="trunc")
    except RuntimeError as exc:
        msg = str(exc)
        assert "rasterio konnte" in msg or "nicht vollstaendig" in msg, msg
        print(f"  OK  truncated raises RuntimeError: {msg[:120]}...")
    else:
        raise AssertionError("Truncated TIFF haette RuntimeError werfen muessen")


def test_inspect_tif_header_bytes(tmp: Path) -> None:
    print("\n=== TEST 3: _inspect_tif_header_bytes ===")
    src = tmp / "healthy.tif"
    _make_source_tif(src, tiled=False)
    diag = _inspect_tif_header_bytes(str(src))
    assert diag["is_tiff"] is True
    assert diag["byte_order"] in ("little_endian", "big_endian")
    assert diag["first_ifd_offset"] is not None
    assert diag["first_ifd_within_file"] is True
    print(f"  OK  healthy: {diag['byte_order']}, is_bigtiff={diag['is_bigtiff']}, "
          f"ifd@{diag['first_ifd_offset']}/{diag['size_bytes']}, "
          f"verdict={diag['verdict']}")

    # Fabrizierter TIFF-Header der einen IFD-Offset > EOF behauptet -
    # exakt das Muster wenn CDSE bei full_pp einen truncated Body ausliefern
    # wuerde. Klassischer TIFF: 'II' + 42 (LE) + IFD-Offset (4 Bytes LE).
    fake = tmp / "fake_truncated.tif"
    # IFD-Offset = 100000, aber wir schreiben nur 100 Bytes.
    header = b"II" + (42).to_bytes(2, "little") + (100_000).to_bytes(4, "little")
    fake.write_bytes(header + b"\x00" * 92)  # 100 Bytes total
    diag2 = _inspect_tif_header_bytes(str(fake))
    print(f"  fabrikierter Bogus-IFD: verdict={diag2['verdict']}, "
          f"ifd_within_file={diag2['first_ifd_within_file']}, "
          f"ifd_offset={diag2['first_ifd_offset']}, size={diag2['size_bytes']}")
    assert diag2["is_tiff"] is True
    assert diag2["first_ifd_within_file"] is False, \
        "Fake TIFF mit IFD@100000 in 100-Byte-Datei sollte truncated melden"
    assert diag2["verdict"] == "structurally_truncated_ifd_offset_beyond_eof"
    print(f"  OK  strukturell truncated (IFD-Offset > EOF) erkannt")

    # Und ein realer Healthy-Fall: IFD innerhalb, Verdikt "header_ok_body..."
    diag_full = _inspect_tif_header_bytes(str(src))
    assert diag_full["first_ifd_within_file"] is True
    assert diag_full["verdict"] == "tiff_header_ok_body_may_be_corrupt"
    print(f"  OK  healthy erneut: {diag_full['verdict']}")

    # 4-Byte Junk
    junk = tmp / "junk.bin"
    junk.write_bytes(b"NOPE")
    diag3 = _inspect_tif_header_bytes(str(junk))
    assert diag3["is_tiff"] is False
    assert diag3["verdict"] in ("too_short", "not_a_tiff (byte-order Bytes falsch)")
    print(f"  OK  Junk-4-byte: verdict={diag3['verdict']}")


def test_save_format_netcdf(tmp: Path) -> None:
    print("\n=== TEST 4: build_full_pp_scenario save_format=netCDF ===")
    import json
    out = tmp / "fullpp_netcdf.json"
    build_full_pp_scenario(
        region="berlin",
        s2_stac_url="http://example.org/collection.json",
        dem_stac_url="http://example.org/dem_item.json",
        target_path=out,
        extent_size="medium",
        workflow="merge_add",
        save_format="netCDF",
    )
    with open(out) as f:
        graph = json.load(f)
    save_node = graph["process_graph"]["saveresult1"]
    assert save_node["arguments"]["format"] == "netCDF", save_node["arguments"]
    print(f"  OK  saveresult1.format = {save_node['arguments']['format']}")

    # Und Default = GTiff bleibt
    out2 = tmp / "fullpp_default.json"
    build_full_pp_scenario(
        region="berlin",
        s2_stac_url="http://example.org/collection.json",
        dem_stac_url="http://example.org/dem_item.json",
        target_path=out2,
        extent_size="medium",
        workflow="merge_add",
    )
    with open(out2) as f:
        graph2 = json.load(f)
    save_node2 = graph2["process_graph"]["saveresult1"]
    assert save_node2["arguments"]["format"] == "GTiff", save_node2["arguments"]
    print(f"  OK  Default saveresult1.format = {save_node2['arguments']['format']}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fullpp_test_"))
    print(f"[test] tmp dir: {tmp}")
    try:
        test_rewrite_default_simple_striped(tmp)
        test_verify_tif_readable(tmp)
        test_inspect_tif_header_bytes(tmp)
        test_save_format_netcdf(tmp)
        print("\n" + "=" * 60)
        print("ALLE TESTS OK")
        print("=" * 60)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

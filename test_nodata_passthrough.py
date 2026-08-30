#!/usr/bin/env python3
"""
test_nodata_passthrough.py - Standalone-Tests fuer das Durchreichen der
deklarierten Nodata-Werte in den Genauigkeitsvergleich.

Befund (Lauf 1165, Terrascope, berlin/medium/merge_add, local_pp): das
Testraster deklariert nodata=32767 und traegt diesen Wert auf 107433
Zellen, die Referenz deklariert NaN. Der Vergleich wertete den
deklarierten Wert nicht aus, rechnete die Sentinel-Zellen gegen
Hoehenwerte um 40 und kam auf MAE 1,6233 / RMSE 189,9962 statt der auf
CDSE gemessenen 0,00128 / 0,00178.

Geprueft wird:
  1. align_rasters liefert die Sentinels beider Dateien und bleibt fuer
     Aufrufer, die drei Werte auspacken, unveraendert.
  2. calculate_metrics trennt Referenz- und Testsentinel, der alte
     Ein-Wert-Aufruf funktioniert weiter.
  3. NaN bleibt immer ungueltig, None maskiert nichts.
  4. _compare_tif_pair schliesst die Sentinel-Zellen aus - nachgestellt
     mit dem Zahlenbild aus Lauf 1165.
  5. Zwei NaN-Seiten (der CDSE-Fall) liefern denselben Wert wie vorher.

Die Testraster werden hier lokal erzeugt; es wird keine Datei aus
outputs/ geoeffnet.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import accuracy_calculator as ac
import run_benchmark as rb

TRANSFORM = Affine(10.0, 0.0, 380000.0, 0.0, -10.0, 5820000.0)
SHAPE = (40, 40)                 # 1600 Zellen
N_SENTINEL = 400                 # davon ungueltig (Anteil wie in Lauf 1165)
TS_NODATA = 32767.0              # Terrascope
CDSE_NODATA = -32768.0           # CDSE


def _write(path: Path, array: np.ndarray, nodata) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=array.shape[0],
                       width=array.shape[1], count=1, dtype=array.dtype,
                       crs="EPSG:32633", transform=TRANSFORM,
                       nodata=nodata) as dst:
        dst.write(array, 1)
    return path


def _paar(tmp: Path, name: str, test_nodata, ref_nodata=float("nan")):
    """Referenz (NaN) und Testraster mit dem gegebenen Sentinel.

    Gueltige Zellen unterscheiden sich um genau 0.5, die Sentinel-Zellen
    tragen im Test den Sentinel und in der Referenz einen Hoehenwert.
    """
    ref = np.full(SHAPE, 40.0, dtype="float32")
    test = np.full(SHAPE, 40.5, dtype="float32")
    test.reshape(-1)[:N_SENTINEL] = np.float32(test_nodata)
    if ref_nodata is not None and not np.isnan(ref_nodata):
        ref.reshape(-1)[:0] = ref_nodata     # kein Sentinel in der Referenz
    return (_write(tmp / f"{name}_ref.tif", ref, ref_nodata),
            _write(tmp / f"{name}_test.tif", test, test_nodata))


def test_align_rasters(tmp: Path) -> None:
    print("\n--- Test 1: align_rasters liefert beide Sentinels ---")
    ref_tif, test_tif = _paar(tmp, "align", TS_NODATA)
    aligned = ac.align_rasters(str(ref_tif), str(test_tif))
    # Auspacken wie bisher - drei Werte.
    ref_data, test_data, profile = aligned
    assert len(aligned) == 3, len(aligned)
    assert ref_data.shape == test_data.shape
    assert profile["crs"].to_epsg() == 32633
    print(f"  Dreiertupel weiterhin auspackbar: len={len(aligned)}")
    print(f"  ref_nodata={aligned.ref_nodata}  test_nodata={aligned.test_nodata}")
    assert np.isnan(aligned.ref_nodata), aligned.ref_nodata
    assert aligned.test_nodata == TS_NODATA, aligned.test_nodata

    # Zweiter Rueckgabepfad: unterschiedliche Grids -> Reprojektion.
    grob = np.full((20, 20), 40.5, dtype="float32")
    grob_tif = tmp / "align_grob.tif"
    with rasterio.open(grob_tif, "w", driver="GTiff", height=20, width=20,
                       count=1, dtype="float32", crs="EPSG:32633",
                       transform=Affine(20.0, 0, 380000.0, 0, -20.0, 5820000.0),
                       nodata=CDSE_NODATA) as dst:
        dst.write(grob, 1)
    a2 = ac.align_rasters(str(ref_tif), str(grob_tif))
    assert len(a2) == 3 and a2.test_nodata == CDSE_NODATA, a2.test_nodata
    print(f"  Reprojektionspfad: test_nodata={a2.test_nodata}")
    print("  OK: beide Rueckgabepunkte tragen die Sentinels.")


def test_calculate_metrics() -> None:
    print("\n--- Test 2: calculate_metrics, getrennt je Seite ---")
    ref = np.array([[[40.0, 40.0, 40.0, np.nan]]])
    test = np.array([[[40.5, TS_NODATA, 40.5, 40.5]]])

    ohne = ac.calculate_metrics(ref, test)["bands"][0]
    getrennt = ac.calculate_metrics(
        ref, test, ref_nodata=float("nan"), test_nodata=TS_NODATA)["bands"][0]
    print(f"  ohne Sentinel : MAE={ohne['MAE']:.4f}  "
          f"gueltig={ohne['valid_pixels']}/{ohne['total_pixels']}")
    print(f"  mit  Sentinel : MAE={getrennt['MAE']:.4f}  "
          f"gueltig={getrennt['valid_pixels']}/{getrennt['total_pixels']}")
    assert ohne["valid_pixels"] == 3          # NaN faellt schon raus
    assert getrennt["valid_pixels"] == 2      # zusaetzlich der 32767er
    assert abs(getrennt["MAE"] - 0.5) < 1e-9, getrennt["MAE"]

    # Alter Ein-Wert-Aufruf, positional wie im CLI: gilt fuer beide Seiten.
    alt = ac.calculate_metrics(ref, test, TS_NODATA)["bands"][0]
    assert alt["valid_pixels"] == 2, alt
    assert abs(alt["MAE"] - 0.5) < 1e-9
    print(f"  alter Aufruf calculate_metrics(ref, test, {TS_NODATA:.0f}): "
          f"MAE={alt['MAE']:.4f}  gueltig={alt['valid_pixels']}")

    # Getrennte Werte haben Vorrang vor dem Sammelparameter.
    vorrang = ac.calculate_metrics(ref, test, nodata=12345.0,
                                   test_nodata=TS_NODATA)["bands"][0]
    assert vorrang["valid_pixels"] == 2, vorrang
    print("  OK: getrennt, rueckwaertskompatibel, mit Vorrang.")


def test_nan_und_none() -> None:
    print("\n--- Test 3: NaN immer ungueltig, None maskiert nichts ---")
    ref = np.array([[[40.0, np.nan, 40.0]]])
    test = np.array([[[40.5, 40.5, 40.5]]])
    # Sentinel gesetzt - NaN muss trotzdem rausfallen.
    b = ac.calculate_metrics(ref, test, ref_nodata=-9999.0,
                             test_nodata=-9999.0)["bands"][0]
    assert b["valid_pixels"] == 2, b
    print(f"  Sentinel -9999 gesetzt, NaN faellt weiter raus: "
          f"gueltig={b['valid_pixels']}/3")
    # None: nichts zusaetzlich maskieren.
    b2 = ac.calculate_metrics(ref, test, ref_nodata=None,
                              test_nodata=None)["bands"][0]
    assert b2["valid_pixels"] == 2, b2
    # NaN als Sentinel ist ein No-Op.
    b3 = ac.calculate_metrics(ref, test, ref_nodata=float("nan"),
                              test_nodata=float("nan"))["bands"][0]
    assert b3["valid_pixels"] == 2 and b3["MAE"] == b2["MAE"]
    print("  None und NaN als Sentinel: identisches Ergebnis")
    print("  OK")


def test_compare_tif_pair(tmp: Path) -> None:
    print("\n--- Test 4: _compare_tif_pair (Fall Lauf 1165) ---")
    ref_tif, test_tif = _paar(tmp, "lauf1165", TS_NODATA)
    mae, rmse, n_bands, valid, total = rb._compare_tif_pair(ref_tif, test_tif)
    anteil = 100.0 * valid / total
    print(f"  Terrascope: MAE={mae:.4f}  RMSE={rmse:.4f}  "
          f"gueltig={valid:,}/{total:,} ({anteil:.1f} %)")
    assert valid == SHAPE[0] * SHAPE[1] - N_SENTINEL, valid
    assert abs(mae - 0.5) < 1e-6, mae
    assert abs(rmse - 0.5) < 1e-6, rmse

    # Gegenprobe: ohne Sentinel-Auswertung waeren es die alten Zahlen.
    import numpy as _np
    from accuracy_calculator import calculate_metrics
    a = ac.align_rasters(str(ref_tif), str(test_tif))
    alt = calculate_metrics(a[0], a[1])["bands"][0]
    print(f"  ohne den Fix waere es: MAE={alt['MAE']:.2f}  "
          f"RMSE={alt['RMSE']:.2f}  gueltig={alt['valid_pixels']:,}")
    assert alt["MAE"] > 1000, alt["MAE"]
    print("  OK: Sentinel-Zellen sind draussen.")


def test_cdse_unveraendert(tmp: Path) -> None:
    """Beide Seiten NaN: der Wert darf sich nicht bewegen."""
    print("\n--- Test 5: beide Seiten NaN (CDSE-Fall) ---")
    ref = np.full(SHAPE, 40.0, dtype="float32")
    test = np.full(SHAPE, 40.5, dtype="float32")
    test.reshape(-1)[:N_SENTINEL] = np.nan
    ref_tif = _write(tmp / "cdse_ref.tif", ref, float("nan"))
    test_tif = _write(tmp / "cdse_test.tif", test, float("nan"))

    mae, rmse, _n, valid, total = rb._compare_tif_pair(ref_tif, test_tif)
    a = ac.align_rasters(str(ref_tif), str(test_tif))
    alt = ac.calculate_metrics(a[0], a[1])["bands"][0]
    print(f"  neu: MAE={mae:.6f} gueltig={valid:,}/{total:,}")
    print(f"  alt: MAE={alt['MAE']:.6f} gueltig={alt['valid_pixels']:,}")
    assert abs(mae - alt["MAE"]) < 1e-12
    assert valid == alt["valid_pixels"]
    print("  OK: identisch - dort greift schon die isfinite-Maske.")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="nodata_pass_"))
    print(f"Temp: {tmp}")
    try:
        test_align_rasters(tmp)
        test_calculate_metrics()
        test_nan_und_none()
        test_compare_tif_pair(tmp)
        test_cdse_unveraendert(tmp)
        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
test_local_reference_nodata.py - Standalone-Tests fuer die Nodata-Behandlung
in der lokalen Referenzpipeline (_apply_local_workflow, step4_result).

Nachgestellt wird der belegte Fall aus
outputs/run_20260826_134353_local_reference (berlin, large, merge_add):
das reprojizierte S2-Raster traegt nodata=-32768 auf einem Teil der Zellen,
das DEM traegt nodata=NaN. Vor dem Fix wanderte der Sentinel als echter
Messwert in die Addition (-32768 + Hoehe ~ -32735) und das Ergebnis
deklarierte NaN als Nodata, wodurch die Artefakte als gueltig galten.

Verglichen wird gegen die Version aus HEAD, damit vorher/nachher auf
DENSELBEN Eingaben nebeneinander steht. Alles laeuft in einem temporaeren
Ordner; outputs/ und die DB werden nicht angefasst, es gibt keine
Backend-Aufrufe.
"""
from __future__ import annotations

import importlib.util
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import run_benchmark as rb

TRANSFORM = Affine(10.0, 0.0, 380000.0, 0.0, -10.0, 5820000.0)
SHAPE = (40, 40)
S2_NODATA = -32768.0          # wie im belegten Lauf (int16-Sentinel)
N_NODATA_CELLS = 100          # Zellen, die den Sentinel tragen
LC_NODATA = 0                 # uint8-Sentinel der Landcover-Karte


def _write(path: Path, array: np.ndarray, nodata) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=array.shape[-2],
                       width=array.shape[-1], count=array.shape[0]
                       if array.ndim == 3 else 1,
                       dtype=array.dtype, crs="EPSG:32633",
                       transform=TRANSFORM, nodata=nodata) as dst:
        if array.ndim == 3:
            dst.write(array)
        else:
            dst.write(array, 1)
    return path


def make_inputs(tmp: Path, bands: int = 1, dataset: str = "dem") -> tuple:
    """(s2_tif, second_tif) auf identischem Grid.

    S2: int16 mit Sentinel-Zellen. Zweites Raster: DEM float32/NaN oder
    Landcover uint8/0 - beide Sentinel-Varianten kommen real vor.
    """
    b04 = np.full(SHAPE, 2500, dtype="int16")
    b04.reshape(-1)[:N_NODATA_CELLS] = int(S2_NODATA)
    if bands == 2:
        scl = np.full(SHAPE, 4, dtype="int16")      # 4 = gueltige Vegetation
        scl.reshape(-1)[:N_NODATA_CELLS] = int(S2_NODATA)
        s2 = np.stack([b04, scl])
    else:
        s2 = b04[np.newaxis, ...]
    s2_tif = _write(tmp / "step3_reprojected" / "openEO_2024-07-06Z.tif",
                    s2, S2_NODATA)

    if dataset == "landcover":
        second = np.full(SHAPE, rb.LC_MASK_CLASS, dtype="uint8")
        second.reshape(-1)[-50:] = LC_NODATA
        second_tif = _write(tmp / "step3_reprojected" / "dem.tif", second,
                            LC_NODATA)
    else:
        dem = np.full(SHAPE, 38.5, dtype="float32")
        dem.reshape(-1)[-50:] = np.nan
        second_tif = _write(tmp / "step3_reprojected" / "dem.tif", dem,
                            float("nan"))
    return s2_tif, second_tif


def _load_head_module(tmp: Path):
    """run_benchmark.py aus HEAD als eigenes Modul laden (Zustand vor dem Fix)."""
    head = subprocess.run(["git", "show", "HEAD:run_benchmark.py"],
                          cwd=ROOT, capture_output=True)
    assert head.returncode == 0, head.stderr.decode(errors="replace")
    path = tmp / "run_benchmark_head.py"
    path.write_bytes(head.stdout)
    spec = importlib.util.spec_from_file_location("rb_head", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _describe(tif: Path) -> dict:
    with rasterio.open(tif) as src:
        data = src.read(1).astype("float64")
        nodata = src.nodata
    finite = data[np.isfinite(data)]
    return {
        "nodata": nodata,
        "min": float(finite.min()) if finite.size else float("nan"),
        "max": float(finite.max()) if finite.size else float("nan"),
        "n_nan": int(np.isnan(data).sum()),
        "unter_-1000": int((finite < -1000).sum()),
    }


def _fmt(d: dict) -> str:
    nod = "nan" if d["nodata"] is not None and math.isnan(d["nodata"]) \
        else str(d["nodata"])
    return (f"nodata={nod}, min={d['min']:.2f}, max={d['max']:.2f}, "
            f"NaN-Zellen={d['n_nan']}, Zellen<-1000={d['unter_-1000']}")


def test_before_after(tmp_root: Path) -> None:
    """merge_add auf identischen Eingaben: HEAD gegen Arbeitsstand."""
    print("\n--- Test 1: merge_add, vorher (HEAD) gegen nachher ---")
    tmp = tmp_root / "beforeafter"
    s2_tif, dem_tif = make_inputs(tmp)
    rb_head = _load_head_module(tmp_root)

    old_dir = tmp / "step4_head"
    old_dir.mkdir(parents=True)
    rb_head._apply_local_workflow("merge_add", [s2_tif], dem_tif, old_dir)
    old = _describe(old_dir / s2_tif.name)

    new_dir = tmp / "step4_neu"
    new_dir.mkdir(parents=True)
    rb._apply_local_workflow("merge_add", [s2_tif], dem_tif, new_dir)
    new = _describe(new_dir / s2_tif.name)

    print(f"  Eingabe S2 : nodata={S2_NODATA}, {N_NODATA_CELLS} Zellen "
          f"tragen den Sentinel")
    print(f"  Eingabe DEM: nodata=nan, 50 Zellen NaN")
    print(f"  HEAD       : {_fmt(old)}")
    print(f"  Arbeitsstand: {_fmt(new)}")

    # Vorher: Sentinel wurde mitgerechnet und galt als gueltiger Wert.
    assert old["unter_-1000"] == N_NODATA_CELLS, old
    assert old["min"] < -32000, old
    # Nachher: keine Artefakte mehr, ungueltige Zellen sind NaN.
    assert new["unter_-1000"] == 0, new
    assert new["min"] > 0, new
    assert new["n_nan"] == N_NODATA_CELLS + 50, new
    assert new["nodata"] is not None and math.isnan(new["nodata"]), new
    print(f"  OK: {N_NODATA_CELLS} Artefaktzellen verschwunden, "
          f"Ausgabe-nodata=nan passt zu den Werten.")


def test_all_workflows(tmp_root: Path) -> None:
    """Der Fix gilt fuer jeden rechnenden Workflow, nicht nur merge_add."""
    print("\n--- Test 2: alle Workflows ---")
    cases = [
        ("merge_add", 1, "dem"),
        ("subtract", 1, "dem"),
        ("resample", 1, "dem"),
        ("focal", 1, "dem"),
        ("aggregation", 1, "dem"),
        ("filter_bbox", 1, "dem"),
        ("mask", 2, "dem"),
        ("lc_mask", 1, "landcover"),
    ]
    for workflow, bands, dataset in cases:
        tmp = tmp_root / f"wf_{workflow}"
        s2_tif, second_tif = make_inputs(tmp, bands=bands, dataset=dataset)
        out_dir = tmp / "step4_result"
        out_dir.mkdir(parents=True)
        outs = rb._apply_local_workflow(workflow, [s2_tif], second_tif, out_dir)
        assert outs, workflow
        d = _describe(outs[0])
        print(f"  {workflow:<12} {_fmt(d)}")
        assert d["unter_-1000"] == 0, f"{workflow}: Artefaktwerte im Ergebnis {d}"
        assert d["nodata"] is not None and math.isnan(d["nodata"]), \
            f"{workflow}: nodata={d['nodata']} passt nicht zu NaN-Werten"
        assert d["n_nan"] >= N_NODATA_CELLS or workflow == "filter_bbox", d
    print("  OK: kein Workflow rechnet den Sentinel mit, alle deklarieren NaN.")


def test_lc_overlay_unveraendert(tmp_root: Path) -> None:
    """lc_overlay bleibt Klassenraster im Quell-dtype mit Quell-Sentinel."""
    print("\n--- Test 3: lc_overlay unveraendert (kategorial) ---")
    tmp = tmp_root / "wf_lc_overlay"
    s2_tif, lc_tif = make_inputs(tmp, bands=1, dataset="landcover")
    out_dir = tmp / "step4_result"
    out_dir.mkdir(parents=True)
    outs = rb._apply_local_workflow("lc_overlay", [s2_tif], lc_tif, out_dir)
    with rasterio.open(outs[0]) as src, rasterio.open(lc_tif) as ref:
        assert src.dtypes[0] == ref.dtypes[0] == "uint8", src.dtypes
        assert src.nodata == ref.nodata == LC_NODATA, src.nodata
        assert np.array_equal(src.read(1), ref.read(1))
    print(f"  dtype=uint8, nodata={LC_NODATA}, Klassen unveraendert")
    print("  OK: kategorialer Zweig nicht angefasst.")


def test_nodata_to_nan_helper() -> None:
    """Die Maske kommt aus dem Attribut, nicht aus einem festen Wert."""
    print("\n--- Test 4: _nodata_to_nan ---")
    arr = np.array([[1, -32768, 0]], dtype="int16")
    assert np.isnan(rb._nodata_to_nan(arr, -32768.0)[0, 1])
    assert not np.isnan(rb._nodata_to_nan(arr, -32768.0)[0, 2])
    # Anderer Sentinel derselben Daten -> andere Zelle wird maskiert.
    assert np.isnan(rb._nodata_to_nan(arr, 0)[0, 2])
    assert not np.isnan(rb._nodata_to_nan(arr, 0)[0, 1])
    # Kein Nodata deklariert -> nichts maskieren.
    assert not np.isnan(rb._nodata_to_nan(arr, None)).any()
    # NaN als Sentinel -> unveraendert (ist schon NaN).
    f = np.array([[1.0, np.nan]], dtype="float32")
    out = rb._nodata_to_nan(f, float("nan"))
    assert np.isnan(out[0, 1]) and out[0, 0] == 1.0
    print("  OK: -32768, 0, None und NaN korrekt unterschieden.")


def test_metric_impact(tmp_root: Path) -> None:
    """Wirkung auf MAE/RMSE: die Artefaktzellen liefen als gueltige Werte in
    den Vergleich ein. Nachgestellt mit einer sauberen Gegenseite, die an den
    ungueltigen Zellen NaN traegt und sonst um 2.0 abweicht."""
    print("\n--- Test 5: Wirkung auf MAE/RMSE ---")
    tmp = tmp_root / "metrik"
    s2_tif, dem_tif = make_inputs(tmp)
    rb_head = _load_head_module(tmp_root)

    old_dir, new_dir = tmp / "step4_head", tmp / "step4_neu"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    rb_head._apply_local_workflow("merge_add", [s2_tif], dem_tif, old_dir)
    rb._apply_local_workflow("merge_add", [s2_tif], dem_tif, new_dir)

    # Gegenseite (CDSE): an den S2-Nodata-Zellen stehen dort ECHTE Zahlen,
    # kein NaN - genau deshalb schlagen die Artefakte der Referenz voll
    # durch. Waere auch die Gegenseite dort ungueltig, fielen die Zellen
    # beidseitig aus der Maske und der Fehler bliebe unsichtbar.
    with rasterio.open(s2_tif) as src:
        b04 = src.read(1).astype("float64")
    with rasterio.open(dem_tif) as src:
        dem = src.read(1).astype("float64")
    b04_clean = np.where(b04 == S2_NODATA, 2500.0, b04)
    clean = (b04_clean + dem + 2.0).astype("float32")
    cdse_tif = _write(tmp / "cdse" / s2_tif.name, clean, float("nan"))

    for label, ref in (("HEAD", old_dir / s2_tif.name),
                       ("Arbeitsstand", new_dir / s2_tif.name)):
        mae, rmse, _bands, valid, total = rb._compare_tif_pair(ref, cdse_tif)
        print(f"  {label:<12} MAE={mae:10.4f}  RMSE={rmse:10.4f}  "
              f"({valid:,}/{total:,} Pixel gueltig)")
        if label == "HEAD":
            assert rmse > 1000, f"Artefakte muessten den RMSE sprengen: {rmse}"
        else:
            assert abs(rmse - 2.0) < 1e-3, rmse
            assert abs(mae - 2.0) < 1e-3, mae
    print("  OK: ohne Fix sprengen 100 Artefaktzellen den RMSE, mit Fix "
          "bleibt die echte Abweichung von 2.0 stehen.")


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="local_ref_nodata_"))
    print(f"Temp: {tmp_root}")
    try:
        test_nodata_to_nan_helper()
        test_before_after(tmp_root)
        test_all_workflows(tmp_root)
        test_lc_overlay_unveraendert(tmp_root)
        test_metric_impact(tmp_root)
        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

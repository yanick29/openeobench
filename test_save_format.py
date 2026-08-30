#!/usr/bin/env python3
"""
test_save_format.py - Standalone-Tests fuer --save-format.

Geprueft wird:
  1. GTiff (Default) laesst die Prozessgraphen von onthefly und
     local_preprocessing BYTE-IDENTISCH zur Version aus HEAD - bisheriges
     Verhalten unveraendert.
  2. netCDF setzt genau format+options im saveresult1-Knoten, sonst nichts.
  3. full_preprocessing bleibt an --fullpp-save-format haengen und wird von
     --save-format nicht beeinflusst.
  4. runs.save_format existiert nach create_database() und import_run()
     schreibt den Wert; eine bestehende DB ohne die Spalte wird beim
     naechsten Import nachgeruestet.
  5. Der Accuracy-Check ueberspringt netCDF-Ausgaben mit klarer Meldung,
     statt zu scheitern.

Keine Backend-Aufrufe, kein Zugriff auf outputs/; alles in einem
temporaeren Ordner mit temporaerer DuckDB.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import database
import run_benchmark as rb

REGION = "berlin"
EXTENT = "xlarge"          # der Fall aus dem Befund
WORKFLOW = "merge_add"


def _pg(path: Path) -> dict:
    return json.loads(path.read_text())["process_graph"]


def _load_head_module(tmp: Path):
    head = subprocess.run(["git", "show", "HEAD:run_benchmark.py"],
                          cwd=ROOT, capture_output=True)
    assert head.returncode == 0, head.stderr.decode(errors="replace")
    path = tmp / "run_benchmark_head.py"
    path.write_bytes(head.stdout)
    spec = importlib.util.spec_from_file_location("rb_head", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gtiff_unveraendert(tmp: Path) -> None:
    """Default GTiff: Graphen bitgleich zu HEAD, fuer beide Strategien."""
    print("\n--- Test 1: GTiff-Default unveraendert (HEAD vs Arbeitsstand) ---")
    rb_head = _load_head_module(tmp)
    stac_url = "https://example.invalid/stac_item_berlin.json"
    n = 0
    for extent in ("medium", EXTENT):
        for workflow in ("merge_add", "aggregation", "mask"):
            old = tmp / f"old_otf_{extent}_{workflow}.json"
            new = tmp / f"new_otf_{extent}_{workflow}.json"
            rb_head.build_onthefly_scenario(REGION, old, extent_size=extent,
                                            workflow=workflow)
            rb.build_onthefly_scenario(REGION, new, extent_size=extent,
                                       workflow=workflow)
            assert old.read_bytes() == new.read_bytes(), \
                f"onthefly {extent}/{workflow} weicht ab"

            old_pp = tmp / f"old_pp_{extent}_{workflow}.json"
            new_pp = tmp / f"new_pp_{extent}_{workflow}.json"
            rb_head.build_local_pp_scenario(REGION, stac_url, old_pp,
                                            extent_size=extent,
                                            workflow=workflow)
            rb.build_local_pp_scenario(REGION, stac_url, new_pp,
                                       extent_size=extent, workflow=workflow)
            assert old_pp.read_bytes() == new_pp.read_bytes(), \
                f"local_pp {extent}/{workflow} weicht ab"
            n += 2
    print(f"  OK: {n} Szenarien byte-identisch zu HEAD.")


def test_netcdf_knoten(tmp: Path) -> None:
    """netCDF aendert genau format+options, sonst keinen Knoten."""
    print("\n--- Test 2: erzeugter save_result-Knoten ---")
    stac_url = "https://example.invalid/stac_item_berlin.json"
    builders = {
        "onthefly": lambda p, fmt: rb.build_onthefly_scenario(
            REGION, p, extent_size=EXTENT, workflow=WORKFLOW, save_format=fmt),
        "local_preprocessing": lambda p, fmt: rb.build_local_pp_scenario(
            REGION, stac_url, p, extent_size=EXTENT, workflow=WORKFLOW,
            save_format=fmt),
    }
    for strategy, build in builders.items():
        g_path, n_path = tmp / f"{strategy}_g.json", tmp / f"{strategy}_n.json"
        build(g_path, "GTiff")
        build(n_path, "netCDF")
        g, n = _pg(g_path), _pg(n_path)
        print(f"\n  {strategy} / GTiff:")
        print("   " + json.dumps({"saveresult1": g["saveresult1"]},
                                 indent=2).replace("\n", "\n   "))
        print(f"  {strategy} / netCDF:")
        print("   " + json.dumps({"saveresult1": n["saveresult1"]},
                                 indent=2).replace("\n", "\n   "))
        assert g["saveresult1"]["arguments"]["format"] == "GTiff"
        assert n["saveresult1"]["arguments"]["format"] == "netCDF"
        assert n["saveresult1"]["arguments"]["options"] == {}
        # data-Anschluss und result-Flag bleiben gleich.
        assert (g["saveresult1"]["arguments"]["data"]
                == n["saveresult1"]["arguments"]["data"])
        assert g["saveresult1"]["result"] == n["saveresult1"]["result"] is True
        # Kein anderer Knoten unterscheidet sich.
        diff = [k for k in set(g) | set(n)
                if k != "saveresult1" and g.get(k) != n.get(k)]
        assert not diff, f"{strategy}: unerwartete Unterschiede in {diff}"
        print(f"  -> nur saveresult1 unterscheidet sich "
              f"({len(g)} Knoten insgesamt)")
    print("\n  OK: format+options gesetzt, Graph sonst unveraendert.")


def test_fullpp_eigenstaendig(tmp: Path) -> None:
    """--fullpp-save-format bleibt fuer full_pp zustaendig und unberuehrt."""
    print("\n--- Test 3: full_preprocessing unabhaengig ---")
    s2, dem = "https://example.invalid/s2.json", "https://example.invalid/dem.json"
    default = tmp / "fullpp_default.json"
    netcdf = tmp / "fullpp_netcdf.json"
    rb.build_full_pp_scenario(REGION, s2, dem, default, extent_size=EXTENT,
                              workflow=WORKFLOW)
    rb.build_full_pp_scenario(REGION, s2, dem, netcdf, extent_size=EXTENT,
                              workflow=WORKFLOW, save_format="netCDF")
    assert _pg(default)["saveresult1"]["arguments"]["format"] == "GTiff"
    assert _pg(netcdf)["saveresult1"]["arguments"]["format"] == "netCDF"
    # Signatur unveraendert: save_format ist weiterhin der einzige Schalter.
    import inspect
    sig = inspect.signature(rb.build_full_pp_scenario)
    assert sig.parameters["save_format"].default == "GTiff", sig
    print("  OK: eigener Parameter, Default GTiff, von --save-format "
          "unbeeinflusst.")


def test_db_spalte(tmp: Path) -> None:
    """runs.save_format wird angelegt und von import_run gefuellt."""
    print("\n--- Test 4: DB-Spalte save_format ---")
    old_db = database.DB_PATH
    db_path = str(tmp / "sf.duckdb")
    database.DB_PATH = db_path
    try:
        database.create_database()
        conn = duckdb.connect(db_path, read_only=True)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('runs')").fetchall()}
        conn.close()
        assert "save_format" in cols, sorted(cols)

        run_dir = tmp / "run_x_onthefly"
        run_dir.mkdir()
        (run_dir / "results.json").write_text(json.dumps({
            "backend_url": "local-test", "backend_name": "test",
            "process_graph": "scenario_onthefly", "status": "success",
            "timestamp": "2026-08-27T10:00:00", "job_status_history": {},
        }))
        rid = database.import_run(str(run_dir), crs_strategy="onthefly",
                                  extent_size=EXTENT, workflow=WORKFLOW,
                                  save_format="netCDF")
        conn = duckdb.connect(db_path, read_only=True)
        row = conn.execute(
            "SELECT run_id, crs_strategy, save_format FROM runs").fetchall()
        conn.close()
        print(f"  runs-Zeile: {row}")
        assert row == [(rid, "onthefly", "netCDF")], row
    finally:
        database.DB_PATH = old_db
    print("  OK: Spalte existiert im Schema-Setup und traegt den Wert.")


def test_bestehende_db_nachruesten(tmp: Path) -> None:
    """Bestehende DB ohne die Spalte: import_run ruestet sie nach.

    Die alte DB wird mit der database.py aus HEAD angelegt - die kennt die
    Spalte nicht. Das ist genau die Lage auf dem Server. (Ein nachtraegliches
    DROP COLUMN geht nicht: die Fremdschluessel von accuracy/band_statistics
    auf runs verhindern es.) import_run() ruft _ensure_run_extra_columns()
    vor jedem INSERT auf - ohne diesen Weg wuerde der Lauf an der
    Insert-Liste scheitern.
    """
    print("\n--- Test 6: bestehende DB ohne Spalte ---")
    old_db = database.DB_PATH
    db_path = str(tmp / "alt.duckdb")
    try:
        head = subprocess.run(["git", "show", "HEAD:database.py"],
                              cwd=ROOT, capture_output=True)
        assert head.returncode == 0, head.stderr.decode(errors="replace")
        head_path = tmp / "database_head.py"
        head_path.write_bytes(head.stdout)
        spec = importlib.util.spec_from_file_location("db_head", head_path)
        db_head = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(db_head)
        db_head.DB_PATH = db_path
        db_head.create_database()

        conn = duckdb.connect(db_path, read_only=True)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('runs')").fetchall()}
        conn.close()
        assert "save_format" not in cols, "Ausgangslage nicht hergestellt"
        print(f"  Ausgangslage: DB von HEAD:database.py angelegt, "
              f"runs hat {len(cols)} Spalten, save_format ist NICHT dabei")
        database.DB_PATH = db_path

        run_dir = tmp / "run_alt_onthefly"
        run_dir.mkdir()
        (run_dir / "results.json").write_text(json.dumps({
            "backend_url": "local-test", "backend_name": "test",
            "process_graph": "scenario_onthefly", "status": "success",
            "timestamp": "2026-08-28T09:00:00", "job_status_history": {},
        }))
        rid = database.import_run(str(run_dir), crs_strategy="onthefly",
                                  extent_size=EXTENT, workflow=WORKFLOW,
                                  save_format="netCDF")
        conn = duckdb.connect(db_path, read_only=True)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('runs')").fetchall()}
        row = conn.execute(
            "SELECT run_id, save_format FROM runs").fetchall()
        conn.close()
        assert "save_format" in cols, sorted(cols)
        assert row == [(rid, "netCDF")], row
        print(f"  nach import_run: Spalte wieder da, Zeile {row}")
    finally:
        database.DB_PATH = old_db
    print("  OK: aeltere DB wird beim naechsten Import nachgeruestet.")


def test_accuracy_skip_netcdf(tmp: Path) -> None:
    """netCDF-Ergebnisse: klare Meldung statt Absturz oder Fehldeutung."""
    print("\n--- Test 5: Accuracy-Check mit netCDF-Ausgabe ---")
    out = tmp / "outputs"
    ref = out / "run_20260827_100000_local_reference" / "step4_result"
    test = out / "run_20260827_100100_onthefly"
    ref.mkdir(parents=True)
    test.mkdir(parents=True)
    (ref / "openEO_2024-07-24Z.tif").write_bytes(b"x")   # Inhalt egal
    (test / "openEO.nc").write_bytes(b"x")               # netCDF-Ergebnis

    import io as _io
    from contextlib import redirect_stdout
    buf = _io.StringIO()
    with redirect_stdout(buf):
        result = rb.run_accuracy_check(
            str(out), REGION, test_strategy="onthefly",
            test_dir=test.parent if test.name == "step4_result" else test,
            reference_dir=ref.parent,
            reference_strategy="local_reference",
            extent_size=None, workflow=WORKFLOW)
    text = buf.getvalue()
    print("   " + text.strip().replace("\n", "\n   "))
    assert result is None, result
    assert "netCDF-Ausgabe statt GeoTIFF" in text, text
    assert "--save-format GTiff" in text, text
    assert "keine gemeinsamen TIF-Dateien" not in text, text
    print("\n  OK: uebersprungen mit eindeutiger Begruendung, kein Fehler.")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="save_format_"))
    print(f"Temp: {tmp}")
    try:
        test_gtiff_unveraendert(tmp)
        test_netcdf_knoten(tmp)
        test_fullpp_eigenstaendig(tmp)
        test_db_spalte(tmp)
        test_bestehende_db_nachruesten(tmp)
        test_accuracy_skip_netcdf(tmp)
        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

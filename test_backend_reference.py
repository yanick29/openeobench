#!/usr/bin/env python3
"""
test_backend_reference.py - Standalone-Tests fuer den Backend-Filter in der
Referenzzuordnung.

Befund: CDSE und Terrascope halten verschiedene Bestaende vor (16 vs 3
S2-Aufnahmen im selben Zeitfenster, DEM-Datum 2011-01-06 vs 2012-11-20,
Nodata -32768 vs 32767). Bis hierher suchte die Referenz ohne Ruecksicht
aufs Backend: Lauf 1170 (subtract, onthefly, Terrascope) wurde gegen eine
CDSE-Referenz gemessen -> MAE 50,41 / RMSE 1255,27, waehrend dieselbe
Konfiguration auf CDSE bei MAE 1,697 liegt.

Geprueft wird:
  1. Backend-Erkennung am Ordner, inklusive BESTEHENDER Referenzordner:
     deren Run-Root traegt "backend_url": "local", die echte URL steht in
     step1_s2_download/results.json.
  2. _find_latest_run_dir liefert je Backend die passende Referenz.
  3. Ohne passende Referenz: None statt Ausweichen aufs andere Backend.
  4. CDSE-Zuordnung unveraendert - auch fuer Ordner ganz ohne Marker.
  5. backfill_accuracy: Konfiguration und Skip-Begruendung tragen das
     Backend, Geschwister-Zeilen des anderen Backends werden verworfen.

Keine Backend-Aufrufe, keine DB, keine Rasterdatei - nur Ordnerstrukturen
mit JSON-Dateien in einem temporaeren Verzeichnis.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import backfill_accuracy as bf
import run_benchmark as rb

REGION = "berlin"
EXTENT = "medium"
WORKFLOW = "subtract"
CDSE = rb.BACKENDS["cdse"]["url"]
TERRA = rb.BACKENDS["terrascope"]["url"]


def _pg(dataset="dem", workflow=WORKFLOW):
    tpl = rb._load_bench_template(REGION, EXTENT)
    return rb._build_workflow_pg(tpl, workflow, region=REGION, dataset=dataset)


def _mk_run(root: Path, name: str, *, root_backend_url=None,
            download_backend_url=None, meta_backend=None,
            with_meta=True) -> Path:
    """Run-Ordner bauen, wie ihn die jeweilige Strategie hinterlaesst.

    root_backend_url     - backend_url in <dir>/results.json
    download_backend_url - backend_url in <dir>/step1_s2_download/results.json
                           (so sieht ein local_reference-Ordner aus)
    meta_backend         - "backend" in run_meta.json (neu geschriebene Runs)
    with_meta            - run_meta.json ueberhaupt anlegen
    """
    d = root / name
    d.mkdir(parents=True)
    (d / "processgraph.json").write_text(json.dumps(_pg()))
    (d / "scenario_onthefly.json").write_text(json.dumps(_pg()))
    if with_meta:
        extra = {"backend": meta_backend} if meta_backend else {}
        rb._write_run_meta(d, 10.0, dataset="dem", **extra)
    if root_backend_url is not None:
        (d / "results.json").write_text(json.dumps(
            {"backend_url": root_backend_url, "backend_name": "x",
             "timestamp": "2026-08-29T10:00:00", "status": "success"}))
    if download_backend_url is not None:
        sub = d / "step1_s2_download"
        sub.mkdir()
        (sub / "results.json").write_text(json.dumps(
            {"backend_url": download_backend_url, "backend_name": "x",
             "timestamp": "2026-08-29T10:00:01", "status": "success"}))
    for sub in ("step4_result",) if name.endswith("_local_reference") else ():
        (d / sub).mkdir(exist_ok=True)
        (d / sub / "openEO_2024-08-05Z.tif").write_bytes(b"x")
    return d


def test_erkennung(tmp: Path) -> None:
    print("\n--- Test 1: Backend-Erkennung am Ordner ---")
    root = tmp / "erkennung"
    root.mkdir()
    faelle = [
        ("run_1_onthefly", dict(root_backend_url=CDSE), "cdse",
         "Messlauf, results.json im Root"),
        ("run_2_onthefly", dict(root_backend_url=TERRA), "terrascope",
         "Messlauf auf Terrascope"),
        ("run_3_local_reference",
         dict(root_backend_url="local", download_backend_url=CDSE), "cdse",
         "Referenz: Root sagt 'local', Download verraet CDSE"),
        ("run_4_local_reference",
         dict(root_backend_url="local", download_backend_url=TERRA),
         "terrascope",
         "Referenz: Root sagt 'local', Download verraet Terrascope"),
        ("run_5_onthefly", dict(meta_backend="terrascope"), "terrascope",
         "run_meta.json (kuenftige Laeufe)"),
        ("run_6_onthefly", dict(with_meta=False), None,
         "Altbestand ohne jeden Marker"),
    ]
    for name, kwargs, erwartet, label in faelle:
        d = _mk_run(root, name, **kwargs)
        got = rb._detect_folder_backend(d)
        print(f"  {label:<48} -> {got}")
        assert got == erwartet, (name, got, erwartet)

    # Unbekannt zaehlt nur fuer das historische Default.
    alt = root / "run_6_onthefly"
    assert rb._folder_matches_backend(alt, "cdse") is True
    assert rb._folder_matches_backend(alt, "terrascope") is False
    print("  Altbestand ohne Marker: passt zu cdse, nicht zu terrascope "
          f"(DEFAULT_BACKEND={rb.DEFAULT_BACKEND})")
    print("  OK")


def _referenz_paar(root: Path) -> None:
    """Je eine local_reference pro Backend, gleiche Konfiguration."""
    _mk_run(root, "run_20260819_163251_local_reference",
            root_backend_url="local", download_backend_url=CDSE)
    _mk_run(root, "run_20260828_120000_local_reference",
            root_backend_url="local", download_backend_url=TERRA)


def test_suche(tmp: Path) -> None:
    print("\n--- Test 2: _find_latest_run_dir waehlt je Backend ---")
    root = tmp / "suche"
    root.mkdir()
    _referenz_paar(root)
    suffix = rb._ACCURACY_LAYOUT["local_reference"][0]
    for backend, erwartet in (("cdse", "run_20260819_163251_local_reference"),
                              ("terrascope",
                               "run_20260828_120000_local_reference")):
        got = rb._find_latest_run_dir(str(root), suffix, REGION,
                                      extent_size=EXTENT, workflow=WORKFLOW,
                                      resolution=10.0, dataset="dem",
                                      backend=backend)
        print(f"  backend={backend:<11} -> {got.name if got else None}")
        assert got is not None and got.name == erwartet, (backend, got)

    # Ohne backend-Angabe wie bisher: neuester Treffer, kein Filter.
    ohne = rb._find_latest_run_dir(str(root), suffix, REGION,
                                   extent_size=EXTENT, workflow=WORKFLOW,
                                   resolution=10.0, dataset="dem")
    assert ohne is not None
    print(f"  ohne backend-Angabe -> {ohne.name} (Verhalten wie vorher)")
    print("  OK")


def test_keine_passende_referenz(tmp: Path) -> None:
    print("\n--- Test 3: kein Ausweichen aufs andere Backend ---")
    root = tmp / "nur_cdse"
    root.mkdir()
    _mk_run(root, "run_20260819_163251_local_reference",
            root_backend_url="local", download_backend_url=CDSE)
    suffix = rb._ACCURACY_LAYOUT["local_reference"][0]
    cdse = rb._find_latest_run_dir(str(root), suffix, REGION,
                                   extent_size=EXTENT, workflow=WORKFLOW,
                                   resolution=10.0, dataset="dem",
                                   backend="cdse")
    terra = rb._find_latest_run_dir(str(root), suffix, REGION,
                                    extent_size=EXTENT, workflow=WORKFLOW,
                                    resolution=10.0, dataset="dem",
                                    backend="terrascope")
    print(f"  nur eine CDSE-Referenz vorhanden: cdse -> {cdse.name}, "
          f"terrascope -> {terra}")
    assert cdse is not None
    assert terra is None, "Terrascope hat sich die CDSE-Referenz geschnappt"

    # Und der Check schreibt dann nichts, sondern meldet es.
    import io as _io
    from contextlib import redirect_stdout
    buf = _io.StringIO()
    test_dir = _mk_run(root, "run_20260828_130000_onthefly",
                       root_backend_url=TERRA)
    with redirect_stdout(buf):
        res = rb.run_accuracy_check(
            str(root), REGION, test_strategy="onthefly", test_dir=test_dir,
            reference_strategy="local_reference", extent_size=EXTENT,
            workflow=WORKFLOW, resolution=10.0, dataset="dem",
            backend="terrascope")
    text = buf.getvalue()
    print("   " + text.strip().replace("\n", "\n   "))
    assert res is None
    assert "Backend: terrascope" in text, text
    assert "auf Backend 'terrascope' gefunden" in text, text
    assert "KEINE andere Strategie als Referenz" in text, text
    print("\n  OK: uebersprungen mit Backend in der Meldung.")


def test_cdse_unveraendert(tmp: Path) -> None:
    """Reine CDSE-Welt, teils ohne Marker: Zuordnung wie bisher."""
    print("\n--- Test 4: CDSE-Zuordnung unveraendert ---")
    root = tmp / "cdse_only"
    root.mkdir()
    alt = _mk_run(root, "run_20260101_100000_local_reference",
                  with_meta=False)          # Altbestand, kein Marker
    (alt / "step4_result").mkdir(exist_ok=True)
    (alt / "step4_result" / "openEO_2024-08-05Z.tif").write_bytes(b"x")
    suffix = rb._ACCURACY_LAYOUT["local_reference"][0]
    mit = rb._find_latest_run_dir(str(root), suffix, REGION,
                                  extent_size=EXTENT, workflow=WORKFLOW,
                                  resolution=10.0, dataset="dem",
                                  backend="cdse")
    ohne = rb._find_latest_run_dir(str(root), suffix, REGION,
                                   extent_size=EXTENT, workflow=WORKFLOW,
                                   resolution=10.0, dataset="dem")
    print(f"  Altbestand ohne Marker: mit backend='cdse' -> {mit.name if mit else None}, "
          f"ohne Filter -> {ohne.name if ohne else None}")
    assert mit is not None and mit == ohne
    print("  OK: derselbe Ordner, der Filter aendert fuer CDSE nichts.")


def test_backfill(tmp: Path) -> None:
    print("\n--- Test 5: backfill_accuracy ---")
    root = tmp / "backfill"
    root.mkdir()
    _referenz_paar(root)
    terra_run = _mk_run(root, "run_20260828_130000_onthefly",
                        root_backend_url=TERRA)
    cdse_run = _mk_run(root, "run_20260819_170000_onthefly",
                       root_backend_url=CDSE)

    cfg_t = bf.run_config({}, terra_run)
    cfg_c = bf.run_config({}, cdse_run)
    print(f"  Terrascope-Lauf: Backend={cfg_t['backend']}")
    print(f"  CDSE-Lauf      : Backend={cfg_c['backend']}")
    assert cfg_t["backend"] == "terrascope"
    assert cfg_c["backend"] == "cdse"
    # Backend gehoert in den Vergleichsschluessel.
    assert bf._config_key(cfg_t, "onthefly") != bf._config_key(cfg_c, "onthefly")

    for cfg, erwartet in ((cfg_t, "run_20260828_120000_local_reference"),
                          (cfg_c, "run_20260819_163251_local_reference")):
        ref_dir, note = bf.resolve_reference(str(root), cfg, None)
        print(f"  Referenz fuer {cfg['backend']:<11} -> "
              f"{ref_dir.name if ref_dir else None}")
        assert ref_dir is not None and ref_dir.name == erwartet, (cfg, ref_dir)

    # Geschwister-Zeile des ANDEREN Backends wird nicht uebernommen.
    fremd = root / "run_20260819_163251_local_reference"     # CDSE
    got = bf.sibling_reference_dir(
        [(1, str(fremd))], cfg_t, "onthefly",
        index={1: ("onthefly", terra_run)},
        config_cache={1: cfg_t}, rows_by_id={1: {}})
    print(f"  Geschwister-Zeile zeigt auf CDSE-Referenz, "
          f"Terrascope-Lauf uebernimmt sie: {got}")
    assert got is None, got

    # Ohne Referenz fuers Backend: Skip-Text nennt das Backend.
    nur_cdse = tmp / "backfill_nur_cdse"
    nur_cdse.mkdir()
    _mk_run(nur_cdse, "run_20260819_163251_local_reference",
            root_backend_url="local", download_backend_url=CDSE)
    cfg = bf.run_config({}, _mk_run(nur_cdse, "run_20260828_130000_onthefly",
                                    root_backend_url=TERRA))
    ref_dir, _note = bf.resolve_reference(str(nur_cdse), cfg, None)
    assert ref_dir is None, ref_dir
    print("  ohne Terrascope-Referenz -> resolve_reference liefert None")
    print("  OK")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="backend_ref_"))
    print(f"Temp: {tmp}")
    try:
        test_erkennung(tmp)
        test_suche(tmp)
        test_keine_passende_referenz(tmp)
        test_cdse_unveraendert(tmp)
        test_backfill(tmp)
        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

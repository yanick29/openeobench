#!/usr/bin/env python3
"""
test_cleanup.py - Standalone-Tests fuer:
  1. Plattenplatz-Precheck (--min-free-gb)
  2. Cleanup nach Accuracy-Check (--cleanup-after-accuracy, --dry-run-cleanup)
     inklusive local_reference-Abhaengigkeits-Regel.

Alle Tests laufen in einem tempoeraeren Ordner mit einer tempoeraeren DuckDB,
damit die produktive benchmark_results.duckdb nicht beruehrt wird.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import database
import run_benchmark
from run_benchmark import (
    check_disk_space,
    cleanup_after_accuracy,
    delete_run_tifs,
    _run_has_accuracy,
    _accuracy_test_run_ids_for_reference,
    _persist_accuracy,
)


def _mk_run_dir(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    # Metadaten die NICHT geloescht werden duerfen
    (d / "results.json").write_text('{"status": "success"}')
    (d / "scenario.json").write_text('{}')
    # TIFs die geloescht werden sollen
    for sub, count in [("", 1), ("step3_main", 2), ("step2_reproj", 1)]:
        sd = d / sub if sub else d
        sd.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (sd / f"openEO_2024-06-{i+1:02d}Z.tif").write_bytes(b"X" * 4096)
    return d


def _fake_run_row(conn, run_id: int, strategy: str, region: str,
                   extent_size: str, workflow: str) -> None:
    # Minimalste runs-Zeile - reicht fuer die Cleanup-Logik.
    conn.execute(
        "INSERT INTO runs (run_id, crs_strategy, region, extent_size, workflow, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, strategy, region, extent_size, workflow, "success"),
    )


def _ensure_region_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info('runs')").fetchall()}
    if "region" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN region TEXT")


def test_disk_precheck() -> None:
    print("\n" + "=" * 60)
    print("TEST 1: disk-space precheck")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="cleanup_test_"))
    try:
        # OK: min_free_gb=0.001 sollte immer durchgehen
        check_disk_space(str(tmp), min_free_gb=0.001, context="context_ok")
        print("[test1a] OK: 0.001 GB Schwelle passiert (freier Platz > 1 MB)")

        # Fail: min_free_gb=10**9 - jede reale Platte hat weniger.
        try:
            check_disk_space(str(tmp), min_free_gb=1e9,
                             context="context_fail")
        except RuntimeError as exc:
            msg = str(exc)
            assert "Zu wenig freier Speicher" in msg, msg
            assert "context_fail" in msg, msg
            print(f"[test1b] OK: RuntimeError bei absurd hoher Schwelle: {msg[:120]}...")
        else:
            raise AssertionError("check_disk_space haette RuntimeError werfen muessen")

        # min_free_gb=0 -> deaktiviert
        check_disk_space(str(tmp), min_free_gb=0, context="deaktiviert")
        print("[test1c] OK: min_free_gb=0 deaktiviert die Pruefung")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_flow() -> None:
    print("\n" + "=" * 60)
    print("TEST 2: cleanup nach Accuracy - CDSE-Run + local_reference-Dependency")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="cleanup_test_"))
    outdir = tmp / "outputs"
    outdir.mkdir()
    fake_db = tmp / "test.duckdb"

    # Alle DB-Zugriffe in run_benchmark gehen ueber database.DB_PATH
    orig_db_path = database.DB_PATH
    database.DB_PATH = str(fake_db)

    try:
        # DB-Schema anlegen
        database.create_database()
        conn = duckdb.connect(str(fake_db))
        _ensure_region_column(conn)

        # Session: 2 CDSE-Runs (onthefly, local_pp) + 1 local_reference.
        # Alle mit Region=berlin, Extent=medium, Workflow=merge_add.
        _fake_run_row(conn, 1, "onthefly",            "berlin", "medium", "merge_add")
        _fake_run_row(conn, 2, "local_preprocessing", "berlin", "medium", "merge_add")
        _fake_run_row(conn, 3, "local_reference",     "berlin", "medium", "merge_add")
        conn.commit()
        conn.close()

        # Run-Ordner + Fake-TIFs
        run1_dir = _mk_run_dir(outdir, "run_20260101_onthefly")
        run2_dir = _mk_run_dir(outdir, "run_20260101_local_pp")
        run3_dir = _mk_run_dir(outdir, "run_20260101_local_reference")

        session_results = [
            {"strategy": "onthefly",            "run_id": 1, "outdir": str(run1_dir),
             "status": "success", "region": "berlin", "extent_size": "medium",
             "workflow": "merge_add"},
            {"strategy": "local_preprocessing", "run_id": 2, "outdir": str(run2_dir),
             "status": "success", "region": "berlin", "extent_size": "medium",
             "workflow": "merge_add"},
            {"strategy": "local_reference",     "run_id": 3, "outdir": str(run3_dir),
             "status": "success", "region": "berlin", "extent_size": "medium",
             "workflow": "merge_add"},
        ]

        # Ausgangszustand: NULL Accuracy-Eintraege.
        assert _run_has_accuracy(1) is False
        assert _accuracy_test_run_ids_for_reference(3) == set()
        print("[test2a] OK: keine Accuracy-Eintraege -> _run_has_accuracy(1)=False")

        # PHASE 1: Dry-run cleanup mit 0 Accuracy-Eintraegen. Nichts darf
        # geloescht werden, alle TIFs muessen erhalten bleiben.
        print("\n--- Phase 1: dry-run OHNE Accuracy-Eintraege ---")
        cleanup_after_accuracy(session_results, output_dir=str(outdir),
                               dry_run=True)
        # Nichts geloescht (dry_run + kein Accuracy)
        for d in (run1_dir, run2_dir, run3_dir):
            tifs_left = list(d.rglob("*.tif"))
            assert len(tifs_left) == 4, (
                f"{d.name}: {len(tifs_left)} TIFs uebrig, erwartet 4"
            )
        print("[test2b] OK: alle 3 Runs komplett unberuehrt (kein Accuracy)")

        # PHASE 2: Accuracy fuer run_id=1 nachtragen (onthefly gegen
        # local_reference id=3). run_id=2 hat weiterhin KEINEN Eintrag.
        # dry-run zeigt run_id=1 als loeschbar, run_id=2 skip, run_id=3
        # skip (dep 2 fehlt).
        print("\n--- Phase 2: Accuracy nur fuer run_id=1 ---")
        _persist_accuracy(1, mae=0.5, rmse=0.7,
                          reference_file=str(run3_dir),
                          reference_run_id=3)
        assert _run_has_accuracy(1) is True
        assert _run_has_accuracy(2) is False
        assert _accuracy_test_run_ids_for_reference(3) == {1}
        print("[test2c] OK: _accuracy_test_run_ids_for_reference(3) = {1}")

        cleanup_after_accuracy(session_results, output_dir=str(outdir),
                               dry_run=True)
        # dry-run: nichts wirklich geloescht
        for d in (run1_dir, run2_dir, run3_dir):
            tifs_left = list(d.rglob("*.tif"))
            assert len(tifs_left) == 4, (
                f"{d.name}: dry-run hat trotzdem geloescht"
            )
        print("[test2d] OK: dry-run hat nichts geloescht")

        # PHASE 3: Accuracy fuer run_id=2 nachtragen. Jetzt sind BEIDE
        # Abhaengigen fertig -> live cleanup darf alles loeschen.
        print("\n--- Phase 3: Accuracy fuer run_id=2 nachtragen ---")
        _persist_accuracy(2, mae=0.3, rmse=0.6,
                          reference_file=str(run3_dir),
                          reference_run_id=3)
        deps = _accuracy_test_run_ids_for_reference(3)
        assert deps == {1, 2}, deps
        print(f"[test2e] OK: alle 2 Abhaengigen jetzt in Accuracy: {deps}")

        # LIVE cleanup
        print("\n--- Phase 4: LIVE cleanup ---")
        cleanup_after_accuracy(session_results, output_dir=str(outdir),
                               dry_run=False)
        for d in (run1_dir, run2_dir, run3_dir):
            tifs_left = list(d.rglob("*.tif"))
            assert len(tifs_left) == 0, (
                f"{d.name}: {len(tifs_left)} TIFs uebrig, erwartet 0"
            )
            # Metadaten muessen bleiben
            assert (d / "results.json").exists(), f"{d.name}: results.json weg!"
            assert (d / "scenario.json").exists(), f"{d.name}: scenario.json weg!"
        print("[test2f] OK: alle TIFs weg, results.json + scenario.json erhalten")

        # PHASE 5: Zweiter Aufruf -> alle Kandidaten leer, kein Fehler.
        print("\n--- Phase 5: Cleanup nochmal -> idempotent ---")
        cleanup_after_accuracy(session_results, output_dir=str(outdir),
                               dry_run=False)
        print("[test2g] OK: zweiter Aufruf laeuft ohne Fehler durch")

    finally:
        database.DB_PATH = orig_db_path
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_blocks_when_dep_missing() -> None:
    """Zusatz-Sicherheitscheck: wenn run_id=2 (Abhaengiger) FEHLT im
    Accuracy-Table, muss local_reference (run_id=3) unberuehrt bleiben,
    auch wenn run_id=1 fertig ist."""
    print("\n" + "=" * 60)
    print("TEST 3: local_reference bleibt erhalten wenn dep fehlt")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="cleanup_test_"))
    outdir = tmp / "outputs"
    outdir.mkdir()
    fake_db = tmp / "test.duckdb"

    orig_db_path = database.DB_PATH
    database.DB_PATH = str(fake_db)

    try:
        database.create_database()
        conn = duckdb.connect(str(fake_db))
        _ensure_region_column(conn)
        _fake_run_row(conn, 1, "onthefly",            "berlin", "medium", "merge_add")
        _fake_run_row(conn, 2, "local_preprocessing", "berlin", "medium", "merge_add")
        _fake_run_row(conn, 3, "local_reference",     "berlin", "medium", "merge_add")
        conn.commit()
        conn.close()

        run1_dir = _mk_run_dir(outdir, "run_a_onthefly")
        run2_dir = _mk_run_dir(outdir, "run_b_local_pp")
        run3_dir = _mk_run_dir(outdir, "run_c_local_ref")

        session = [
            {"strategy": "onthefly",            "run_id": 1, "outdir": str(run1_dir),
             "status": "success", "region": "berlin", "extent_size": "medium",
             "workflow": "merge_add"},
            {"strategy": "local_preprocessing", "run_id": 2, "outdir": str(run2_dir),
             "status": "success", "region": "berlin", "extent_size": "medium",
             "workflow": "merge_add"},
            {"strategy": "local_reference",     "run_id": 3, "outdir": str(run3_dir),
             "status": "success", "region": "berlin", "extent_size": "medium",
             "workflow": "merge_add"},
        ]

        # NUR run_id=1 hat Accuracy. run_id=2 fehlt.
        _persist_accuracy(1, 0.1, 0.2, str(run3_dir), reference_run_id=3)

        cleanup_after_accuracy(session, output_dir=str(outdir), dry_run=False)

        # run_id=1 (mit Accuracy): geloescht
        assert list(run1_dir.rglob("*.tif")) == [], "run1 haette geloescht werden muessen"
        # run_id=2 (ohne Accuracy): erhalten
        assert len(list(run2_dir.rglob("*.tif"))) == 4, "run2 haette erhalten bleiben muessen"
        # run_id=3 (local_reference, dep 2 fehlt): erhalten
        assert len(list(run3_dir.rglob("*.tif"))) == 4, "local_reference haette erhalten bleiben muessen"

        print("[test3] OK: run_id=1 geloescht, run_id=2 und local_reference (3) intakt")
    finally:
        database.DB_PATH = orig_db_path
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_disk_precheck()
    test_cleanup_flow()
    test_cleanup_blocks_when_dep_missing()
    print("\n" + "=" * 60)
    print("ALLE TESTS OK")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

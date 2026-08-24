#!/usr/bin/env python3
"""
test_backfill_accuracy.py - Standalone-Tests fuer:
  1. den Fix in run_benchmark: der Accuracy-Check erfasst ALLE Laeufe eines
     Aufrufs, nicht nur den letzten (_accuracy_targets_from_session +
     run_accuracy_check(test_dir=...)).
  2. den Nachtrag ueber backfill_accuracy.py (Trockenlauf, echter Lauf,
     kein Ueberschreiben ohne --force, zeitreduzierender Workflow,
     kategorialer Datensatz).
  3. den Nachweis, dass Prozessgraphen und Auswahl-Logik sonst unveraendert
     bleiben (Vergleich gegen die Version aus HEAD).

Alles laeuft in einem temporaeren Ordner mit temporaerer DuckDB - die
produktive benchmark_results.duckdb wird nicht angefasst. Es werden keine
Backend-Aufrufe gemacht; die Raster werden lokal erzeugt.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import duckdb
import numpy as np
import rasterio
from rasterio.transform import Affine

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import database
import run_benchmark as rb

BERLIN_EPSG = 32633
# Beliebiges, aber festes UTM-Fenster - fuer den Vergleich zaehlt nur, dass
# Referenz und Test auf demselben Gitter liegen (align_rasters macht dann
# einen Early-Exit ohne Resampling).
BASE_TRANSFORM = Affine(10.0, 0.0, 380000.0, 0.0, -10.0, 5820000.0)
SHAPE = (20, 20)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _write_tif(path: Path, array: np.ndarray, nodata=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=array.shape[0],
        width=array.shape[1], count=1, dtype=array.dtype,
        crs=f"EPSG:{BERLIN_EPSG}", transform=BASE_TRANSFORM,
        nodata=nodata,
    ) as dst:
        dst.write(array, 1)


def _mk_run_dir(out_root: Path, strategy: str, stamp: str, pg: dict,
                resolution: float, dataset: str, tifs: dict,
                timestamp: str, mtime: float) -> Path:
    """Einen Run-Ordner bauen, wie ihn run_benchmark hinterlassen wuerde.

    tifs: {Dateiname: numpy-Array} - landen im Ergebnis-Unterordner der
    Strategie (onthefly: Root, local_reference: step4_result, local_pp:
    step3_main).
    """
    suffix = rb._ACCURACY_LAYOUT[strategy][0]
    run_dir = out_root / f"run_{stamp}_{suffix}"
    run_dir.mkdir(parents=True)

    # Szenario/Prozessgraph: Quelle fuer Region-, Extent- und
    # Workflow-Erkennung.
    (run_dir / "processgraph.json").write_text(json.dumps(pg, indent=1))
    if strategy == "onthefly":
        (run_dir / "scenario_onthefly.json").write_text(json.dumps(pg, indent=1))
    rb._write_run_meta(run_dir, resolution, dataset)

    nodata = rb.DATASETS[dataset].get("nodata") if dataset == "landcover" else None
    for name, arr in tifs.items():
        _write_tif(rb._tif_dir(run_dir, strategy) / name, arr, nodata=nodata)

    # results.json - dort, wo import_run sie erwartet.
    res_dir = run_dir if strategy != "local_preprocessing" else run_dir / "step3_main"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "results.json").write_text(json.dumps({
        "backend_url": "local-test", "backend_name": "test",
        "process_graph": f"test_{strategy}", "status": "success",
        "timestamp": timestamp, "job_status_history": {},
        "total_time": 1.0,
    }, indent=1))

    os.utime(run_dir, (mtime, mtime))
    return run_dir


def _import(run_dir: Path, strategy: str, extent_size: str, workflow: str,
            resolution: float, dataset: str) -> int:
    res_dir = run_dir if strategy != "local_preprocessing" else run_dir / "step3_main"
    return database.import_run(
        str(res_dir), crs_strategy=strategy, run_type="cold",
        extent_size=extent_size, workflow=workflow,
        local_resampling="nearest", resolution_m=resolution, dataset=dataset)


class Fixture:
    """Ein Block: n onthefly-Wiederholungen + eine local_reference dazu."""

    def __init__(self, out_root: Path, region: str, extent_size: str,
                 workflow: str, dataset: str, resolution: float = 10.0):
        self.out_root = out_root
        self.region = region
        self.extent_size = extent_size
        self.workflow = workflow
        self.dataset = dataset
        self.resolution = resolution
        self.template = rb._load_bench_template(region, extent_size)
        self.pg = rb._build_workflow_pg(self.template, workflow, region=region,
                                        resolution=resolution, dataset=dataset)
        self.test_runs = []       # [(run_id, Path)]
        self.reference = None     # (run_id, Path)

    def _arrays(self, kind: str) -> dict:
        """Deterministische Raster. reference/test unterscheiden sich fest."""
        dated = ["openEO_2024-06-05Z.tif", "openEO_2024-06-15Z.tif"]
        if self.dataset == "landcover":
            base = np.full(SHAPE, 10, dtype="uint8")
            base[:, :5] = 20
            test = base.copy()
            test[0, :4] = 30          # 4 von 400 Pixeln abweichend
            arr = base if kind == "reference" else test
            return {name: arr for name in dated}
        base = np.fromfunction(lambda y, x: (y * 20 + x) / 10.0, SHAPE,
                               dtype="float32").astype("float32")
        arr = base if kind == "reference" else (base + np.float32(0.5))
        if self.workflow in rb.TIME_REDUCING_WORKFLOWS and kind == "test":
            # Zeitreduzierender Workflow: das Backend schreibt EINE
            # undatierte Datei, die Referenz behaelt die Datumsnamen.
            return {"openEO.tif": arr}
        return {name: arr for name in dated}

    def build(self, n_repeats: int, t0: datetime, mtime0: float,
              with_reference: bool = True) -> None:
        for i in range(n_repeats):
            stamp = (t0 + timedelta(minutes=i)).strftime("%Y%m%d_%H%M%S")
            ts = (t0 + timedelta(minutes=i)).isoformat()
            d = _mk_run_dir(self.out_root, "onthefly", stamp, self.pg,
                            self.resolution, self.dataset,
                            self._arrays("test"), ts, mtime0 + i * 60)
            rid = _import(d, "onthefly", self.extent_size, self.workflow,
                          self.resolution, self.dataset)
            self.test_runs.append((rid, d))
        if not with_reference:
            # Referenzlauf gescheitert (im echten Fall: CDSE-Warteschlangen-
            # Timeout) - es gibt Messlaeufe, aber keine Ground-Truth.
            return
        stamp = (t0 + timedelta(minutes=n_repeats)).strftime("%Y%m%d_%H%M%S")
        ts = (t0 + timedelta(minutes=n_repeats)).isoformat()
        d = _mk_run_dir(self.out_root, "local_reference", stamp, self.pg,
                        self.resolution, self.dataset,
                        self._arrays("reference"), ts,
                        mtime0 + n_repeats * 60)
        rid = _import(d, "local_reference", self.extent_size, self.workflow,
                      self.resolution, self.dataset)
        self.reference = (rid, d)


def _accuracy_rows(db_path: str) -> list:
    """(run_id, mae, rmse, agreement_pct, metric_kind, reference_run_id,
    reference_file) je Zeile.

    Die kategorialen Spalten legt erst _persist_accuracy beim ersten
    Schreiben an (idempotente Migration) - in einer DB ohne jeden Eintrag
    fehlen sie noch. Deshalb wird nur abgefragt, was existiert.
    """
    conn = duckdb.connect(db_path, read_only=True)
    have = {r[1] for r in conn.execute(
        "PRAGMA table_info('accuracy')").fetchall()}
    cols = ["run_id", "mae", "rmse", "agreement_pct", "metric_kind",
            "reference_run_id", "reference_file"]
    sel = [c if c in have else "NULL" for c in cols]
    rows = conn.execute(
        f"SELECT {', '.join(sel)} FROM accuracy ORDER BY run_id").fetchall()
    conn.close()
    return rows


def _old_style_check(out_root: Path, fx: Fixture) -> None:
    """Der Aufruf, wie ihn main() VOR dem Fix gemacht haette: genau einmal,
    mit der run_id der letzten Wiederholung und ohne Ordner-Vorgabe (also
    _find_latest_run_dir -> letzter Ordner)."""
    rb.run_accuracy_check(
        str(out_root), fx.region, test_strategy="onthefly",
        test_run_id=fx.test_runs[-1][0], extent_size=fx.extent_size,
        workflow=fx.workflow, resampling_method="nearest",
        reference_strategy="local_reference", resolution=fx.resolution,
        dataset=fx.dataset)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_session_targets() -> None:
    """Teil 1: alle Wiederholungen werden erfasst, nicht nur die letzte."""
    print("\n--- Test 1: _accuracy_targets_from_session ---")
    session = [
        {"strategy": "onthefly", "status": "success", "run_id": 880,
         "outdir": "outputs/run_a_onthefly"},
        {"strategy": "onthefly", "status": "success", "run_id": 881,
         "outdir": "outputs/run_b_onthefly"},
        {"strategy": "onthefly", "status": "success", "run_id": 882,
         "outdir": "outputs/run_c_onthefly"},
        {"strategy": "onthefly", "status": "error", "run_id": None,
         "outdir": "outputs/run_d_onthefly"},
        {"strategy": "local_pp_cached", "status": "success", "run_id": 883,
         "outdir": "outputs/run_e_local_pp"},
    ]
    # alte Auswahl (ueberschreibende Schleife) - zum Vergleich nachgebaut
    old = {}
    for r in session:
        if r["status"] != "success" or r["run_id"] is None:
            continue
        s = r["strategy"]
        if s == "local_pp_cached":
            s = "local_preprocessing"
        old[s] = r["run_id"]
    print(f"  alt (je Strategie genau eine run_id): {old}")

    new = rb._accuracy_targets_from_session(
        session, ("onthefly", "local_preprocessing", "full_preprocessing"))
    print(f"  neu: {[(s, rid) for s, rid, _ in new]}")
    assert [rid for _, rid, _ in new] == [880, 881, 882, 883], new
    assert new[3][0] == "local_preprocessing", "Alias nicht normalisiert"
    # Fehlgeschlagene Laeufe bleiben aussen vor.
    assert all(rid is not None for _, rid, _ in new)
    # Die alte Auswahl haette genau 2 Eintraege gehabt (letzte je Strategie).
    assert len(old) == 2 and len(new) == 4
    print("  OK: 4 Ziele statt 2 - jede Wiederholung bekommt einen Vergleich.")


def test_process_graph_unchanged() -> None:
    """Teil 1: der Prozessgraph ist bitgleich zur Version aus HEAD."""
    print("\n--- Test 2: Prozessgraphen unveraendert (HEAD vs Arbeitsstand) ---")
    with tempfile.TemporaryDirectory() as tmp:
        old_path = Path(tmp) / "run_benchmark_head.py"
        head = subprocess.run(["git", "show", "HEAD:run_benchmark.py"],
                              cwd=ROOT, capture_output=True)
        assert head.returncode == 0, head.stderr.decode(errors="replace")
        old_path.write_bytes(head.stdout)
        spec = importlib.util.spec_from_file_location("rb_head", old_path)
        rb_head = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rb_head)

    n = 0
    for region in ("berlin", "wien"):
        for extent in ("small", "medium"):
            tpl_old = rb_head._load_bench_template(region, extent)
            tpl_new = rb._load_bench_template(region, extent)
            assert tpl_old == tpl_new
            for wf in rb.WORKFLOWS:
                ds = ("landcover" if wf in rb.CATEGORICAL_WORKFLOWS else "dem")
                pg_old = rb_head._build_workflow_pg(
                    rb_head._load_bench_template(region, extent), wf,
                    region=region, dataset=ds)
                pg_new = rb._build_workflow_pg(
                    rb._load_bench_template(region, extent), wf,
                    region=region, dataset=ds)
                assert json.dumps(pg_old, sort_keys=True) == \
                       json.dumps(pg_new, sort_keys=True), \
                       f"PG weicht ab: {region}/{extent}/{wf}"
                n += 1
    print(f"  OK: {n} Prozessgraphen identisch "
          f"(2 Regionen x 2 Extents x {len(rb.WORKFLOWS)} Workflows).")


def test_single_run_equivalence(tmp_root: Path) -> None:
    """Teil 1: mit genau EINEM Lauf liefert der neue Aufruf (test_dir gesetzt)
    exakt dasselbe wie der alte (Auswahl per _find_latest_run_dir)."""
    print("\n--- Test 3: identisches Ergebnis bei einem einzelnen Lauf ---")
    out_root = tmp_root / "out_equiv"
    out_root.mkdir()
    db_path = str(tmp_root / "equiv.duckdb")
    database.DB_PATH = db_path
    database.create_database()

    fx = Fixture(out_root, "berlin", "small", "merge_add", "dem")
    fx.build(1, datetime(2026, 3, 1, 8, 0, 0), 1_760_000_000.0)

    old = rb.run_accuracy_check(
        str(out_root), "berlin", test_strategy="onthefly",
        test_run_id=fx.test_runs[0][0], extent_size="small",
        workflow="merge_add", reference_strategy="local_reference",
        resolution=10.0, dataset="dem")
    new = rb.run_accuracy_check(
        str(out_root), "berlin", test_strategy="onthefly",
        test_run_id=fx.test_runs[0][0], test_dir=fx.test_runs[0][1],
        extent_size="small", workflow="merge_add",
        reference_strategy="local_reference", resolution=10.0, dataset="dem")
    for key in ("mae", "rmse", "run_id", "reference_run_id", "test_dir",
                "reference_dir", "n_dates", "valid_pixels"):
        assert old[key] == new[key], f"{key}: {old[key]!r} != {new[key]!r}"
    print(f"  OK: identisch (MAE={new['mae']:.6f}, test_dir="
          f"{Path(new['test_dir']).name}).")


def test_main_reference_check(tmp_root: Path) -> None:
    """Teil 1 end-to-end: run_benchmark.main() mit --repeat 3 schreibt drei
    accuracy-Zeilen statt einer. Die Strategie-Runner sind ersetzt - es geht
    nur um den Ablauf nach print_summary(), kein Backend-Aufruf."""
    print("\n--- Test 4: main() --repeat 3 --reference-check ---")
    out_root = tmp_root / "out_main"
    out_root.mkdir()
    db_path = str(tmp_root / "main.duckdb")
    database.DB_PATH = db_path
    database.create_database()

    fx = Fixture(out_root, "berlin", "small", "merge_add", "dem")
    fx.build(3, datetime(2026, 5, 1, 7, 0, 0), 1_780_000_000.0)
    prebuilt = [
        {"strategy": "onthefly", "repeat": i + 1, "run_type": "cold",
         "status": "success", "preprocessing_time": None, "total_time": 1.0,
         "run_id": rid, "outdir": str(d)}
        for i, (rid, d) in enumerate(fx.test_runs)
    ]

    def fake_runner(args, repeat_idx):
        return dict(prebuilt[repeat_idx])

    argv = ["run_benchmark.py", "--strategy", "onthefly", "--region", "berlin",
            "--extent-size", "small", "--workflow", "merge_add",
            "--repeat", "3", "--reference-check", "--min-free-gb", "0",
            "--output-dir", str(out_root)]
    with patch.object(rb, "run_strategy_onthefly", fake_runner), \
            patch.object(sys, "argv", argv):
        rb.main()

    rows = _accuracy_rows(db_path)
    ids = [r[0] for r in rows]
    print(f"  accuracy-Zeilen nach main(): {ids}")
    assert ids == [rid for rid, _ in fx.test_runs], ids
    assert len({round(r[1], 9) for r in rows}) == 1, "Werte weichen ab"
    assert {r[5] for r in rows} == {fx.reference[0]}
    print("  OK: alle drei Wiederholungen haben ihren eigenen Eintrag.")


def test_backfill(tmp_root: Path) -> dict:
    """Teil 2: Trockenlauf, echter Nachtrag, kein Ueberschreiben, --force."""
    print("\n--- Test 5: backfill_accuracy (3 Bloecke) ---")
    out_root = tmp_root / "out_backfill"
    out_root.mkdir()
    db_path = str(tmp_root / "backfill.duckdb")
    database.DB_PATH = db_path
    database.create_database()

    blocks = {
        "merge_add": Fixture(out_root, "berlin", "small", "merge_add", "dem"),
        "aggregation": Fixture(out_root, "berlin", "medium", "aggregation", "dem"),
        "lc_overlay": Fixture(out_root, "wien", "small", "lc_overlay", "landcover"),
    }
    t0 = datetime(2026, 4, 1, 9, 0, 0)
    mt = 1_770_000_000.0
    for i, (name, fx) in enumerate(blocks.items()):
        fx.build(3, t0 + timedelta(hours=i), mt + i * 3600)
        print(f"  Block {name}: test-run_ids={[r[0] for r in fx.test_runs]}, "
              f"reference run_id={fx.reference[0]}")

    # Zustand VOR dem Fix nachstellen: je Block genau ein Wert (der letzte).
    for fx in blocks.values():
        _old_style_check(out_root, fx)
    rows = _accuracy_rows(db_path)
    print(f"\n  Vor dem Nachtrag: {len(rows)} accuracy-Zeile(n): "
          f"{[r[0] for r in rows]}")
    assert len(rows) == 3, rows
    assert [r[0] for r in rows] == sorted(fx.test_runs[-1][0]
                                          for fx in blocks.values())
    return {"out_root": out_root, "db_path": db_path, "blocks": blocks}


def assert_backfill_result(state: dict) -> None:
    """Nach dem echten Backfill-Lauf: jede Wiederholung hat ihren Wert und
    alle Wiederholungen eines Blocks sind identisch (deterministisch)."""
    rows = _accuracy_rows(state["db_path"])
    by_id = {r[0]: r for r in rows}
    print(f"\n  Nach dem Nachtrag: {len(rows)} accuracy-Zeilen.")
    for name, fx in state["blocks"].items():
        ids = [rid for rid, _ in fx.test_runs]
        assert all(i in by_id for i in ids), f"{name}: fehlt {set(ids) - set(by_id)}"
        if fx.dataset == "landcover":
            vals = {by_id[i][3] for i in ids}          # agreement_pct
            kinds = {by_id[i][4] for i in ids}
            assert kinds == {"categorical"}, kinds
            assert all(by_id[i][1] is None for i in ids), "mae darf NULL sein"
            print(f"  {name}: run_ids={ids}, agreement_pct={vals}, "
                  f"metric_kind={kinds}")
        else:
            vals = {round(by_id[i][1], 9) for i in ids}  # mae
            kinds = {by_id[i][4] for i in ids}
            assert kinds == {"continuous"}, kinds
            print(f"  {name}: run_ids={ids}, mae={vals}, metric_kind={kinds}")
        assert len(vals) == 1, f"{name}: Werte weichen ab: {vals}"
        refs = {by_id[i][5] for i in ids}
        assert refs == {fx.reference[0]}, f"{name}: reference_run_id {refs}"
    print("  OK: jede Wiederholung hat ihren Wert, alle identisch, "
          "reference_run_id gesetzt.")


def test_no_fallback_without_reference(tmp_root: Path) -> None:
    """Ohne local_reference wird NICHT gegen onthefly verglichen, sondern
    uebersprungen - und der Block MIT Referenz bleibt davon unberuehrt."""
    print("\n--- Test 6: kein Rueckfall ohne local_reference ---")
    out_root = tmp_root / "out_noref"
    out_root.mkdir()
    db_path = str(tmp_root / "noref.duckdb")
    database.DB_PATH = db_path
    database.create_database()

    mit = Fixture(out_root, "berlin", "small", "merge_add", "dem")
    mit.build(3, datetime(2026, 7, 1, 5, 0, 0), 1_800_000_000.0)
    ohne = Fixture(out_root, "berlin", "large", "focal", "dem")
    ohne.build(2, datetime(2026, 7, 1, 8, 0, 0), 1_800_010_000.0,
               with_reference=False)
    print(f"  mit Referenz  : run_ids={[r[0] for r in mit.test_runs]} "
          f"(local_reference={mit.reference[0]})")
    print(f"  ohne Referenz : run_ids={[r[0] for r in ohne.test_runs]} "
          f"(local_reference fehlt)")

    cmd = [sys.executable, "backfill_accuracy.py", "--db", db_path,
           "--output-dir", str(out_root), "--dry-run"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    plan = out.stdout[out.stdout.index("  Plan:"):]
    print("\n".join("  | " + ln for ln in plan.splitlines()
                    if ln.strip().startswith(("Plan:", "run_id=", "Uebersprungen"))))
    assert "Plan: 3 nachzutragen, 2 uebersprungen" in out.stdout, plan
    for rid, _ in ohne.test_runs:
        assert (f"run_id={rid} (onthefly): keine local_reference fuer "
                f"Region=berlin, Extent=large, Workflow=focal") in out.stdout, plan
    # Jede geplante Referenz ist ein local_reference-Ordner.
    ref_lines = [ln for ln in plan.splitlines() if "Referenz:" in ln]
    assert ref_lines and all("_local_reference" in ln for ln in ref_lines), ref_lines

    # Echter Lauf: die drei mit Referenz bekommen Werte, die zwei ohne nicht.
    out = subprocess.run(cmd[:-1], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    rows = {r[0]: r for r in _accuracy_rows(db_path)}
    got = sorted(rows)
    print(f"  accuracy-Zeilen danach: {got}")
    assert got == sorted(rid for rid, _ in mit.test_runs), got
    for rid, _ in ohne.test_runs:
        assert rid not in rows, f"run_id={rid} haette keinen Wert bekommen duerfen"
    # Und die geschriebenen Zeilen zeigen wirklich auf local_reference.
    for rid, _ in mit.test_runs:
        assert Path(rows[rid][6]).name.endswith("_local_reference"), rows[rid]
    print("  OK: uebersprungen statt gegen onthefly verglichen, "
          "Referenzblock unveraendert.")


def test_preflight_reference(tmp_root: Path) -> None:
    """Fehlt die Referenz, startet --reference-check keinen einzigen Lauf."""
    print("\n--- Test 7: Vorabpruefung startet keine Messlaeufe ---")
    out_root = tmp_root / "out_preflight"
    out_root.mkdir()
    database.DB_PATH = str(tmp_root / "preflight.duckdb")
    database.create_database()

    calls = []

    def fake_runner(args, repeat_idx):
        calls.append(repeat_idx)
        return {"strategy": "onthefly", "repeat": repeat_idx + 1,
                "run_type": "cold", "status": "error", "run_id": None,
                "outdir": None, "preprocessing_time": None, "total_time": None}

    argv = ["run_benchmark.py", "--strategy", "onthefly", "--region", "wien",
            "--extent-size", "small", "--workflow", "merge_add",
            "--repeat", "3", "--reference-check", "--min-free-gb", "0",
            "--output-dir", str(out_root)]
    with patch.object(rb, "run_strategy_onthefly", fake_runner), \
            patch.object(sys, "argv", argv):
        try:
            rb.main()
            raised = None
        except SystemExit as exc:
            raised = exc.code
    print(f"  SystemExit={raised}, gestartete Laeufe={len(calls)}")
    assert raised == 2, f"kein Abbruch (exit={raised})"
    assert calls == [], f"es wurden {len(calls)} Laeufe gestartet"

    # Mit --allow-missing-reference laufen sie bewusst trotzdem.
    with patch.object(rb, "run_strategy_onthefly", fake_runner), \
            patch.object(sys, "argv", argv + ["--allow-missing-reference"]):
        rb.main()
    print(f"  mit --allow-missing-reference: gestartete Laeufe={len(calls)}")
    assert calls == [0, 1, 2], calls
    assert _accuracy_rows(database.DB_PATH) == []
    print("  OK: Abbruch ohne Referenz, Escape-Hatch funktioniert.")


def main() -> int:
    old_db = database.DB_PATH
    tmp_root = Path(tempfile.mkdtemp(prefix="backfill_test_"))
    print(f"Temp: {tmp_root}")
    try:
        test_session_targets()
        test_process_graph_unchanged()
        test_single_run_equivalence(tmp_root)
        test_main_reference_check(tmp_root)
        state = test_backfill(tmp_root)

        # Trockenlauf ueber die echte CLI.
        print("\n--- Trockenlauf (CLI) ---")
        cmd = [sys.executable, "backfill_accuracy.py",
               "--db", state["db_path"], "--output-dir", str(state["out_root"]),
               "--dry-run"]
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        print(out.stdout[-4000:] or out.stderr[-4000:])
        assert out.returncode == 0, out.stderr
        assert _accuracy_rows(state["db_path"]).__len__() == 3, \
            "Trockenlauf hat geschrieben!"

        print("\n--- Echter Nachtrag (CLI) ---")
        out = subprocess.run(cmd[:-1], cwd=ROOT, capture_output=True, text=True)
        print(out.stdout[-3000:] or out.stderr[-3000:])
        assert out.returncode == 0, out.stderr
        assert_backfill_result(state)

        print("\n--- Zweiter Lauf ohne --force (darf nichts tun) ---")
        before = _accuracy_rows(state["db_path"])
        out = subprocess.run(cmd[:-1], cwd=ROOT, capture_output=True, text=True)
        assert "Nichts nachzutragen" in out.stdout, out.stdout[-2000:]
        assert _accuracy_rows(state["db_path"]) == before, \
            "Bestehende Zeilen wurden veraendert!"
        print("  OK: bestehende Eintraege unangetastet.")

        print("\n--- --force ersetzt genau eine Zeile ---")
        target = state["blocks"]["merge_add"].test_runs[0][0]
        out = subprocess.run(cmd[:-1] + ["--force", "--run-id", str(target)],
                             cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        after = _accuracy_rows(state["db_path"])
        assert len(after) == len(before), f"{len(before)} -> {len(after)}"
        assert [r for r in after if r[0] == target][0][1] == \
               [r for r in before if r[0] == target][0][1]
        print(f"  OK: run_id={target} ersetzt, Zeilenzahl unveraendert "
              f"({len(after)}), Wert identisch.")

        test_no_fallback_without_reference(tmp_root)
        test_preflight_reference(tmp_root)

        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        database.DB_PATH = old_db
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

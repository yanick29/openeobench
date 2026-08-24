#!/usr/bin/env python3
"""backfill_accuracy.py - fehlende Genauigkeitswerte fuer bereits gelaufene
Runs nachtragen, ohne einen Benchmark zu starten.

Hintergrund: der Accuracy-Check lief bis zum Fix in run_benchmark.py genau
EINMAL am Ende eines Aufrufs und verglich den per _find_latest_run_dir
ermittelten Ordner - also immer nur die letzte Wiederholung. Bei --repeat 3
bekam damit nur der dritte Lauf eine Zeile in der accuracy-Tabelle. Dieses
Skript traegt die fehlenden Zeilen fuer alle schon vorhandenen Ordner nach.

Referenz ist IMMER local_reference (s. REFERENCE_STRATEGY). Fehlt sie fuer
eine Konfiguration, wird der Lauf uebersprungen und in der Skip-Liste
begruendet - es tritt KEINE andere Strategie an ihre Stelle.

Verglichen wird ueber run_benchmark.run_accuracy_check - dieselbe Funktion,
die auch der regulaere Check benutzt. Damit sind alle Spalten identisch
gefuellt (inkl. metric_kind/agreement_pct/kappa/confusion_json bei
kategorialen Datensaetzen) und die Strategie-Layouts (local_pp ->
step3_main/, onthefly -> ./, local_reference -> step4_result/) sowie die
Sonderbehandlung zeitreduzierender Workflows (TIME_REDUCING_WORKFLOWS,
undatierte openEO.tif gegen datierte Referenz) gelten unveraendert mit.

Zuordnung run_id <-> Ausgabeordner: ueber den timestamp in der results.json
des Runs gegen runs.timestamp in der DB - derselbe Weg, den
run_benchmark._lookup_run_id_for_dir schon fuer den regulaeren Check geht,
und robuster als das Mitlesen der Konsolenausgabe. Das Log-Parsing
("Output: ..." unmittelbar vor "Run importiert: ... (run_id=NNN)") gibt es
zusaetzlich unter --log, aber nur als Nachrang fuer Ordner, deren
results.json fehlt oder deren timestamp nicht in der DB steht.

VORSCHLAG (hier bewusst NICHT umgesetzt): run_benchmark koennte die run_id
nach dem import_run in die run_meta.json des Ordners zurueckschreiben. Das
waere die direkteste Zuordnung, ohne Umweg ueber timestamp-Gleichheit oder
Logdateien. Bestehende Ordner werden davon nicht rueckwirkend beruehrt -
fuer sie bleibt der timestamp-Weg noetig, deshalb aendert dieses Skript
nichts an vorhandenen run_meta.json-Dateien.

Beispiele:
  py backfill_accuracy.py --dry-run
  py backfill_accuracy.py --dry-run --min-run-id 880 --strategy onthefly
  py backfill_accuracy.py --min-run-id 880
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import duckdb

import database
import run_benchmark as rb

# Strategien, die als TEST-Seite eines Vergleichs in Frage kommen.
# local_reference ist die Ground-Truth und bekommt selbst keine Zeile.
CDSE_TEST_STRATEGIES = ("onthefly", "local_preprocessing", "full_preprocessing")

# Die Referenz ist IMMER die lokale Ground-Truth - kein Rueckfall, kein
# Schalter. Fehlt sie, wird der Lauf uebersprungen.
#
# Warum hart verdrahtet: ein Vergleich gegen onthefly liefert Zahlen, die
# aussehen wie ein Genauigkeitswert, aber etwas anderes messen (zwei
# CDSE-Wege gegeneinander statt CDSE gegen eine unabhaengige lokale
# Rechnung). In der Auswertung stehen beide in derselben Spalte und sind
# hinterher nicht mehr auseinanderzuhalten. Real passiert fuer wien und
# newyork, nachdem der Referenzlauf am CDSE-Warteschlangen-Timeout
# gescheitert war.
REFERENCE_STRATEGY = "local_reference"

# Label in der DB -> Schluessel in rb._ACCURACY_LAYOUT.
STRATEGY_ALIASES = {"local_pp_cached": "local_preprocessing"}

# "Output: <dir>" ... spaeter ... "Run importiert: ... (run_id=NNN)"
_LOG_OUTPUT_RE = re.compile(r"Output:\s+(\S+)")
_LOG_RUNID_RE = re.compile(r"Run importiert:.*\(run_id=(\d+)\)")


def _canonical_strategy(strategy):
    """DB-Label auf den Schluessel in _ACCURACY_LAYOUT normalisieren."""
    if strategy is None:
        return None
    return STRATEGY_ALIASES.get(strategy, strategy)


def _results_json_dir(run_dir: Path, strategy: str) -> Path:
    """Ordner, in dem die results.json dieses Runs liegt.

    import_run() bekommt bei local_pp/full_pp den Ergebnis-Unterordner
    (step3_main / step5_main) uebergeben, bei onthefly und local_reference
    dagegen das Run-Root. Der timestamp-Abgleich muss deshalb an genau
    dieser Stelle suchen - im Zweifel werden beide Kandidaten probiert.
    """
    candidates = []
    if strategy != "local_reference":
        candidates.append(rb._tif_dir(run_dir, strategy))
    candidates.append(run_dir)
    for cand in candidates:
        if (cand / "results.json").is_file():
            return cand
    return candidates[0]


# ---------------------------------------------------------------------------
# Zuordnung run_id <-> Ordner
# ---------------------------------------------------------------------------

def timestamp_index(conn) -> dict:
    """{timestamp: run_id} aus der runs-Tabelle.

    Fachlich derselbe Abgleich wie run_benchmark._lookup_run_id_for_dir
    (results.json.timestamp -> runs.timestamp, bei Gleichstand die groesste
    run_id), nur EINMAL abgefragt statt mit einer DB-Verbindung je Ordner -
    bei mehreren hundert Runs ist das der Unterschied zwischen Sekunden und
    Minuten.
    """
    rows = conn.execute(
        "SELECT timestamp, MAX(run_id) FROM runs "
        "WHERE timestamp IS NOT NULL GROUP BY timestamp").fetchall()
    return {ts: run_id for ts, run_id in rows}


def _run_id_for_dir(res_dir: Path, ts_map: dict):
    """run_id eines Ordners ueber den timestamp seiner results.json."""
    path = res_dir / "results.json"
    if not path.is_file():
        return None
    try:
        ts = json.loads(path.read_text()).get("timestamp")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return ts_map.get(ts) if ts else None


def index_run_dirs(output_dir: str, ts_map: dict,
                   verbose: bool = False) -> dict:
    """{run_id: (strategy, Path)} fuer alle Run-Ordner unter output_dir.

    Der Ordnername traegt das Strategie-Suffix (run_*_onthefly,
    run_*_local_pp, run_*_full_pp, run_*_local_reference), die run_id kommt
    aus dem timestamp-Abgleich der results.json gegen die runs-Tabelle.
    """
    index = {}
    collisions = {}
    for strategy, (suffix, _sub) in rb._ACCURACY_LAYOUT.items():
        for run_dir in sorted(Path(output_dir).glob(f"run_*_{suffix}")):
            if not run_dir.is_dir():
                continue
            res_dir = _results_json_dir(run_dir, strategy)
            run_id = _run_id_for_dir(res_dir, ts_map)
            if run_id is None:
                if verbose:
                    print(f"  [index] keine run_id fuer {run_dir.name} "
                          f"(results.json in {res_dir.name}?)")
                continue
            if run_id in index and index[run_id][1] != run_dir:
                collisions.setdefault(run_id, [index[run_id][1]]).append(run_dir)
                continue
            index[run_id] = (strategy, run_dir)
    for run_id, dirs in collisions.items():
        print(f"  WARNUNG: run_id={run_id} passt auf mehrere Ordner "
              f"({', '.join(d.name for d in dirs)}) - identische timestamps. "
              f"Es wird {index[run_id][1].name} verwendet.")
    return index


def parse_log_index(log_path: str) -> dict:
    """{run_id: Path} aus einem Benchmark-Log.

    Nachrangiger Weg (s. Modul-Docstring): im Log steht die Zeile
    "Output: <dir>" vor dem spaeteren "Run importiert: ... (run_id=NNN)".
    Zugeordnet wird der zuletzt gesehene Output-Ordner.
    """
    mapping = {}
    last_output = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m_out = _LOG_OUTPUT_RE.search(line)
                if m_out:
                    last_output = m_out.group(1).rstrip("/\\")
                    continue
                m_id = _LOG_RUNID_RE.search(line)
                if m_id and last_output:
                    mapping[int(m_id.group(1))] = Path(last_output)
                    last_output = None
    except OSError as exc:
        print(f"  WARNUNG: Log '{log_path}' nicht lesbar: {exc}")
    return mapping


def _strategy_of_dir(run_dir: Path):
    """Strategie aus dem Ordner-Suffix, oder None."""
    for strategy, (suffix, _sub) in rb._ACCURACY_LAYOUT.items():
        if run_dir.name.endswith(f"_{suffix}"):
            return strategy
    return None


# ---------------------------------------------------------------------------
# Konfiguration eines Runs (Region, Extent, Workflow, Aufloesung, Datensatz)
# ---------------------------------------------------------------------------

def _detect_extent_size(region: str, run_dir: Path):
    """extent_size-Label ueber den exakten Bounding-Box-Vergleich bestimmen.

    Fallback fuer Runs ohne extent_size-Spalte in der DB. Die Labels
    schliessen sich gegenseitig aus (exakter Bounds-Vergleich), es kann
    also hoechstens eines passen.
    """
    for label in rb.SIZE_KM:
        try:
            target = rb._compute_extent(region, label)
        except (KeyError, ValueError):
            continue
        if rb._folder_matches_extent(run_dir, target):
            return label
    return None


def run_config(row: dict, run_dir: Path) -> dict:
    """Vergleichs-Konfiguration eines Runs.

    DB-Spalten haben Vorrang; fehlt eine (aeltere DB ohne die Spalte oder
    NULL), wird sie aus dem Ordner erkannt - mit genau den Funktionen, die
    auch _find_latest_run_dir zum Filtern benutzt. So kann Zuordnung und
    Kandidatensuche nicht auseinanderlaufen.
    """
    region = rb._detect_folder_region(run_dir)
    extent_size = row.get("extent_size")
    if not extent_size and region:
        extent_size = _detect_extent_size(region, run_dir)
    workflow = row.get("workflow") or rb._detect_folder_workflow(run_dir)
    resolution = row.get("resolution_m")
    if resolution is None:
        resolution = rb._detect_folder_resolution(run_dir)
    dataset = row.get("dataset") or rb._detect_folder_dataset(run_dir)
    return {
        "region": region,
        "extent_size": extent_size,
        "workflow": workflow,
        "resolution": float(resolution) if resolution is not None else None,
        "dataset": dataset or rb.DEFAULT_DATASET,
        "resampling": row.get("local_resampling") or None,
    }


def _config_key(cfg: dict, strategy: str) -> tuple:
    """Vergleichsschluessel: gleiche Region, Extent, Workflow, Aufloesung,
    Datensatz und Strategie."""
    return (strategy, cfg["region"], cfg["extent_size"], cfg["workflow"],
            cfg["resolution"], cfg["dataset"])


def _has_result_tifs(run_dir: Path, strategy: str, workflow: str) -> bool:
    """Liegen im Ergebnis-Unterordner ueberhaupt Workflow-TIFs?

    Wichtig fuer die Referenzwahl: cleanup_after_accuracy loescht die TIFs
    aufgeraeumter Runs, der Ordner bleibt aber mit results.json/JSONs
    stehen. Eine so geleerte Referenz waere formal ein Treffer, inhaltlich
    aber wertlos.
    """
    tif_dir = rb._tif_dir(run_dir, strategy)
    if rb._collect_workflow_tifs(tif_dir):
        return True
    if workflow in rb.TIME_REDUCING_WORKFLOWS:
        return bool(rb._collect_reduced_tifs(tif_dir))
    return False


# ---------------------------------------------------------------------------
# Referenzwahl
# ---------------------------------------------------------------------------

def existing_accuracy_refs(conn) -> list:
    """[(run_id, reference_file), ...], neueste zuerst - einmal geladen und
    dann fuer jeden Kandidaten wiederverwendet."""
    try:
        return conn.execute(
            "SELECT run_id, reference_file FROM accuracy "
            "WHERE reference_file IS NOT NULL ORDER BY accuracy_id DESC"
        ).fetchall()
    except duckdb.Error:
        return []


def sibling_reference_dir(acc_refs: list, cfg: dict, strategy: str,
                          index: dict, config_cache: dict, rows_by_id: dict):
    """local_reference-ORDNER aus einer vorhandenen accuracy-Zeile derselben
    Konfiguration, oder None.

    Damit trifft der Nachtrag dieselbe Referenz wie die schon eingetragenen
    Geschwister-Wiederholungen und nicht stumm eine andere (neuere).

    Zeilen, deren reference_file KEIN local_reference-Ordner ist, werden
    bewusst uebergangen: die stammen aus --accuracy-check (Referenz
    onthefly) oder aus dem frueheren Rueckfall. Sie als Vorlage zu nehmen
    wuerde eine fremde Referenz in die nachgetragenen Zeilen weitertragen.
    """
    want = _config_key(cfg, strategy)
    for sib_run_id, ref_file in acc_refs:
        entry = index.get(sib_run_id)
        if not entry:
            continue
        sib_strategy, sib_dir = entry
        sib_row = rows_by_id.get(sib_run_id, {})
        if sib_run_id not in config_cache:
            config_cache[sib_run_id] = run_config(sib_row, sib_dir)
        if _config_key(config_cache[sib_run_id], sib_strategy) != want:
            continue
        ref_dir = Path(ref_file)
        if _strategy_of_dir(ref_dir) != REFERENCE_STRATEGY:
            continue
        if not ref_dir.is_dir():
            continue
        return ref_dir
    return None


def resolve_reference(output_dir: str, cfg: dict, preferred_dir: Path):
    """(ref_dir, hinweis) - den local_reference-Ordner endgueltig waehlen.

    preferred_dir (aus einer Geschwister-Zeile) gewinnt, solange er noch
    Ergebnis-TIFs enthaelt. Sonst der neueste passende local_reference-Lauf
    mit gleicher Region, Extent, Workflow, Aufloesung und Datensatz - exakt
    die Auswahl, die auch der regulaere Check trifft.

    Gibt es keinen brauchbaren, ist das Ergebnis None und der Lauf wird
    uebersprungen. Eine andere Strategie tritt NIE an die Stelle der
    Referenz - der Wert saehe normal aus, wuerde aber etwas anderes messen.
    """
    if preferred_dir is not None:
        if _has_result_tifs(preferred_dir, REFERENCE_STRATEGY, cfg["workflow"]):
            return (preferred_dir, "wie Geschwister-Zeile")
        note = "Geschwister-Referenz ohne TIFs (aufgeraeumt?), neueste stattdessen"
    else:
        note = "neueste passende"
    suffix, _sub = rb._ACCURACY_LAYOUT[REFERENCE_STRATEGY]
    ref_dir = rb._find_latest_run_dir(output_dir, suffix, cfg["region"],
                                      extent_size=cfg["extent_size"],
                                      workflow=cfg["workflow"],
                                      resolution=cfg["resolution"],
                                      dataset=cfg["dataset"])
    if ref_dir is not None and not _has_result_tifs(
            ref_dir, REFERENCE_STRATEGY, cfg["workflow"]):
        # Ordner da, aber ohne Raster (cleanup-after-accuracy): als Referenz
        # unbrauchbar. Lieber kein Wert als ein Wert gegen etwas anderes.
        return (None, "local_reference vorhanden, aber ohne Ergebnis-TIFs")
    return (ref_dir, note)


def preview_pairs(run_dir: Path, strategy: str, ref_dir: Path,
                  ref_strategy: str, workflow: str) -> str:
    """Kurzbeschreibung der Dateipaarung fuer den Trockenlauf - ohne die
    Raster zu lesen (nur Dateinamen)."""
    test_tif_dir = rb._tif_dir(run_dir, strategy)
    ref_tifs = rb._collect_workflow_tifs(rb._tif_dir(ref_dir, ref_strategy))
    test_tifs = rb._collect_workflow_tifs(test_tif_dir)
    common = sorted(set(ref_tifs) & set(test_tifs))
    if common:
        return f"{len(common)} gemeinsame Date-TIFs"
    if workflow in rb.TIME_REDUCING_WORKFLOWS:
        pair = rb._pair_time_reduced(ref_tifs, test_tif_dir)
        if pair:
            _ref_path, _test_path, test_name, ref_name = pair
            return (f"zeitreduziert: {test_name} gegen Referenz {ref_name} "
                    f"(von {len(ref_tifs)} Datumsnamen)")
    return (f"KEINE Paarung (Referenz {len(ref_tifs)} TIFs, "
            f"Test {len(test_tifs)} TIFs)")


# ---------------------------------------------------------------------------
# Schutz: keine DB-Schreibvorgaenge waehrend ein Benchmark laeuft
# ---------------------------------------------------------------------------

def benchmark_running():
    """(True/False/None, Detailtext).

    None heisst "nicht feststellbar" - dann wird nur gewarnt. duckdb selbst
    laesst ohnehin keinen zweiten Schreiber auf dieselbe Datei zu; diese
    Pruefung faengt den Fall frueher und mit klarer Meldung ab.
    """
    own_pid = str(os.getpid())
    cmds = []
    if sys.platform.startswith("win"):
        cmds.append(["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process | "
                     "Select-Object -ExpandProperty CommandLine"])
    else:
        cmds.append(["ps", "-eo", "pid,args"])
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0 and not out.stdout:
            continue
        hits = []
        for line in out.stdout.splitlines():
            if "run_benchmark.py" not in line:
                continue
            if "backfill_accuracy" in line:
                continue
            if line.strip().split(" ")[0] == own_pid:
                continue
            # Lange Wrapper-Kommandozeilen kuerzen - es geht nur darum, den
            # Treffer erkennbar zu machen.
            hits.append(line.strip()[:160])
        return (bool(hits), "\n".join(hits[:5]))
    return (None, "Prozessliste nicht abfragbar")


# ---------------------------------------------------------------------------
# DB-Zugriff
# ---------------------------------------------------------------------------

def _table_columns(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(
            f"PRAGMA table_info('{table}')").fetchall()}
    except duckdb.Error:
        return set()


def fetch_runs(conn, run_cols: set) -> list:
    """Alle Runs als Dicts - nur mit den Spalten, die es in dieser DB gibt."""
    wanted = ["run_id", "crs_strategy", "status", "timestamp", "extent_size",
              "workflow", "local_resampling", "resolution_m", "dataset"]
    sel = [c for c in wanted if c in run_cols]
    rows = conn.execute(
        f"SELECT {', '.join(sel)} FROM runs ORDER BY run_id").fetchall()
    return [dict(zip(sel, r)) for r in rows]


def runs_with_accuracy(conn) -> set:
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT run_id FROM accuracy "
            "WHERE run_id IS NOT NULL").fetchall()}
    except duckdb.Error as exc:
        print(f"  WARNUNG: accuracy-Tabelle nicht lesbar ({exc}). "
              f"Es wird angenommen, dass noch keine Zeile existiert.")
        return set()


def delete_accuracy_rows(run_id: int) -> int:
    """Vorhandene Zeilen eines Runs entfernen (nur mit --force)."""
    conn = duckdb.connect(database.DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM accuracy WHERE run_id = ?",
                     (run_id,)).fetchone()[0]
    conn.execute("DELETE FROM accuracy WHERE run_id = ?", (run_id,))
    conn.commit()
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Planung
# ---------------------------------------------------------------------------

def _warn_foreign_references(acc_refs: list) -> None:
    """Vorhandene Zeilen nennen, deren Referenz kein local_reference ist.

    Der Nachtrag ruehrt sie nicht an - aber sie stehen in derselben Spalte
    wie die Werte gegen die lokale Ground-Truth und sind in der Auswertung
    sonst nicht als etwas anderes erkennbar. Entweder bewusst per
    --accuracy-check entstanden oder Ergebnis des frueheren Rueckfalls.
    """
    foreign = sorted({run_id for run_id, ref_file in acc_refs
                      if _strategy_of_dir(Path(ref_file)) != REFERENCE_STRATEGY})
    if not foreign:
        return
    shown = ", ".join(str(r) for r in foreign[:15])
    more = f" (+{len(foreign) - 15} weitere)" if len(foreign) > 15 else ""
    print(f"\n  HINWEIS: {len(foreign)} vorhandene accuracy-Zeile(n) haben "
          f"KEINE local_reference als Referenz: run_ids {shown}{more}.\n"
          f"  Sie stammen aus --accuracy-check (Referenz onthefly) oder aus "
          f"dem frueheren Rueckfall und messen etwas anderes als der "
          f"Referenzvergleich.\n"
          f"  Dieses Skript aendert sie nicht - nach einem local_reference-"
          f"Lauf lassen sie sich mit --force gezielt ersetzen.")


def build_plan(conn, args, index: dict) -> list:
    """Liste von Vorhaben: je Run entweder 'todo' oder 'skip' + Begruendung."""
    run_cols = _table_columns(conn, "runs")
    rows = fetch_runs(conn, run_cols)
    rows_by_id = {r["run_id"]: r for r in rows}
    have_accuracy = runs_with_accuracy(conn)
    acc_refs = existing_accuracy_refs(conn)
    _warn_foreign_references(acc_refs)
    wanted_strategies = set(args.strategy or CDSE_TEST_STRATEGIES)
    config_cache = {}
    plan = []

    for row in rows:
        run_id = row["run_id"]
        strategy = _canonical_strategy(row.get("crs_strategy"))
        if args.run_id and run_id not in args.run_id:
            continue
        if args.min_run_id is not None and run_id < args.min_run_id:
            continue
        if args.max_run_id is not None and run_id > args.max_run_id:
            continue
        if strategy not in wanted_strategies:
            continue
        if run_id in have_accuracy and not args.force:
            continue

        entry = {"run_id": run_id, "strategy": strategy,
                 "existing": run_id in have_accuracy}

        if run_id not in index:
            entry.update(action="skip",
                         reason="kein Ausgabeordner zugeordnet "
                                "(results.json/timestamp fehlt)")
            plan.append(entry)
            continue

        dir_strategy, run_dir = index[run_id]
        if dir_strategy != strategy:
            entry.update(action="skip",
                         reason=f"Ordner-Suffix ({dir_strategy}) passt nicht "
                                f"zu crs_strategy ({strategy})")
            plan.append(entry)
            continue
        entry["run_dir"] = run_dir

        if run_id not in config_cache:
            config_cache[run_id] = run_config(row, run_dir)
        cfg = config_cache[run_id]
        entry["config"] = cfg
        if not cfg["region"]:
            entry.update(action="skip", reason="Region nicht erkennbar")
            plan.append(entry)
            continue
        if not _has_result_tifs(run_dir, strategy, cfg["workflow"]):
            entry.update(action="skip",
                         reason="keine Ergebnis-TIFs im Ordner "
                                f"({rb._tif_dir(run_dir, strategy).name or '.'})")
            plan.append(entry)
            continue

        preferred = sibling_reference_dir(acc_refs, cfg, strategy, index,
                                          config_cache, rows_by_id)
        ref_note = ("aus Geschwister-Zeile" if preferred is not None
                    else "keine Geschwister-Zeile")
        ref_strategy = REFERENCE_STRATEGY

        ref_dir, dir_note = resolve_reference(args.output_dir, cfg, preferred)
        if ref_dir is None:
            entry.update(action="skip",
                         reason=f"keine local_reference fuer Region="
                                f"{cfg['region']}, Extent={cfg['extent_size']}, "
                                f"Workflow={cfg['workflow']}, "
                                f"Aufloesung={cfg['resolution']}, "
                                f"Datensatz={cfg['dataset']} vorhanden"
                                + (f" ({dir_note})"
                                   if dir_note.startswith("local_reference")
                                   else "")
                                + " - es wird KEIN Wert geschrieben und "
                                  "KEINE andere Strategie als Referenz "
                                  "eingesetzt")
            plan.append(entry)
            continue

        entry.update(action="todo", ref_strategy=ref_strategy,
                     ref_dir=ref_dir, ref_note=f"{ref_note}, {dir_note}",
                     pairing=preview_pairs(run_dir, strategy, ref_dir,
                                           ref_strategy, cfg["workflow"]))
        plan.append(entry)

    return plan


def print_plan(plan: list, args) -> None:
    todo = [e for e in plan if e["action"] == "todo"]
    skip = [e for e in plan if e["action"] == "skip"]
    print(f"\n{'='*72}")
    print(f"  Plan: {len(todo)} nachzutragen, {len(skip)} uebersprungen")
    print(f"{'='*72}")
    for e in todo:
        cfg = e["config"]
        flag = "  [FORCE: ersetzt vorhandene Zeile]" if e["existing"] else ""
        print(f"\n  run_id={e['run_id']}  {e['strategy']}{flag}")
        print(f"    Test:     {e['run_dir'].name}  -> "
              f"{rb._tif_dir(e['run_dir'], e['strategy']).name or '.'}/")
        print(f"    Referenz: {e['ref_dir'].name}  ({e['ref_strategy']}, "
              f"{e['ref_note']})")
        print(f"    Konfig:   Region={cfg['region']}, Extent={cfg['extent_size']}, "
              f"Workflow={cfg['workflow']}, Aufloesung={cfg['resolution']}, "
              f"Datensatz={cfg['dataset']}, "
              f"Resampling={cfg['resampling'] or args.resampling}")
        print(f"    Paarung:  {e['pairing']}")
    if skip:
        limit = len(skip) if args.verbose else 20
        print(f"\n  Uebersprungen ({len(skip)}):")
        for e in skip[:limit]:
            print(f"    run_id={e['run_id']} ({e['strategy']}): {e['reason']}")
        if len(skip) > limit:
            print(f"    ... und {len(skip) - limit} weitere (--verbose zeigt alle)")


# ---------------------------------------------------------------------------
# Ausfuehrung
# ---------------------------------------------------------------------------

def execute_plan(plan: list, args) -> dict:
    stats = {"ok": 0, "fail": 0, "replaced": 0}
    for e in [x for x in plan if x["action"] == "todo"]:
        cfg = e["config"]
        if e["existing"] and args.force:
            n = delete_accuracy_rows(e["run_id"])
            stats["replaced"] += n
            print(f"\n  [--force] {n} vorhandene accuracy-Zeile(n) fuer "
                  f"run_id={e['run_id']} geloescht.")
        result = rb.run_accuracy_check(
            args.output_dir, cfg["region"],
            test_strategy=e["strategy"],
            test_run_id=e["run_id"],
            test_dir=e["run_dir"],
            extent_size=cfg["extent_size"],
            workflow=cfg["workflow"],
            resampling_method=cfg["resampling"] or args.resampling,
            reference_strategy=e["ref_strategy"],
            resolution=cfg["resolution"],
            dataset=cfg["dataset"],
            reference_dir=e["ref_dir"],
        )
        if result is None:
            stats["fail"] += 1
            print(f"  -> run_id={e['run_id']}: kein Wert geschrieben.")
        else:
            stats["ok"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fehlende accuracy-Eintraege fuer bereits gelaufene Runs "
                    "nachtragen (ohne Benchmark).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Beispiele:")[-1])
    parser.add_argument("--output-dir", default="outputs",
                        help="Basisordner mit den run_*-Ordnern (Default: outputs)")
    parser.add_argument("--db", default=None,
                        help=f"Pfad zur DuckDB (Default: {database.DB_PATH})")
    parser.add_argument("--min-run-id", type=int, default=None,
                        help="Nur run_ids >= diesem Wert betrachten.")
    parser.add_argument("--max-run-id", type=int, default=None,
                        help="Nur run_ids <= diesem Wert betrachten.")
    parser.add_argument("--run-id", type=int, action="append", default=None,
                        help="Genau diese run_id nachtragen (mehrfach moeglich).")
    parser.add_argument("--strategy", action="append", default=None,
                        choices=list(CDSE_TEST_STRATEGIES),
                        help="Nur diese Test-Strategie(n). Default: alle drei.")
    parser.add_argument("--resampling", default="nearest",
                        help="Resampling-Methode, falls der Run keine in der "
                             "DB stehen hat (Default: nearest).")
    parser.add_argument("--log", default=None,
                        help="Benchmark-Log als NACHRANGIGE Quelle fuer die "
                             "Zuordnung run_id -> Ordner (nur fuer Ordner, die "
                             "ueber results.json/timestamp nicht auffindbar sind).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur auflisten, was getan wuerde. Keine "
                             "Schreibzugriffe auf die DB.")
    parser.add_argument("--force", action="store_true",
                        help="Vorhandene accuracy-Zeilen ersetzen (sonst "
                             "werden Runs mit Eintrag uebersprungen).")
    parser.add_argument("--ignore-running", action="store_true",
                        help="Trotz erkannt laufendem Benchmark schreiben "
                             "(nicht empfohlen).")
    parser.add_argument("--verbose", action="store_true",
                        help="Auch Ordner nennen, denen keine run_id "
                             "zugeordnet werden konnte.")
    args = parser.parse_args()

    if args.db:
        # Wirkt auch fuer run_benchmark: dessen Helfer importieren DB_PATH
        # erst zur Laufzeit aus dem Modul.
        database.DB_PATH = args.db
    print(f"  DB:      {database.DB_PATH}")
    print(f"  Ordner:  {args.output_dir}")

    if not Path(database.DB_PATH).is_file():
        print(f"  FEHLER: DB '{database.DB_PATH}' existiert nicht.")
        return 2

    # Die DB darf nicht beschrieben werden, waehrend ein Benchmark laeuft.
    running, detail = benchmark_running()
    if running:
        print(f"\n  ACHTUNG: es laeuft offenbar ein Benchmark:\n    "
              + detail.replace("\n", "\n    "))
        if not args.dry_run and not args.ignore_running:
            print("  Abbruch - waehrend eines laufenden Benchmarks wird nicht "
                  "in die DB geschrieben (--ignore-running erzwingt es).")
            return 3
    elif running is None:
        print(f"  Hinweis: laufender Benchmark nicht pruefbar ({detail}). "
              f"Bitte selbst sicherstellen, dass gerade keiner laeuft.")

    conn = duckdb.connect(database.DB_PATH, read_only=True)
    try:
        index = index_run_dirs(args.output_dir, timestamp_index(conn),
                               verbose=args.verbose)
        if args.log:
            extra = parse_log_index(args.log)
            added = 0
            for run_id, run_dir in extra.items():
                if run_id in index or not run_dir.is_dir():
                    continue
                strategy = _strategy_of_dir(run_dir)
                if strategy is None:
                    continue
                index[run_id] = (strategy, run_dir)
                added += 1
            print(f"  Log '{args.log}': {added} zusaetzliche Zuordnung(en).")
        print(f"  Zuordnung: {len(index)} Ordner mit run_id.")
        plan = build_plan(conn, args, index)
    finally:
        # Vor jedem Schreibzugriff schliessen: duckdb laesst read_only- und
        # Schreibverbindung auf dieselbe Datei nicht gleichzeitig zu.
        conn.close()

    print_plan(plan, args)

    todo = [e for e in plan if e["action"] == "todo"]
    if args.dry_run:
        print(f"\n  [dry-run] Es wurde nichts geschrieben. "
              f"{len(todo)} Eintrag/Eintraege waeren nachgetragen worden.")
        return 0
    if not todo:
        print("\n  Nichts nachzutragen.")
        return 0

    stats = execute_plan(plan, args)
    print(f"\n{'='*72}")
    print(f"  Fertig: {stats['ok']} Eintrag/Eintraege geschrieben, "
          f"{stats['fail']} ohne Wert"
          + (f", {stats['replaced']} ersetzt" if stats["replaced"] else ""))
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

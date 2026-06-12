"""
Korrigiert scenario-Namen fuer local_preprocessing und local_pp_cached Runs.

Vorher: scenario = "scenario_local_pp"
Nachher: scenario = "{crs_strategy}_{region}" (z.B. "local_pp_berlin", "local_pp_cached_berlin")

Region wird aus dem stac_item_{region}_*.json Dateinamen in den Run-Output-Ordnern
ermittelt. Matching ueber job_id (eindeutig).

Aufruf:
    python utils/fix_local_pp_scenario_names.py --dry-run
    python utils/fix_local_pp_scenario_names.py --apply
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from database import DB_PATH  # noqa: E402


def _load_region_keys() -> set[str]:
    """REGIONS-Keys aus run_benchmark.py extrahieren, ohne das Modul zu importieren
    (vermeidet schwere Imports wie rasterio)."""
    src = (REPO_ROOT / "run_benchmark.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "REGIONS":
                    if isinstance(node.value, ast.Dict):
                        return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise RuntimeError("REGIONS dict in run_benchmark.py nicht gefunden")


REGION_KEYS = _load_region_keys()

STAC_PATTERN = re.compile(r"^stac_item_([a-zA-Z0-9]+)_\d{8}_\d{6}\.json$")


def find_region(run_dir: Path) -> str | None:
    """Sucht stac_item_{region}_*.json im run_dir und gibt die Region zurueck."""
    for stac_file in run_dir.glob("stac_item_*.json"):
        m = STAC_PATTERN.match(stac_file.name)
        if m and m.group(1) in REGION_KEYS:
            return m.group(1)
    return None


def collect_mappings(outputs_root: Path) -> dict[str, str]:
    """job_id -> region, aus allen outputs/run_*_local_pp/ Ordnern."""
    mappings: dict[str, str] = {}
    for run_dir in outputs_root.glob("run_*_local_pp"):
        region = find_region(run_dir)
        if not region:
            print(f"  WARN: keine region in {run_dir.name}", file=sys.stderr)
            continue
        results = run_dir / "step3_main" / "results.json"
        if not results.exists():
            print(f"  WARN: keine results.json in {run_dir.name}/step3_main", file=sys.stderr)
            continue
        with open(results) as f:
            job_id = json.load(f).get("job_id")
        if not job_id:
            print(f"  WARN: keine job_id in {results}", file=sys.stderr)
            continue
        mappings[job_id] = region
    return mappings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH, help=f"DuckDB-Pfad (Standard: {DB_PATH})")
    parser.add_argument("--outputs", default="outputs", help="outputs/ root (Standard: outputs)")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true", help="Nur anzeigen, was geaendert wuerde")
    grp.add_argument("--apply", action="store_true", help="Aenderungen wirklich schreiben")
    args = parser.parse_args()

    outputs_root = Path(args.outputs)
    if not outputs_root.is_dir():
        sys.exit(f"FEHLER: outputs Verzeichnis nicht gefunden: {outputs_root}")

    print(f"Scanne {outputs_root}/run_*_local_pp/ ...")
    job_to_region = collect_mappings(outputs_root)
    print(f"  {len(job_to_region)} Mappings aus Dateisystem extrahiert.\n")

    conn = duckdb.connect(args.db, read_only=args.dry_run)
    rows = conn.execute("""
        SELECT run_id, job_id, crs_strategy, scenario
        FROM runs
        WHERE crs_strategy IN ('local_preprocessing', 'local_pp_cached')
          AND scenario = 'scenario_local_pp'
    """).fetchall()
    print(f"  {len(rows)} DB-Zeilen zu aktualisieren.\n")

    updates: list[tuple[str, int]] = []
    unmatched: list[tuple[int, str | None]] = []
    for run_id, job_id, strategy, _scenario in rows:
        region = job_to_region.get(job_id) if job_id else None
        if region is None:
            unmatched.append((run_id, job_id))
            continue
        new_scenario = f"{strategy}_{region}"
        updates.append((new_scenario, run_id))
        print(f"  run_id={run_id:>4}  job_id={job_id}  ->  scenario='{new_scenario}'")

    if unmatched:
        print(f"\n  {len(unmatched)} Zeilen ohne Region-Match (uebersprungen):")
        for run_id, job_id in unmatched:
            print(f"    run_id={run_id}  job_id={job_id}")

    if args.apply and updates:
        conn.executemany(
            "UPDATE runs SET scenario = ? WHERE run_id = ?",
            updates,
        )
        print(f"\n{len(updates)} Zeilen aktualisiert.")
    elif args.dry_run:
        print(f"\nDRY-RUN: keine Aenderungen geschrieben. {len(updates)} wuerden aktualisiert.")

    conn.close()


if __name__ == "__main__":
    main()

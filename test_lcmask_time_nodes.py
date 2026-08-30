#!/usr/bin/env python3
"""
test_lcmask_time_nodes.py - Standalone-Tests fuer das Entfernen der
t-Knoten aus dem lc_mask-Zweig, wenn der Zweitcube per load_stac kommt.

Befund (Lauf 1211, lc_mask, local_preprocessing): seit das STAC-Item die
datacube-Extension traegt, erkennt CDSE die Dimensionen als
['x','y','bands'] und lehnt den Knoten reducedimension_dem ab:
  [400] ProcessParameterInvalid: The value passed for parameter 'dimension'
  in process 'reduce_dimension' is invalid: Must be one of
  ['x','y','bands'] but got 't'.

Geprueft wird:
  1. Vorher/Nachher am lc_mask-Graphen, wie ihn local_pp erzeugt.
  2. onthefly (load_collection) behaelt beide t-Knoten - dort hat der
     Zweitcube eine echte Zeitachse.
  3. Kein Knoten des load_stac-Graphen arbeitet noch auf 't'.
  4. lc_overlay und alle DEM-Workflows sind auf allen vier Bauwegen
     byte-identisch zu HEAD.
  5. Die Workflow-Erkennung liefert weiterhin "lc_mask".

Keine Backend-Aufrufe, keine Rasterdatei, kein Zugriff auf outputs/.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import run_benchmark as rb

REGION = "berlin"
EXTENT = "medium"
LC = "landcover"
STAC = "https://example.invalid/stac_item_berlin.json"


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


def _pp_graph(module, tmp: Path, name: str, workflow="lc_mask",
              dataset=LC) -> dict:
    """lc_mask-Graph, wie ihn build_local_pp_scenario schreibt."""
    path = tmp / f"{name}.json"
    module.build_local_pp_scenario(REGION, STAC, path, extent_size=EXTENT,
                                   workflow=workflow, dataset=dataset)
    return json.loads(path.read_text())["process_graph"]


def _kette(pg: dict) -> str:
    """Knotenkette vom Zweitcube bis zum Ergebnis, der Verdrahtung nach."""
    schritte, node, gesehen = [], "lcmask1", set()
    while node and node not in gesehen:
        gesehen.add(node)
        schritte.append(f"{node} ({pg[node]['process_id']})")
        ref = pg[node]["arguments"].get("mask") or pg[node]["arguments"].get("data")
        node = ref.get("from_node") if isinstance(ref, dict) else None
    return " <- ".join(schritte)


def _t_knoten(pg: dict) -> list:
    """Alle Knoten, die auf der Dimension 't' arbeiten."""
    treffer = []
    for name, node in pg.items():
        args = node.get("arguments", {})
        if args.get("dimension") == "t" or args.get("name") == "t":
            treffer.append(f"{name} ({node['process_id']})")
    return sorted(treffer)


def test_vorher_nachher(tmp: Path) -> None:
    print("\n--- Test 1: lc_mask ueber local_pp, vorher (HEAD) und nachher ---")
    rb_head = _load_head_module(tmp)
    old = _pp_graph(rb_head, tmp, "old")
    new = _pp_graph(rb, tmp, "new")

    print("\n  VORHER (HEAD):")
    print(f"    Knoten: {sorted(old)}")
    print(f"    Kette : {_kette(old)}")
    print(f"    auf 't': {_t_knoten(old)}")
    print("\n  NACHHER:")
    print(f"    Knoten: {sorted(new)}")
    print(f"    Kette : {_kette(new)}")
    print(f"    auf 't': {_t_knoten(new) or 'keine'}")

    assert "reducedimension_dem" in old and "dropdimension_lcmask" in old
    assert old["filterbands_lcmask"]["arguments"]["data"] == \
        {"from_node": "reducedimension_dem"}

    assert "reducedimension_dem" not in new, sorted(new)
    assert "dropdimension_lcmask" not in new, sorted(new)
    # Der Nachfolger haengt direkt an renamelabels1, das per Retargeting
    # auf loadstac1 zeigt.
    assert new["filterbands_lcmask"]["arguments"]["data"] == \
        {"from_node": "renamelabels1"}
    assert new["renamelabels1"]["arguments"]["data"] == \
        {"from_node": "loadstac1"}
    assert new["lcmaskbuild1"]["arguments"]["data"] == \
        {"from_node": "filterbands_lcmask"}
    # Maskenziel und Ergebnis unveraendert.
    assert new["lcmask1"]["arguments"]["data"] == {"from_node": "loadcollection1"}
    assert new["saveresult1"]["arguments"]["data"] == {"from_node": "lcmask1"}

    diff = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    print(f"\n  Unterschiedliche Knoten: {diff}")
    assert diff == ["dropdimension_lcmask", "filterbands_lcmask",
                    "lcmaskbuild1", "reducedimension_dem"], diff
    print("  OK: beide t-Knoten weg, Rest der Kette unveraendert.")


def test_onthefly_unveraendert(tmp: Path) -> None:
    """load_collection: der Zweitcube hat eine echte Zeitachse."""
    print("\n--- Test 2: onthefly behaelt die t-Knoten ---")
    rb_head = _load_head_module(tmp)
    old = rb_head._build_workflow_pg(
        rb_head._load_bench_template(REGION, EXTENT), "lc_mask",
        region=REGION, dataset=LC)
    new = rb._build_workflow_pg(
        rb._load_bench_template(REGION, EXTENT), "lc_mask",
        region=REGION, dataset=LC)
    assert json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True)
    print(f"    auf 't': {_t_knoten(new)}")
    print(f"    Kette  : {_kette(new)}")
    print("  OK: byte-identisch zu HEAD, reduce_dimension + drop_dimension "
          "bleiben.")


def test_keine_t_knoten_mehr(tmp: Path) -> None:
    print("\n--- Test 3: load_stac-Graph ohne jeden t-Knoten ---")
    for workflow in ("lc_mask",):
        pg = _pp_graph(rb, tmp, f"t_{workflow}", workflow=workflow)
        assert not _t_knoten(pg), _t_knoten(pg)
        print(f"  {workflow} via local_pp: keine Knoten auf 't'")
    # full_pp baut denselben Zweig.
    path = tmp / "fullpp_lc_mask.json"
    rb.build_full_pp_scenario(REGION, "https://example.invalid/s2.json",
                              "https://example.invalid/dem.json", path,
                              extent_size=EXTENT, workflow="lc_mask",
                              dataset=LC)
    pg = json.loads(path.read_text())["process_graph"]
    assert not _t_knoten(pg), _t_knoten(pg)
    print("  lc_mask via full_pp : keine Knoten auf 't'")
    print("  OK")


def test_andere_workflows_unveraendert(tmp: Path) -> None:
    print("\n--- Test 4: uebrige Workflows byte-identisch zu HEAD ---")
    rb_head = _load_head_module(tmp)
    s2u, demu = ("https://example.invalid/s2.json",
                 "https://example.invalid/dem.json")
    n = 0
    for region in ("berlin", "wien"):
        for extent in ("small", EXTENT):
            for workflow in rb.WORKFLOWS:
                if workflow == "lc_mask":
                    continue
                ds = LC if workflow in rb.CATEGORICAL_WORKFLOWS else "dem"
                a = rb_head._build_workflow_pg(
                    rb_head._load_bench_template(region, extent), workflow,
                    region=region, dataset=ds)
                b = rb._build_workflow_pg(
                    rb._load_bench_template(region, extent), workflow,
                    region=region, dataset=ds)
                assert json.dumps(a, sort_keys=True) == \
                    json.dumps(b, sort_keys=True), \
                    f"_build_workflow_pg weicht ab: {region}/{extent}/{workflow}"
                n += 1
                for label, build in (
                    ("onthefly", lambda m, p, w=workflow, d=ds, e=extent,
                     r=region: m.build_onthefly_scenario(
                         r, p, extent_size=e, workflow=w, dataset=d)),
                    ("local_pp", lambda m, p, w=workflow, d=ds, e=extent,
                     r=region: m.build_local_pp_scenario(
                         r, STAC, p, extent_size=e, workflow=w, dataset=d)),
                    ("full_pp", lambda m, p, w=workflow, d=ds, e=extent,
                     r=region: m.build_full_pp_scenario(
                         r, s2u, demu, p, extent_size=e, workflow=w,
                         dataset=d)),
                ):
                    pa = tmp / f"head_{label}_{region}_{extent}_{workflow}.json"
                    pb = tmp / f"new_{label}_{region}_{extent}_{workflow}.json"
                    build(rb_head, pa)
                    build(rb, pb)
                    assert pa.read_bytes() == pb.read_bytes(), \
                        f"{label} weicht ab: {region}/{extent}/{workflow}"
                    n += 1
    print(f"  OK: {n} Graphen/Szenarien identisch "
          f"(lc_overlay + {len(rb.WORKFLOWS) - 2} DEM-Workflows, "
          f"2 Regionen x 2 Extents x 4 Bauwege).")


def test_workflow_erkennung(tmp: Path) -> None:
    print("\n--- Test 5: Workflow-Erkennung ---")
    pg = _pp_graph(rb, tmp, "detect")
    assert rb._detect_pg_workflow(pg) == "lc_mask", rb._detect_pg_workflow(pg)
    assert rb._detect_pg_workflow({"process_graph": pg}) == "lc_mask"
    print("  OK: lc_mask wird weiterhin als 'lc_mask' erkannt "
          "(lcmask1/lcmaskbuild1 tragen die Signatur).")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lcmask_time_"))
    print(f"Temp: {tmp}")
    try:
        test_vorher_nachher(tmp)
        test_onthefly_unveraendert(tmp)
        test_keine_t_knoten_mehr(tmp)
        test_andere_workflows_unveraendert(tmp)
        test_workflow_erkennung(tmp)
        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

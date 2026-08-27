#!/usr/bin/env python3
"""
test_lc_mask_bands.py - Standalone-Tests fuer den Bandnamenskonflikt in
lc_mask.

Befund: die lokale Referenz liefert 31,7 % gueltige Zellen (Flaechenanteil
der Klasse 10), CDSE dagegen 99,6 % - die Maske greift dort praktisch
nicht. Im erzeugten Graphen trugen S2-Cube und Masken-Cube beide das
Bandlabel "B04", weil rename_labels die Klassenkarte auf B04 umbenannte.

Geprueft wird:
  1. lc_mask: Klassenkarte behaelt ihren eigenen Bandnamen, Labels sind
     disjunkt, die Maskenbedingung haengt am Klassenband.
  2. rename_labels(source=...) ist bei lc_mask identisch zu lc_overlay -
     also nichts zu aendern, weder direkt noch ueber load_stac.
  3. lc_overlay und alle DEM-Workflows sind byte-identisch zur Version aus
     HEAD.
  4. Die Workflow-Erkennung liefert weiterhin "lc_mask".

Keine Backend-Aufrufe, kein Zugriff auf outputs/.
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


def _lc_mask_pg(module, dataset: str = LC) -> dict:
    """Der lc_mask-Graph, wie ihn local_pp erzeugt (load_stac + source=[])."""
    tpl = module._load_bench_template(REGION, EXTENT)
    return module._build_workflow_pg(tpl, "lc_mask", region=REGION,
                                     dataset=dataset)


def _relevant(pg: dict) -> dict:
    """Nur die Knoten, um die es hier geht - kompakt zum Anschauen."""
    keys = ["loadcollection1", "renamelabels1", "reducedimension_dem",
            "filterbands_lcmask", "lcmaskbuild1", "lcmask1", "saveresult1"]
    out = {}
    for k in keys:
        if k not in pg:
            continue
        n = pg[k]
        if k == "loadcollection1":
            out[k] = {"process_id": n["process_id"],
                      "bands": n["arguments"].get("bands")}
        elif k == "lcmaskbuild1":
            out[k] = {"process_id": n["process_id"],
                      "data": n["arguments"]["data"],
                      "process": "not(eq(x, %s))" % rb.LC_MASK_CLASS}
        else:
            out[k] = n
    return out


def test_vorher_nachher(tmp: Path) -> None:
    print("\n--- Test 1: lc_mask-Graph vorher (HEAD) und nachher ---")
    rb_head = _load_head_module(tmp)
    old = _lc_mask_pg(rb_head)
    new = _lc_mask_pg(rb)

    print("\n  VORHER (HEAD):")
    print("   " + json.dumps(_relevant(old), indent=2).replace("\n", "\n   "))
    print("\n  NACHHER:")
    print("   " + json.dumps(_relevant(new), indent=2).replace("\n", "\n   "))

    band = rb.DATASETS[LC]["band"]
    s2_bands = old["loadcollection1"]["arguments"]["bands"]

    # Vorher: Klassenkarte hiess wie das S2-Band.
    assert old["renamelabels1"]["arguments"]["target"] == ["B04"], old
    assert set(old["renamelabels1"]["arguments"]["target"]) & set(s2_bands)
    assert old["lcmaskbuild1"]["arguments"]["data"] == \
        {"from_node": "reducedimension_dem"}
    assert "filterbands_lcmask" not in old

    # Nachher: eigener Name, disjunkt zu S2, Bedingung am Klassenband.
    assert new["renamelabels1"]["arguments"]["target"] == [band], new
    assert not (set(new["renamelabels1"]["arguments"]["target"])
                & set(s2_bands)), "Labels ueberlappen weiterhin"
    assert new["filterbands_lcmask"]["arguments"]["bands"] == [band]
    assert new["filterbands_lcmask"]["arguments"]["data"] == \
        {"from_node": "reducedimension_dem"}
    assert new["lcmaskbuild1"]["arguments"]["data"] == \
        {"from_node": "filterbands_lcmask"}
    # Maskenziel und Ergebnis-Cube unveraendert: S2 wird maskiert.
    assert new["lcmask1"]["arguments"]["data"] == \
        {"from_node": "loadcollection1"}
    assert new["lcmask1"]["arguments"]["mask"] == \
        {"from_node": "lcmaskbuild1"}
    assert new["saveresult1"]["arguments"]["data"] == {"from_node": "lcmask1"}
    assert "merge1" not in new

    # Sonst aendert sich nichts am Graphen.
    diff = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    print(f"\n  Unterschiedliche Knoten: {diff}")
    assert diff == ["filterbands_lcmask", "lcmaskbuild1", "renamelabels1"], diff
    print(f"  OK: S2={s2_bands}, Klassenkarte=[{band!r}] - disjunkt.")


def test_source_wie_lc_overlay(tmp: Path) -> None:
    """source bleibt unveraendert - lc_mask und lc_overlay sind identisch."""
    print("\n--- Test 2: rename_labels.source ---")
    stac = "https://example.invalid/stac_item_berlin.json"
    nodes = {}
    for workflow in ("lc_overlay", "lc_mask"):
        # direkt gebaut (load_collection, dataset-Bandname als source)
        pg = rb._build_workflow_pg(rb._load_bench_template(REGION, EXTENT),
                                   workflow, region=REGION, dataset=LC)
        # ueber local_pp gebaut (load_stac, Builder setzt source=[])
        path = tmp / f"pp_{workflow}.json"
        rb.build_local_pp_scenario(REGION, stac, path, extent_size=EXTENT,
                                   workflow=workflow, dataset=LC)
        pp = json.loads(path.read_text())["process_graph"]
        nodes[workflow] = (pg["renamelabels1"]["arguments"],
                           pp["renamelabels1"]["arguments"])
        print(f"  {workflow:<11} direkt : source="
              f"{pg['renamelabels1']['arguments']['source']!r}, "
              f"target={pg['renamelabels1']['arguments']['target']!r}")
        print(f"  {workflow:<11} via pp : source="
              f"{pp['renamelabels1']['arguments']['source']!r}, "
              f"target={pp['renamelabels1']['arguments']['target']!r}")
    for i, wie in enumerate(("direkt", "ueber local_pp")):
        a = nodes["lc_overlay"][i]
        b = nodes["lc_mask"][i]
        assert a["source"] == b["source"], f"{wie}: {a} != {b}"
        assert a["target"] == b["target"], f"{wie}: {a} != {b}"
    print("  OK: lc_mask hat jetzt exakt dieselbe rename_labels-Belegung "
          "wie das funktionierende lc_overlay - kein Grund, source zu "
          "aendern.")


def test_andere_workflows_unveraendert(tmp: Path) -> None:
    """lc_overlay und alle DEM-Workflows bleiben bitgleich."""
    print("\n--- Test 3: uebrige Workflows byte-identisch zu HEAD ---")
    rb_head = _load_head_module(tmp)
    stac = "https://example.invalid/stac_item_berlin.json"
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
                         r, stac, p, extent_size=e, workflow=w, dataset=d)),
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
          f"({len(rb.WORKFLOWS) - 1} Workflows x 2 Regionen x 2 Extents "
          f"x 4 Bauwege).")


def test_workflow_erkennung(tmp: Path) -> None:
    """_detect_pg_workflow liefert weiterhin lc_mask (nicht lc_overlay)."""
    print("\n--- Test 4: Workflow-Erkennung ---")
    pg = _lc_mask_pg(rb)
    assert rb._detect_pg_workflow(pg) == "lc_mask", rb._detect_pg_workflow(pg)
    assert rb._detect_pg_workflow({"process_graph": pg}) == "lc_mask"
    ov = rb._build_workflow_pg(rb._load_bench_template(REGION, EXTENT),
                               "lc_overlay", region=REGION, dataset=LC)
    assert rb._detect_pg_workflow(ov) == "lc_overlay"
    print("  OK: lc_mask -> 'lc_mask', lc_overlay -> 'lc_overlay'.")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lc_mask_"))
    print(f"Temp: {tmp}")
    try:
        test_vorher_nachher(tmp)
        test_source_wie_lc_overlay(tmp)
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

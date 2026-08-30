#!/usr/bin/env python3
"""
test_stac_datacube.py - Standalone-Tests fuer die datacube-Extension im
STAC-Item (cube:dimensions).

Befund: bei lc_mask greift die Maske auf CDSE nicht (99,6 % Uebereinstimmung
mit dem unmaskierten S2 statt 31,7 %). Client und Backend leiten die
Dimensionen des per load_stac geladenen Cubes unterschiedlich ab - der
Client meldet ['x','y','bands'], das Backend legt intern eine Zeitachse an.
Joblog: "Dry-run load_stac: failed to parse cube metadata (No datacube
extension found in STAC object)" und "No cube:dimensions metadata".

Geprueft wird:
  1. Vorher/Nachher am erzeugten Item (stac_extensions + properties).
  2. cube:dimensions fuer JEDEN Datensatz und JEDES dem-Format, ohne
     Zeitdimension.
  3. Die Werte stammen aus dem Ziel-Grid, nicht aus Konstanten.
  4. pystac erkennt die Extension und liest x/y/bands - genau der Weg,
     den der openEO-Client geht.
  5. Die Collections (zarr-Wrapper, --dem-tiles) tragen den Block ebenfalls.
  6. Alles uebrige am Item ist byte-identisch zu HEAD.

Keine Backend-Aufrufe, kein Zugriff auf outputs/, keine Rasterdatei.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rasterio.transform import Affine

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import run_benchmark as rb

REGION = "berlin"
EPSG = 32633
EXTENT = {"west": 13.3, "south": 52.45, "east": 13.45, "north": 52.55}


def _grid(px: float = 10.0, py: float = 10.0, w: int = 1000, h: int = 800,
          x0: float = 380000.0, y0: float = 5820000.0) -> dict:
    """Ziel-Grid im Stil von _grid_from_dst_meta / read_s2_grid."""
    transform = Affine(px, 0.0, x0, 0.0, -py, y0)
    return {"transform": transform, "width": w, "height": h,
            "bounds": (x0, y0 - h * py, x0 + w * px, y0), "shape": (h, w)}


def _item(module, dataset="landcover", dem_format="gtiff", grid=None,
          item_id="dem_reprojected_berlin_x", href="https://h/dem.tif"):
    return module.build_stac_item(
        region=REGION, asset_href=href, epsg=EPSG, item_id=item_id,
        extent=EXTENT, dem_format=dem_format,
        grid=grid if grid is not None else _grid(), dataset=dataset)


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


def _show(label: str, item: dict) -> None:
    print(f"\n  {label}:")
    doc = {"stac_extensions": item["stac_extensions"],
           "properties": item["properties"]}
    print("   " + json.dumps(doc, indent=2).replace("\n", "\n   "))


def test_vorher_nachher(tmp: Path) -> None:
    print("\n--- Test 1: STAC-Item vorher (HEAD) und nachher ---")
    rb_head = _load_head_module(tmp)
    old = _item(rb_head)
    new = _item(rb)
    _show("VORHER (HEAD)", old)
    _show("NACHHER", new)

    assert "cube:dimensions" not in old["properties"]
    assert not any("datacube" in u for u in old["stac_extensions"])

    assert new["stac_extensions"] == old["stac_extensions"] + [
        rb.STAC_DATACUBE_EXTENSION], new["stac_extensions"]
    assert "cube:dimensions" in new["properties"]

    # Ausser den beiden neuen Feldern aendert sich nichts am Item.
    stripped = json.loads(json.dumps(new))
    stripped["properties"].pop("cube:dimensions")
    stripped["stac_extensions"] = old["stac_extensions"]
    assert json.dumps(stripped, sort_keys=True) == \
        json.dumps(old, sort_keys=True), "Item sonst veraendert"
    print("\n  OK: genau stac_extensions + properties['cube:dimensions'] "
          "kommen dazu, Assets/Geometrie/proj-Felder unveraendert.")


def test_alle_datensaetze_und_formate() -> None:
    print("\n--- Test 2: alle Datensaetze x alle dem-Formate ---")
    for dataset in sorted(rb.DATASETS):
        for dem_format in rb.DEM_FORMATS:
            it = _item(rb, dataset=dataset, dem_format=dem_format)
            props = it["properties"]
            dims = props.get("cube:dimensions")
            assert dims, f"{dataset}/{dem_format}: kein cube:dimensions"
            assert rb.STAC_DATACUBE_EXTENSION in it["stac_extensions"]
            # Keine Zeitdimension - weder als Schluessel noch als Typ.
            assert set(dims) == {"x", "y", "bands"}, sorted(dims)
            assert not any(d.get("type") == "temporal" for d in dims.values())
            assert dims["bands"]["values"] == [rb.DATASETS[dataset]["band"]]
            # Deckungsgleich mit den proj-Feldern desselben Items.
            bbox = props["proj:bbox"]
            assert dims["x"]["extent"] == [bbox[0], bbox[2]], dims["x"]
            assert dims["y"]["extent"] == [bbox[1], bbox[3]], dims["y"]
            assert dims["x"]["reference_system"] == props["proj:epsg"]
            assert dims["y"]["reference_system"] == props["proj:epsg"]
            t = props["proj:transform"]
            assert dims["x"]["step"] == abs(t[0]), dims["x"]
            assert dims["y"]["step"] == abs(t[4]), dims["y"]
            print(f"  {dataset:<10} {dem_format:<7} x/y-step="
                  f"{dims['x']['step']}/{dims['y']['step']}  "
                  f"bands={dims['bands']['values']}  "
                  f"crs={dims['x']['reference_system']}")
    print("  OK: Block ueberall vorhanden, nie mit Zeitachse.")


def test_werte_aus_dem_grid() -> None:
    """Nicht hartkodiert: anderes Grid -> andere Werte."""
    print("\n--- Test 3: Werte kommen aus dem Grid ---")
    g = _grid(px=20.0, py=60.0, w=500, h=100, x0=500000.0, y0=6000000.0)
    dims = _item(rb, grid=g)["properties"]["cube:dimensions"]
    assert dims["x"]["step"] == 20.0, dims["x"]
    assert dims["y"]["step"] == 60.0, dims["y"]
    assert dims["x"]["extent"] == [500000.0, 500000.0 + 500 * 20.0]
    assert dims["y"]["extent"] == [6000000.0 - 100 * 60.0, 6000000.0]
    print(f"  Grid 20x60 m, 500x100 px -> x={dims['x']['extent']} "
          f"step={dims['x']['step']}, y={dims['y']['extent']} "
          f"step={dims['y']['step']}")
    # Unterschiedliche Zellgroessen pro Achse werden getrennt uebernommen.
    assert dims["x"]["step"] != dims["y"]["step"]
    print("  OK: x und y unabhaengig aus dem Transform abgeleitet.")


def test_pystac_erkennung() -> None:
    """pystac erkennt Extension und Dimensionen - der Client-Weg."""
    print("\n--- Test 4: Erkennung durch pystac ---")
    try:
        import pystac
        from pystac.extensions.datacube import DatacubeExtension
    except ImportError as exc:
        print(f"  uebersprungen: pystac nicht installiert ({exc})")
        return
    it = pystac.Item.from_dict(_item(rb))
    assert DatacubeExtension.has_extension(it), it.stac_extensions
    dims = DatacubeExtension.ext(it).dimensions
    kinds = {name: d.dim_type for name, d in dims.items()}
    print(f"  pystac {pystac.__version__}: has_extension=True, "
          f"dimensions={kinds}")
    assert set(dims) == {"x", "y", "bands"}, sorted(dims)
    assert not any(getattr(d, "dim_type", None) == "temporal"
                   for d in dims.values()), kinds
    assert dims["x"].extent == [380000.0, 390000.0], dims["x"].extent
    assert dims["bands"].to_dict()["values"] == ["MAP"]
    print("  OK: Extension deklariert und maschinell lesbar, ohne t.")


def test_collections() -> None:
    print("\n--- Test 5: Collections (zarr-Wrapper, --dem-tiles) ---")
    it = _item(rb, dem_format="zarr", href="https://h/dem.zarr")
    coll = rb.build_dem_stac_collection("c1", "https://h/c1.json", it,
                                        "https://h/i1.json")
    assert rb.STAC_DATACUBE_EXTENSION in coll["stac_extensions"]
    assert coll["cube:dimensions"] == it["properties"]["cube:dimensions"]
    print(f"  zarr-Collection: cube:dimensions = Item-Block, "
          f"x={coll['cube:dimensions']['x']['extent']}")

    # Zwei Kacheln nebeneinander -> Collection deckt beide ab.
    left = _item(rb, grid=_grid(w=500, x0=380000.0), item_id="t0")
    right = _item(rb, grid=_grid(w=500, x0=385000.0), item_id="t1")
    tiles = rb.build_dem_tiles_collection(
        "c2", "https://h/c2.json",
        [(left, "https://h/t0.json"), (right, "https://h/t1.json")])
    dims = tiles["cube:dimensions"]
    assert rb.STAC_DATACUBE_EXTENSION in tiles["stac_extensions"]
    assert dims["x"]["extent"] == [380000.0, 390000.0], dims["x"]
    assert dims["y"]["extent"] == left["properties"]["cube:dimensions"]["y"]["extent"]
    assert dims["x"]["step"] == 10.0
    print(f"  Kachel-Collection: Kachel-x {left['properties']['cube:dimensions']['x']['extent']}"
          f" + {right['properties']['cube:dimensions']['x']['extent']}"
          f" -> Collection-x {dims['x']['extent']}")
    # Die Items behalten ihre eigene Geometrie.
    assert left["properties"]["proj:bbox"] != right["properties"]["proj:bbox"]
    print("  OK: Collection-Block ist die Vereinigung, Items unveraendert.")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="stac_datacube_"))
    print(f"Temp: {tmp}")
    try:
        test_vorher_nachher(tmp)
        test_alle_datensaetze_und_formate()
        test_werte_aus_dem_grid()
        test_pystac_erkennung()
        test_collections()
        print("\nALLE TESTS BESTANDEN")
        return 0
    except AssertionError as exc:
        print(f"\nFEHLGESCHLAGEN: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

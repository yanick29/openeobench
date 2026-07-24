#!/usr/bin/env python3
"""
test_dem_format.py

Standalone-Verifikation fuer das --dem-format Experiment (nur lokal, ohne
CDSE). Reprojiziert ein synthetisches DEM in einen In-Memory Puffer und
schreibt daraus DREI Assets:

  1. GeoTIFF (Layout striped)          .tif
  2. Zarr-Verzeichnis-Store (xarray)   .zarr/
  3. NetCDF-4 Datei (xarray)           .nc

Danach werden die drei Assets EINZELN wieder eingelesen und verglichen:
  - Pixelwerte per np.array_equal
  - SHA-256 des Rohpuffers
  - Georeferenz (CRS-Rekonstruktion + affine Transform aus Zarr/NetCDF)

Zeigt zusaetzlich die STAC-Item media_types die build_stac_item pro Format
liefert (damit im Log dokumentiert ist welchen Typ CDSE spaeter zu sehen
bekommt).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

sys.path.insert(0, str(Path(__file__).parent))

from run_benchmark import (
    DEM_FORMATS,
    _DEM_FORMAT_EXT,
    _DEM_FORMAT_MEDIA_TYPE,
    _check_dem_format_deps,
    _grid_from_dst_meta,
    _link_item_into_collection,
    _reproject_dem_to_array,
    _split_dem_into_tiles,
    _tile_grid_layout,
    _verify_tile_union_identity,
    _wgs84_extent_from_meta,
    _write_dem_with_layout,
    _write_dem_as_zarr,
    _write_dem_as_netcdf,
    _inspect_asset_size,
    build_dem_stac_collection,
    build_dem_tiles_collection,
    build_stac_item,
)


def _make_synthetic_dem(path: Path) -> None:
    """Deterministisches int16 GeoTIFF (EPSG:4326, ca. Berlin)."""
    width, height = 1200, 1200
    ys = np.arange(height, dtype=np.int32).reshape(-1, 1)
    xs = np.arange(width, dtype=np.int32).reshape(1, -1)
    data = ((xs * 3 + ys * 7) % 32000).astype(np.int16)
    transform = from_bounds(13.30, 52.45, 13.45, 52.55, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "int16",
        "count": 1,
        "width": width,
        "height": height,
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -32768,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _sha256(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def _read_gtiff(path: Path) -> tuple:
    with rasterio.open(path) as src:
        data = src.read()
        crs = src.crs
        transform = src.transform
    return data, crs, transform


def _read_xarray_asset(path: Path) -> tuple:
    """Liest Zarr-Store oder NetCDF via xarray zurueck. Gibt (data_3d, crs_str,
    transform_tuple) zurueck. data_3d hat immer eine band-Achse (count, y, x)
    fuer den direkten Vergleich mit rasterio.read().

    mask_and_scale=False damit xarray den Fill-Wert nicht in NaN wandelt
    und den Datentyp nicht nach float upcasted - wir wollen die rohen
    int16-Werte fuer den Byte-Vergleich."""
    import xarray as xr
    if path.is_dir():
        ds = xr.open_zarr(str(path), mask_and_scale=False)
    else:
        ds = xr.open_dataset(str(path), engine="netcdf4",
                             mask_and_scale=False)
    try:
        da = ds["DEM"]
        arr = da.values
        if arr.ndim == 2:
            arr = arr[np.newaxis, :, :]
        # Grid-Mapping Variable spatial_ref lesen
        sr_attrs = ds["spatial_ref"].attrs if "spatial_ref" in ds else {}
        crs = sr_attrs.get("crs_wkt") or sr_attrs.get("spatial_ref") or ""
        geot_str = sr_attrs.get("GeoTransform", "")
        transform = None
        if geot_str:
            parts = [float(v) for v in geot_str.split()]
            if len(parts) == 6:
                # GDAL GeoTransform: c a b f d e (rasterio order)
                c, a, b, f, d, e = parts
                transform = (a, b, c, d, e, f)  # rasterio.Affine argument order
    finally:
        ds.close()
    return arr, crs, transform


class _RangeHTTPHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + HTTP-Range-Support (206 Partial Content).

    Pythons http.server ignoriert Range-Header und liefert immer 200/voll.
    GDALs /vsicurl/ liest aber chunkweise per Range-Request - genau wie
    spaeter CDSE gegen den nginx des Benchmark-Hosts. Nur GET/HEAD auf
    Dateien, single-range ("bytes=start-end"), mehr braucht GDAL nicht.
    """

    def log_message(self, *args):  # kein Request-Spam im Testlog
        pass

    def do_HEAD(self):
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()

    def do_GET(self):
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            s, _, e = spec.partition("-")
            if s:
                start = int(s)
                end = int(e) if e else size - 1
            else:  # Suffix-Range "bytes=-N"
                start = max(0, size - int(e))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_error(416)
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(length))


def _read_zarr_via_vsicurl(store: Path) -> tuple:
    """Oeffnet den Zarr-Store per GDAL /vsicurl/ ueber einen lokalen
    Range-HTTP-Server - OHNE STAC-Item, die Georeferenz muss also aus dem
    Store selbst kommen. Gibt (crs, transform_tuple, data_3d) zurueck.

    Seit Versuch 5 (Store ohne .zmetadata) geht der Open ueber den
    direkten ARRAY-Subpfad ZARR:"...":/DEM: ohne konsolidierte Metadaten
    kann GDAL den Store-ROOT ueber HTTP nicht oeffnen (404 - kein
    Directory-Listing), der Subpfad braucht nur die einzelnen
    .zarray/.zattrs und liefert CRS+Transform weiterhin aus dem Store
    (lokal belegt, GDAL 3.12). Pfad case-sensitiv wie auf dem nginx."""
    handler = partial(_RangeHTTPHandler, directory=str(store.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        url = f'ZARR:"/vsicurl/http://127.0.0.1:{port}/{store.name}":/DEM'
        with rasterio.open(url) as ds:
            arr = ds.read()
            return ds.crs, tuple(ds.transform)[:6], arr
    finally:
        server.shutdown()
        server.server_close()


def _log_stac_media_types() -> None:
    print("\n[stac] media_types die build_stac_item pro Format liefert:")
    for fmt in DEM_FORMATS:
        item = build_stac_item(
            region="berlin", asset_href=f"http://example.org/asset{_DEM_FORMAT_EXT[fmt]}",
            epsg=32633,
            item_id=f"test_{fmt}",
            dem_format=fmt,
        )
        asset = item["assets"]["data"]
        print(f"  {fmt:8s}  media_type={asset['type']:36s}  href={asset['href']}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dem_format_test_"))
    print(f"[test] tmp dir: {tmp}")

    # 0. Optionale Deps pruefen
    for fmt in ("zarr", "netcdf"):
        try:
            _check_dem_format_deps(fmt)
        except ImportError as exc:
            print(f"[test] FEHLER: {exc}")
            print(f"[test] Bitte oben genannte Pakete installieren und "
                  f"Test erneut ausfuehren.")
            return 1
    print("[test] Optionale Pakete (xarray, zarr, netcdf4) OK.")

    # 1. Synthetisches DEM
    src_tif = tmp / "src_dem.tif"
    _make_synthetic_dem(src_tif)
    print(f"[test] synthetisches DEM erzeugt: {src_tif} "
          f"({src_tif.stat().st_size / 1024:.1f} KB)")

    # 2. EINE Reprojektion, Puffer wird von allen 3 Writern geteilt.
    print("\n[test] EINE Reprojektion in In-Memory-Puffer "
          "(EPSG:4326 -> EPSG:32633, nearest, 10 m Snap)")
    data, dst_meta = _reproject_dem_to_array(
        str(src_tif), "EPSG:32633",
        resampling="nearest", target_resolution=10.0,
    )
    print(f"  Puffer: shape={data.shape}, dtype={data.dtype}, "
          f"crs={dst_meta['crs']}, "
          f"transform=({dst_meta['transform'].a:.1f}, {dst_meta['transform'].e:.1f})")

    ref_hash = _sha256(data)
    print(f"  In-Memory-SHA256: {ref_hash}")

    # 3. Drei Writer, drei Asset-Pfade
    paths = {}
    for fmt in DEM_FORMATS:
        target = tmp / f"asset{_DEM_FORMAT_EXT[fmt]}"
        if fmt == "gtiff":
            _write_dem_with_layout(data, dst_meta, str(target), layout="striped")
        elif fmt == "zarr":
            _write_dem_as_zarr(data, dst_meta, str(target))
        elif fmt == "netcdf":
            _write_dem_as_netcdf(data, dst_meta, str(target))
        info = _inspect_asset_size(str(target))
        marker = "dir" if info["is_directory"] else "file"
        print(f"\n[test] {fmt:8s} geschrieben: {target}  "
              f"({marker}, {info['size_bytes'] / 1024:.1f} KB, "
              f"{info['num_files']} Dateien)")
        paths[fmt] = target

    # 4. Wieder einlesen + vergleichen
    print("\n[test] Wieder einlesen + Pixel-Vergleich gegen In-Memory-Puffer")
    all_ok = True
    for fmt, path in paths.items():
        if fmt == "gtiff":
            arr, crs, transform = _read_gtiff(path)
            crs_str = str(crs) if crs else ""
        else:
            arr, crs_str, transform = _read_xarray_asset(path)
        arr_hash = _sha256(arr)
        equal = np.array_equal(arr, data)
        marker = "OK" if equal and arr_hash == ref_hash else "MISMATCH"
        print(f"  {fmt:8s}  sha256={arr_hash}  array_equal={equal}  "
              f"crs={(crs_str or '(none)')[:40]}...  transform_present={transform is not None}  "
              f"[{marker}]")
        if not equal or arr_hash != ref_hash:
            all_ok = False

    # 5. Georeferenz-Rueckwaerts-Check (Zarr/NetCDF): Transform-Parameter
    print("\n[test] Georeferenz-Vergleich (Zarr/NetCDF -> rasterio)")
    src_a = dst_meta["transform"].a
    src_e = dst_meta["transform"].e
    src_c = dst_meta["transform"].c
    src_f = dst_meta["transform"].f
    print(f"  In-Memory: a={src_a}, e={src_e}, origin=({src_c}, {src_f})")
    for fmt in ("zarr", "netcdf"):
        _, crs_str, tr = _read_xarray_asset(paths[fmt])
        if tr is None:
            print(f"  {fmt:8s}  KEINE GeoTransform in spatial_ref!")
            all_ok = False
            continue
        a, b, c, d, e, f = tr
        matches = (abs(a - src_a) < 1e-6 and abs(e - src_e) < 1e-6
                   and abs(c - src_c) < 1e-6 and abs(f - src_f) < 1e-6)
        marker = "OK" if matches else "MISMATCH"
        print(f"  {fmt:8s}  a={a}, e={e}, origin=({c}, {f})  crs_wkt={(crs_str or '')[:30]}...  [{marker}]")
        if not matches:
            all_ok = False

    # 6. Zarr: Georeferenz muss aus dem STORE ALLEIN kommen (kein STAC-Item).
    #    6a. Konventions-Attribute liegen wirklich in den .zattrs
    #    6b. GDAL /vsicurl/-Open ueber lokalen Range-HTTP-Server liefert
    #        CRS + Transform + identische Pixel
    print("\n[test] zarr-Georeferenz OHNE STAC-Item (GeoZarr/CF + GDAL _CRS)")

    # 6c (Versuch 5): Store ist UNKONSOLIDIERT - kein .zmetadata, und
    # 'shape' liegt in jeder einzelnen .zarray (Kern der Hypothese gegen
    # CDSEs "missing key: 'shape'").
    zmeta_absent = not (paths["zarr"] / ".zmetadata").exists()
    shape_ok = True
    for arr_dir in sorted(p for p in paths["zarr"].iterdir() if p.is_dir()):
        zarray = arr_dir / ".zarray"
        has_shape = (zarray.exists()
                     and "shape" in json.loads(zarray.read_text()))
        if not has_shape:
            shape_ok = False
        print(f"  {arr_dir.name}/.zarray: shape={'OK' if has_shape else 'FEHLT'}")
    print(f"  .zmetadata abwesend={zmeta_absent}  "
          f"[{'OK' if zmeta_absent and shape_ok else 'MISMATCH'}]")
    if not (zmeta_absent and shape_ok):
        all_ok = False

    dem_attrs = json.loads((paths["zarr"] / "DEM" / ".zattrs").read_text())
    sr_attrs = json.loads((paths["zarr"] / "spatial_ref" / ".zattrs").read_text())
    conv_ok = (
        isinstance(dem_attrs.get("_CRS"), dict)
        and "wkt" in dem_attrs["_CRS"]
        and dem_attrs.get("grid_mapping") == "spatial_ref"
        and sr_attrs.get("grid_mapping_name") not in (None, "", "unknown")
        and "crs_wkt" in sr_attrs
        and "GeoTransform" in sr_attrs
    )
    print(f"  .zattrs: _CRS keys={sorted(dem_attrs.get('_CRS', {}))}, "
          f"grid_mapping_name={sr_attrs.get('grid_mapping_name')!r}, "
          f"GeoTransform={'GeoTransform' in sr_attrs}  "
          f"[{'OK' if conv_ok else 'MISMATCH'}]")
    if not conv_ok:
        all_ok = False

    try:
        vs_crs, vs_tr, vs_arr = _read_zarr_via_vsicurl(paths["zarr"])
    except Exception as exc:
        print(f"  /vsicurl-Open FEHLGESCHLAGEN: {type(exc).__name__}: {exc}")
        all_ok = False
    else:
        epsg_ok = vs_crs is not None and vs_crs.to_epsg() == 32633
        a, b, c, d, e, f = vs_tr
        tr_ok = (abs(a - src_a) < 1e-6 and abs(e - src_e) < 1e-6
                 and abs(c - src_c) < 1e-6 and abs(f - src_f) < 1e-6
                 and abs(b) < 1e-6 and abs(d) < 1e-6)
        px_ok = np.array_equal(vs_arr, data) and _sha256(vs_arr) == ref_hash
        marker = "OK" if epsg_ok and tr_ok and px_ok else "MISMATCH"
        print(f"  /vsicurl: crs={vs_crs}  epsg_ok={epsg_ok}  "
              f"transform=({a}, {e}, origin=({c}, {f}))  transform_ok={tr_ok}  "
              f"pixel_identisch={px_ok}  [{marker}]")
        if not (epsg_ok and tr_ok and px_ok):
            all_ok = False

    # 7. zarr: STAC-Collection-Wrapper. Bei dem_format=zarr zeigt load_stac
    #    auf eine Collection statt aufs Item - hier wird lokal geprueft,
    #    dass Collection + eingebettetes Item strukturell valide sind und
    #    alle proj-/Band-Metadaten tragen.
    print("\n[test] STAC Collection fuer zarr (Struktur, Links, proj/eo:bands)")
    item_url = "http://example.org/stac_item_berlin_TEST.json"
    coll_url = "http://example.org/stac_collection_berlin_TEST.json"
    coll_id = "dem_collection_berlin_TEST"
    item = build_stac_item(
        region="berlin", asset_href="http://example.org/asset.zarr",
        epsg=32633, item_id="dem_reprojected_berlin_TEST",
        dem_format="zarr", grid=_grid_from_dst_meta(dst_meta),
    )
    _link_item_into_collection(item, item_url, coll_id, coll_url)
    coll = build_dem_stac_collection(coll_id, coll_url, item, item_url)

    def _rels(obj):
        return {l["rel"]: l["href"] for l in obj.get("links", [])}

    coll_rels = _rels(coll)
    item_rels = _rels(item)
    proj_keys = ("proj:epsg", "proj:shape", "proj:bbox", "proj:transform")
    checks = {
        "coll_pflichtfelder": all(coll.get(k) for k in (
            "type", "stac_version", "id", "description", "license",
            "extent", "links")) and coll["type"] == "Collection",
        "coll_extent": (isinstance(coll["extent"]["spatial"]["bbox"][0], list)
                        and len(coll["extent"]["spatial"]["bbox"][0]) == 4
                        and coll["extent"]["temporal"]["interval"][0][0]
                        is not None),
        "coll_links_absolut": all(h.startswith("http")
                                  for h in coll_rels.values()),
        "coll_rel_item_zeigt_auf_item": coll_rels.get("item") == item_url,
        "coll_rel_self_root": (coll_rels.get("self") == coll_url
                               and coll_rels.get("root") == coll_url),
        "coll_item_assets_proj_bands": all(
            k in coll.get("item_assets", {}).get("data", {})
            for k in proj_keys + ("eo:bands",)),
        "coll_summaries_epsg": coll.get("summaries", {}).get("proj:epsg")
                               == [32633],
        "item_in_collection": (item.get("collection") == coll_id
                               and item_rels.get("collection") == coll_url
                               and item_rels.get("parent") == coll_url
                               and item_rels.get("self") == item_url),
        "item_proj_felder_erhalten": all(
            k in item["properties"] and k in item["assets"]["data"]
            for k in proj_keys),
        "item_eo_bands_erhalten": item["assets"]["data"].get("eo:bands")
                                  == [{"name": "DEM"}],
    }
    for name, ok in checks.items():
        print(f"  {name:32s} [{'OK' if ok else 'MISMATCH'}]")
        if not ok:
            all_ok = False

    # pystac-Schema-Validierung wenn moeglich (braucht jsonschema + Netz
    # fuer die STAC-Schemas). Ein Validierungs-FEHLER ist ein Testfehler;
    # fehlende Pakete/Netz nur ein Skip - die Pflichtfeld-Checks oben
    # laufen immer.
    try:
        import pystac
        from pystac.errors import STACValidationError
        try:
            pystac.validation.validate_dict(coll)
            pystac.validation.validate_dict(item)
            print("  pystac-Schema-Validierung: Collection + Item OK")
        except STACValidationError as exc:
            print(f"  pystac-Schema-Validierung FEHLGESCHLAGEN: {exc}")
            all_ok = False
        except Exception as exc:
            print(f"  pystac-Validierung uebersprungen "
                  f"(kein Netz/Schema-Download?): {type(exc).__name__}")
    except ImportError:
        print("  pystac/jsonschema nicht installiert - nur Pflichtfeld-Checks")

    # 8. --dem-tiles: 2x2-Zerlegung aus demselben Puffer. Pflichttest:
    #    Vereinigung der Kacheln == Einzel-DEM, bitgenau - einmal
    #    in-memory (dieselbe Verifikation wie im Benchmark vor dem
    #    Upload) und einmal ueber den vollen Write/Read-Roundtrip der
    #    vier GeoTIFFs (Fenster-Offsets unabhaengig aus den Geotransforms
    #    der geschriebenen Dateien hergeleitet).
    print("\n[test] --dem-tiles: 2x2-Kacheln (Union bitgenau == Einzel-DEM?)")
    assert _tile_grid_layout(4) == (2, 2)
    tiles = _split_dem_into_tiles(data, dst_meta, 4)
    if not _verify_tile_union_identity(tiles, data, dst_meta):
        all_ok = False

    t0 = dst_meta["transform"]
    assembled = np.zeros_like(data)
    cover = np.zeros((dst_meta["height"], dst_meta["width"]), dtype=np.uint8)
    tile_grids = []
    for i, (tile_data, tile_meta) in enumerate(tiles):
        tile_path = tmp / f"tile{i}.tif"
        _write_dem_with_layout(tile_data, tile_meta, str(tile_path),
                               layout="striped")
        arr, crs, tr = _read_gtiff(tile_path)
        col_off = int(round((tr.c - t0.c) / t0.a))
        row_off = int(round((tr.f - t0.f) / t0.e))
        th, tw = arr.shape[1], arr.shape[2]
        assembled[:, row_off:row_off + th, col_off:col_off + tw] = arr
        cover[row_off:row_off + th, col_off:col_off + tw] += 1
        tile_grids.append(_grid_from_dst_meta(tile_meta))
        print(f"  tile{i}: shape=({th}, {tw})  offset=({row_off}, {col_off})  "
              f"crs={crs}")
    roundtrip_ok = (bool((cover == 1).all())
                    and np.array_equal(assembled, data)
                    and _sha256(assembled) == ref_hash)
    print(f"  Roundtrip-Union: sha256={_sha256(assembled)}  "
          f"array_equal={np.array_equal(assembled, data)}  "
          f"abdeckung_1x={bool((cover == 1).all())}  "
          f"[{'OK' if roundtrip_ok else 'MISMATCH'}]")
    if not roundtrip_ok:
        all_ok = False

    # 8b. STAC-Struktur fuer die Kacheln: Collection mit VIER Items (je
    #     ein Asset). Begruendung s. build_dem_tiles_collection - mehrere
    #     Assets gleichen Bandnamens in EINEM Item wuerde der
    #     geopyspark-Treiber bis auf das erste stumm verwerfen.
    print("\n[test] STAC Collection fuer --dem-tiles "
          "(4 Items, per-Kachel proj-Felder)")
    tiles_coll_url = "http://example.org/stac_collection_berlin_TILES.json"
    tiles_coll_id = "dem_collection_berlin_TILES"
    items_with_urls = []
    for i, (_tile_data, tile_meta) in enumerate(tiles):
        item_url_i = f"http://example.org/stac_item_berlin_TILES_tile{i}.json"
        itm = build_stac_item(
            region="berlin",
            asset_href=f"http://example.org/asset_tile{i}.tif",
            epsg=32633, item_id=f"dem_reprojected_berlin_TILES_tile{i}",
            extent=_wgs84_extent_from_meta(tile_meta),
            dem_format="gtiff", grid=_grid_from_dst_meta(tile_meta),
        )
        _link_item_into_collection(itm, item_url_i, tiles_coll_id,
                                   tiles_coll_url)
        items_with_urls.append((itm, item_url_i))
    tiles_coll = build_dem_tiles_collection(tiles_coll_id, tiles_coll_url,
                                            items_with_urls)

    tc_rels = [l for l in tiles_coll["links"] if l["rel"] == "item"]
    per_item_ok = True
    for i, ((itm, item_url_i), grid) in enumerate(zip(items_with_urls,
                                                      tile_grids)):
        a = itm["assets"]["data"]
        left, bottom, right, top = grid["bounds"]
        ok = (a.get("proj:shape") == [grid["height"], grid["width"]]
              and a.get("proj:bbox") == [left, bottom, right, top]
              and itm["properties"].get("proj:shape") == [grid["height"],
                                                          grid["width"]]
              and a.get("eo:bands") == [{"name": "DEM"}]
              and itm.get("collection") == tiles_coll_id
              and all(l["href"].startswith("http")
                      for l in itm["links"]))
        if not ok:
            per_item_ok = False
        print(f"  tile{i}: proj:shape/bbox passend zum Ausschnitt "
              f"[{'OK' if ok else 'MISMATCH'}]")
    union_bbox = tiles_coll["extent"]["spatial"]["bbox"][0]
    item_bboxes = [itm["bbox"] for itm, _ in items_with_urls]
    coll_checks = {
        "coll_4_rel_item_links": (
            len(tc_rels) == 4
            and [l["href"] for l in tc_rels]
            == [u for _, u in items_with_urls]),
        "coll_links_absolut": all(l["href"].startswith("http")
                                  for l in tiles_coll["links"]),
        "coll_bbox_union": union_bbox == [
            min(b[0] for b in item_bboxes), min(b[1] for b in item_bboxes),
            max(b[2] for b in item_bboxes), max(b[3] for b in item_bboxes)],
        "coll_item_assets_ohne_geometrie": (
            "proj:shape" not in tiles_coll["item_assets"]["data"]
            and "proj:bbox" not in tiles_coll["item_assets"]["data"]
            and tiles_coll["item_assets"]["data"].get("eo:bands")
            == [{"name": "DEM"}]),
        "per_item_proj_felder": per_item_ok,
    }
    for name, ok in coll_checks.items():
        print(f"  {name:32s} [{'OK' if ok else 'MISMATCH'}]")
        if not ok:
            all_ok = False

    try:
        import pystac
        from pystac.errors import STACValidationError
        try:
            pystac.validation.validate_dict(tiles_coll)
            for itm, _ in items_with_urls:
                pystac.validation.validate_dict(itm)
            print("  pystac-Schema-Validierung: Tiles-Collection + 4 Items OK")
        except STACValidationError as exc:
            print(f"  pystac-Schema-Validierung FEHLGESCHLAGEN: {exc}")
            all_ok = False
        except Exception as exc:
            print(f"  pystac-Validierung uebersprungen "
                  f"(kein Netz/Schema-Download?): {type(exc).__name__}")
    except ImportError:
        print("  pystac/jsonschema nicht installiert - nur Pflichtfeld-Checks")

    # 9. STAC-Media-Types
    _log_stac_media_types()

    print()
    if all_ok:
        print("[test] ALLE FORMATE PIXEL-IDENTISCH + GEOREFERENZ ERHALTEN.")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
    print("[test] FEHLER: mindestens ein Format weicht ab.")
    print(f"[test] Debug-Ordner bleibt: {tmp}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

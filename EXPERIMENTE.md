# Experimente und Testdimensionen des Benchmark-Tools

Stand: 2026-07-28. Grundlage ist ausschließlich der Code im Arbeitsstand
(`run_benchmark.py`, 4857 Zeilen; `database.py`; `accuracy_calculator.py`;
`analyze.py`). Zeilenangaben beziehen sich auf diesen Stand.

Alle Aussagen sind am Code belegt. Wo etwas nicht aus dem Code ableitbar ist,
steht das ausdrücklich dabei. Die Spalte „Status auf CDSE" in Abschnitt 4 ist
bewusst leer (`TBD`) — welche Kombination auf dem Backend tatsächlich
durchläuft, lässt sich aus dem Code nicht bestimmen.

---

## 1. Was das Tool misst

Gemessen wird, **wie teuer und wie genau** die Zusammenführung zweier Raster
mit unterschiedlichem Koordinatenbezugssystem auf dem Copernicus Data Space
Ecosystem (CDSE) ist:

- **Sentinel-2 L2A**, Band B04, liegt in UTM vor (`scenarios/bench_onthefly_*.json`,
  Knoten `loadcollection1`).
- **Copernicus DEM 30 m** (`COPERNICUS_30`) liegt in EPSG:4326 vor
  (Knoten `loadcollection2`).

Die Kernfrage ist, **wo** die dafür nötige Reprojektion stattfindet — im Backend
oder lokal beim Client — und was das kostet. Dafür implementiert
`run_benchmark.py` vier Strategien (`ALL_STRATEGIES` Z. 42, `EXTRA_STRATEGIES`
Z. 46) und variiert sie über neun Regionen, fünf Ausdehnungen und sieben
Workflows.

Erfasste Messgrößen (Tabellen und Spalten in `database.py`, `create_database`):

| Gruppe | Spalten in `runs` |
|---|---|
| Backend-Zeiten | `submit_time`, `queue_time`, `processing_time`, `job_execution_time`, `download_time`, `total_time` |
| Lokale Zeiten | `preprocessing_time`, `dem_download_time`, `s2_download_time` |
| Backend-Kosten | `credits`, `cpu_seconds`, `duration_backend`, `input_pixels_mp`, `max_memory_gb` |
| Output-Geometrie | `output_crs`, `pixel_shape`, `bounding_box`, `num_output_files` |
| Versuchsparameter | `crs_strategy`, `run_type`, `extent_size`, `workflow`, `local_resampling`, `target_crs`, `dem_layout`, `dem_format`, `dem_snap`, `dem_tiles` |
| Reproduzierbarkeit | `git_commit`, `openeo_version`, `rasterio_version`, `numpy_version`, `proj_version`, `environment_json` |

Drei weitere Tabellen (`database.py`, `create_database`):

- `band_statistics` — min/max/mean/stddev/valid_percent je Band und Ausgabedatei,
  aus den STAC-Assets der `job-results.json` (`import_run`).
- `accuracy` — `rmse`, `mae`, `reference_file`, `reference_run_id` je Vergleich
  (befüllt von `run_benchmark._persist_accuracy`); die Spalten `max_diff`,
  `mean_diff`, `correlation` füllt **nur** der Standalone-CLI
  `accuracy_calculator.save_to_db`.
- `nginx_access_log` — HTTP-Zugriffe des Backends auf die selbst gehosteten
  Assets, geholt per SSH aus dem nginx-Log (`database.import_nginx_access_log`).
  Damit werden Anzahl und Verteilung der Range-Requests gemessen.

Die statistische Auswertung liegt in `analyze.py`: Mediane mit
Bootstrap-95-%-Konfidenzintervallen (`bootstrap_median_ci`, 2000 Iterationen)
und Mann-Whitney-U-Tests (`print_significance_tests`).

**Zeitmessung:** `openeotest.py` pollt den Jobstatus in einem festen
5-Sekunden-Intervall (im Upstream war es ein exponentieller Backoff) — der
maximale Messfehler pro Job liegt damit bei rund 5 s.

---

## 2. Experimentdimensionen

### 2.1 Übersicht aller 30 CLI-Flags

Alle Flags sind in `run_benchmark.main()` definiert (Z. 4470–4699). Es gibt
keine Subparser, keine Argument-Gruppen und keine gegenseitig ausschließenden
Gruppen.

| Dimension | CLI-Flag | Mögliche Werte | Default |
|---|---|---|---|
| **Strategie** | `--strategy` | `onthefly`, `local_preprocessing`, `full_preprocessing`, `local_reference`, `all` | `all` |
| | `--include-full-pp` | `auto`, `yes`, `no` | `auto` |
| | `--dem-cache` | Flag | aus |
| **Region** | `--region` | 9 Werte, s. 2.2 | `berlin` |
| **Ausdehnung** | `--extent-size` | `small`, `medium`, `large`, `xlarge`, `xxlarge` | `medium` |
| **Workflow** | `--workflow` | 7 Werte, s. 2.4 | `merge_add` |
| **DEM-Format** | `--dem-format` | `gtiff`, `zarr`, `netcdf` | `gtiff` |
| **DEM-Layout** | `--dem-layout` | `striped`, `tiled_uncompressed`, `cog` | `striped` |
| **Kachelung** | `--dem-tiles` | Ganzzahl ≥ 1 | `1` |
| **Gitterausrichtung** | `--snap-dem-to-s2` | Flag | aus |
| | `--resample-s2-to-dem` | Flag | aus |
| **Ziel-CRS** | `--target-crs` | `EPSG:xxxx`, `epsg:xxxx` oder `xxxx` | `None` (= Regions-UTM) |
| | `--force-target-crs` | Flag | aus |
| | `--reproject-s2` | Flag | aus |
| **Resampling** | `--local-resampling` | `nearest`, `bilinear`, `cubic` | `nearest` |
| **Wiederholungen** | `--repeat` | Ganzzahl | `1` |
| | `--run-type` | `cold`, `hot`, `auto` | `auto` |
| **Timeouts** | `--job-timeout` | Sekunden | `3600` |
| **Plattenschutz** | `--min-free-gb` | Gleitkommazahl GB; ≤ 0 = aus | `20.0` |
| **Cleanup** | `--cleanup-after-accuracy` | Flag | aus |
| | `--dry-run-cleanup` | Flag | aus |
| **Genauigkeit** | `--accuracy-check` | Flag | aus |
| | `--reference-check` | Flag | aus |
| **Hosting/Upload** | `--host` | ssh-Ziel | ENV `BENCHMARK_HOST`, sonst fest verdrahtet |
| | `--web-path` | Remote-Verzeichnis | ENV `BENCHMARK_WEB_PATH` |
| | `--url-base` | öffentliche URL-Basis | ENV `BENCHMARK_URL_BASE` |
| **full_pp-Varianten** | `--fullpp-upload-profile` | `simple_striped`, `tiled_deflate` | `simple_striped` |
| | `--fullpp-save-format` | `GTiff`, `netCDF` | `GTiff` |
| **Infrastruktur** | `--api-url` | URL | CDSE openEO 1.2 |
| | `--output-dir` | Pfad | `outputs` |

### 2.2 Regionen — `REGIONS`, `run_benchmark.py:144–181`

Alle Basis-Extents sind einheitlich 0.15° × 0.10°. Für jede Region existiert ein
Template `scenarios/bench_onthefly_{region}.json`; alle neun sind strukturell
identisch (gleiche Collections, gleiche Zeiträume, gleicher Wolkenfilter).

| Region | Bounding Box (W, S, O, N) | Ziel-EPSG | UTM-Zone |
|---|---|---|---|
| `amsterdam` | 4.80, 52.33, 4.95, 52.43 | 32631 | 31 N |
| `berlin` | 13.30, 52.45, 13.45, 52.55 | 32633 | 33 N |
| `hamburg` | 9.85, 53.50, 10.00, 53.60 | 32632 | 32 N |
| `kapstadt` | 18.35, −34.00, 18.50, −33.90 | 32734 | 34 S |
| `newyork` | −74.05, 40.70, −73.90, 40.80 | 32618 | 18 N |
| `rom` | 12.40, 41.85, 12.55, 41.95 | 32633 | 33 N |
| `tokio` | 139.65, 35.60, 139.80, 35.70 | 32654 | 54 N |
| `wien` | 16.30, 48.15, 16.45, 48.25 | 32633 | 33 N |
| `zuerich` | 8.45, 47.33, 8.60, 47.43 | 32632 | 32 N |

Die Region bestimmt das Default-Ziel-CRS und ist Filterkriterium beim Auffinden
von Vergleichsläufen (`_detect_folder_region`, `_find_latest_run_dir`).
Zonengrenzen-Erkennung: `_utm_zone_for_lon` (Z. 1117),
`_extent_spans_multiple_utm_zones` (Z. 1122), `_is_utm_epsg` (Z. 123).

### 2.3 Ausdehnung — `SIZE_KM`, `run_benchmark.py:56`; Berechnung `_compute_extent` (Z. 1092)

| Wert | Kantenlänge | Berechnung |
|---|---|---|
| `small` | 5 km | Quadrat um den Mittelpunkt, `half_km/111.0` in Grad |
| `medium` | nominell 10 km | **Sonderfall:** liefert den unveränderten `REGIONS`-Extent zurück (Z. 1100–1101); `SIZE_KM["medium"]` wird dafür nicht benutzt |
| `large` | 50 km | wie `small` |
| `xlarge` | 100 km | wie `small`; löst den Auto-Skip für `full_preprocessing` aus |
| `xxlarge` | 200 km | wie `small`; überschreitet laut Kommentar die CDSE-Tile-Grenze von 120 km |

Hinweis: Der feste `medium`-Extent ist 0.10° hoch (≈ 11,1 km) und in
Ost-West-Richtung breitenabhängig — je nach Region zwischen rund 9,9 km
(Hamburg) und 13,8 km (Kapstadt). Die Konsolenausgabe in `main()` (Z. 4737)
druckt trotzdem pauschal „10 km".

### 2.4 Workflows — `WORKFLOWS`, `run_benchmark.py:59–60`

Prozessgraph-Aufbau in `_build_workflow_pg` (Z. 1157–1392), lokale Entsprechung
in `_apply_local_workflow` (Z. 3318–3457).

Alle Workflows teilen eine gemeinsame Basis (Z. 1194–1230):
`rename_labels(DEM→B04)` als Knoten `renamelabels1`, damit `merge_cubes`
pixelweise addiert statt Bänder zu konkatenieren, und
`reduce_dimension(t, first)` als `reducedimension_dem`, um die Zeitdimension des
DEM zu entfernen.

| Workflow | Was gerechnet wird | Zeilen |
|---|---|---|
| `merge_add` | `B04 + DEM` (`overlap_resolver = add`) | 1232–1233 |
| `subtract` | `B04 − DEM` | 1235–1246 |
| `mask` | S2 zusätzlich mit Band SCL; maskiert wird alles mit SCL ∉ {4, 5}; dann `+ DEM` | 1248–1290 |
| `aggregation` | `reduce_dimension(t, mean)` über `(B04 + DEM)` | 1292–1311 |
| `focal` | `apply_kernel` mit 3×3-Mittelwertkern über `(B04 + DEM)` | 1313–1325 |
| `resample` | DEM-Roundtrip `resample_spatial(3035 @ 30 m)` → `resample_spatial(Regions-UTM @ 10 m)`, dann `+ B04` | 1327–1353 |
| `filter_bbox` | `(B04 + DEM)`, zugeschnitten auf die mittleren 50 % je Kante (= 25 % der Fläche) | 1355–1390 |

**Wichtig für den Referenzvergleich:** `_apply_local_workflow` behandelt
`resample` **identisch zu `merge_add`** (Z. 3376–3379) — der Roundtrip wird lokal
nicht nachgebildet. Ein `--reference-check` mit `--workflow resample` misst
deshalb eine konstruktionsbedingte Differenz, keine reine Rundungsabweichung.

### 2.5 DEM-Format und DEM-Layout

`DEM_FORMATS` (Z. 95), Writer `_write_dem_with_layout` (Z. 215),
`_write_dem_as_zarr` (Z. 515), `_write_dem_as_netcdf` (Z. 561):

| Format | Endung / Media-Type | Besonderheit |
|---|---|---|
| `gtiff` | `.tif`, `image/tiff; application=geotiff` | einziges Format, das `--dem-layout` und `--dem-tiles` unterstützt |
| `zarr` | `.zarr` (Verzeichnis), `application/vnd+zarr` | Upload per `scp -r`; `load_stac` zeigt auf eine **Collection** statt aufs Item |
| `netcdf` | `.nc`, `application/x-netcdf` | `load_stac` zeigt weiter aufs Item |

`DEM_LAYOUTS` (Z. 84), Blockgröße `_COG_BLOCK_SIZE = 128` (Z. 85):

| Layout | Schreibprofil |
|---|---|
| `striped` | `tiled=False`, keine Kompression, `interleave=band` |
| `tiled_uncompressed` | `tiled=True`, 128×128, keine Kompression |
| `cog` | `tiled=True`, 128×128, `compress=deflate`, plus Overviews (`build_overviews`, average) |

Die Pixelwerte sind über alle Layouts und Formate identisch — alle Writer
schreiben denselben In-Memory-Puffer aus `_reproject_dem_to_array`. Das ist
durch `test_dem_layout.py` abgesichert.

### 2.6 Weitere Dimensionen

| Dimension | Werte / Wirkung | Fundstelle |
|---|---|---|
| **Kachelung** | `--dem-tiles N`: zerlegt das reprojizierte DEM in N räumliche Kacheln, je Kachel ein STAC-Item in einer Collection. Raster möglichst quadratisch (`rows*cols == N` exakt; Primzahl → 1×N). Vor dem Upload prüft `_verify_tile_union_identity` bitgenau, dass die Kachel-Union dem Einzel-DEM entspricht. | `_tile_grid_layout` (757), `_split_dem_into_tiles` (768), `_verify_tile_union_identity` (814), `build_dem_tiles_collection` (2031) |
| **Gitterausrichtung** | `--snap-dem-to-s2`: leitet das erwartete S2-10-m-Zielgitter aus dem Extent ab und croppt den reprojizierten Puffer darauf (reines Slicing, kein zweiter Warp). Verifikation vor dem Upload. | `_s2_grid_from_extent` (673), `_crop_to_grid` (705), `_verify_snap_grid` (866), `_verify_snap_crop_identity` (891) |
| **Resampling-Richtung** | `--resample-s2-to-dem`: zieht serverseitig S2 auf das DEM-Gitter (`resample_cube_spatial`) statt umgekehrt. | `build_local_pp_scenario` (1546–1569) |
| **Ziel-CRS** | Ohne `--target-crs` gilt das Regions-UTM. Bei UTM-Ziel erzwingt `_reproject_dem_to_array` 10 m Auflösung und snappt den Origin auf das 10-m-Raster; bei Nicht-UTM (z. B. 3035) native Auflösung ohne Snap. | `_normalize_crs` (110), `_reproject_dem_to_array` (604–636) |
| **Resampling-Methode** | `nearest` / `bilinear` / `cubic`. Laut Kommentar (Z. 62–63) ist `nearest` pixelidentisch zu CDSEs internem Verfahren. Der Wert steuert zusätzlich das Resampling im Accuracy-Vergleich. | `LOCAL_RESAMPLING` (64–68), `_reproject_dem_to_array` (599) |
| **Wiederholungen** | `--repeat N` je Strategie; `--run-type auto` markiert den ersten Lauf als `cold`, alle weiteren als `hot`. Reines DB-Label — es wird kein Backend-Cache geleert. | `_run_type_for` (1082), Schleife (4757) |
| **Plattenschutz** | Vor jedem Einzellauf prüft `check_disk_space` den freien Platz. Bei Unterschreitung: Status `aborted_disk_full` und `break` aus der Repeat-Schleife dieser Strategie. | `check_disk_space` (4024), `_free_gb` (4012) |
| **Cleanup** | Löscht nach den Accuracy-Checks ausschließlich `*.tif`/`*.tiff`. CDSE-Läufe erst nach vorhandenem Accuracy-Eintrag; ein `local_reference`-Lauf erst, wenn alle abhängigen Läufe ihren Eintrag haben. | `cleanup_after_accuracy` (4164), `_list_run_tifs` (4092) |

---

## 3. Die vier Strategien im Detail

Dispatch in `main()` (Z. 4749–4754). Gemeinsame Grundlage aller Prozessgraphen
ist `_load_bench_template` (Z. 1144) → `_build_workflow_pg` (Z. 1157).

### 3.1 `onthefly` — alles im Backend

**Funktion:** `run_strategy_onthefly` (Z. 2506–2539).

1. Ausgabeordner `run_<TS>_onthefly` anlegen (`_make_outdir`).
2. Szenario aus dem Regions-Template bauen und als `scenario_onthefly.json`
   ablegen (`build_onthefly_scenario`, Z. 1435).
3. Job per Subprozess `openeotest.py run` ausführen (`run_openeo`, Z. 1018).
4. Lauf in die DuckDB importieren (`database.import_run`).

**Transformation:** vollständig im Backend. Es gibt in dieser Strategie keinen
einzigen rasterio-Aufruf und keinen Upload. CDSE löst den CRS-Konflikt implizit
beim `merge_cubes`.

**Prozessgraph:**

```
load_collection(SENTINEL2_L2A, B04)  ─ loadcollection1 ─┐
load_collection(COPERNICUS_30)       ─ loadcollection2 ─→ rename_labels(DEM→B04)
                                                       → reduce_dimension(t, first)
                          merge_cubes(cube1, cube2, overlap_resolver=add) ─ merge1
                          [+ workflow-spezifische Knoten]
                          → save_result(GTiff)
```

Sonderfall: Überspannt der Extent eine UTM-Zonengrenze — oder ist
`--force-target-crs` gesetzt — wird zusätzlich ein `resample_spatial` als Knoten
`resampletargetcrs1` hinter `loadcollection1` eingefügt
(`_force_onthefly_target_crs`, Z. 1395). Das Ziel-CRS stammt dabei **immer** aus
`REGIONS`, nie aus `--target-crs`. Der Knotenname ist bewusst nicht
`resamplespatial1/2`, weil das die Signatur von `--workflow resample` in
`_detect_pg_workflow` wäre.

**Zwischenprodukte:** nur lokale JSONs und die CDSE-Ergebnis-TIFs im Run-Ordner.
Keine Uploads.

**Zeit:** `preprocessing_time = None` (Z. 2530); `total_time` ist reine
Backend-Zeit.

### 3.2 `local_preprocessing` — DEM lokal, S2 aus dem Katalog

**Funktion:** `run_strategy_local_pp` (Z. 2598–3006), im Code in fünf Schritten
gegliedert.

1. **Setup** (Z. 2599–2692): Ziel-CRS bestimmen, `--dem-format`/`--dem-tiles`
   validieren, Paketabhängigkeiten prüfen (`_check_dem_format_deps`), bei
   `--snap-dem-to-s2` das Zielgitter ableiten, Remote-Namen und URLs bilden.
2. **Schritt 1/5 — DEM beschaffen** (`_get_or_download_dem`, Z. 2542): eigener
   CDSE-Download-Job (`build_dem_download_scenario`, Z. 1464). Mit `--dem-cache`
   wird die Datei unter `outputs/dem_cache/dem_{region}_{extent_size}.tif`
   wiederverwendet. **Die Download-Zeit zählt nicht in `preprocessing_time`.**
3. **Schritt 2/5 — lokale Reprojektion** (`_reproject_dem_to_array`, Z. 585) in
   einen In-Memory-Puffer, optional Crop aufs S2-Gitter (`_crop_to_grid`),
   optional Kachelung (`_split_dem_into_tiles`), dann Schreiben im gewählten
   Format und Layout.
4. **Pflicht-Verifikationen** vor dem Upload — bewusst außerhalb der gemessenen
   Zeit. Bei Verletzung `RuntimeError`, bevor irgendetwas hochgeladen wird.
5. **Schritt 3/5 — Upload** des Assets per `scp_upload` bzw. `scp_upload_dir`
   (Zarr-Verzeichnis) auf den Webserver.
6. **Schritt 4/5 — STAC** erzeugen und hochladen (`build_stac_item`, Z. 1858;
   bei Kacheln oder Zarr zusätzlich eine Collection).
7. **Schritt 5/5 — CDSE-Job** mit `load_stac` (`build_local_pp_scenario`, Z. 1491).
8. **Persistenz** (`import_run`) und Abholen der nginx-Zugriffe
   (`import_nginx_access_log`).

**Transformation:** DEM lokal (rasterio), S2 weiterhin serverseitig aus dem
CDSE-Katalog. Die finale Ausrichtung DEM ↔ S2 und der Merge passieren im Backend.
Mit `--resample-s2-to-dem` wird die Richtung umgedreht.

**Prozessgraph:**

```
load_collection(SENTINEL2_L2A, B04)                    ─ loadcollection1
[optional resample_cube_spatial(S2 → DEM-Grid)]        ─ resamplecubespatial1
load_stac(http://<host>/stac_item_… | …collection_….json) ─ loadstac1
   → rename_labels(source=[], target=["B04"])
   → reduce_dimension(t, first)
merge_cubes(cube1=S2, cube2=DEM, overlap_resolver=add) ─ merge1
   → [workflow-spezifisch] → save_result(GTiff)
```

`loadstac1` zeigt auf die **Collection**-URL, wenn `--dem-format zarr` oder
`--dem-tiles > 1`, sonst auf die **Item**-URL.

**Hochgeladen werden:** das reprojizierte DEM (bzw. seine Kacheln), das STAC-Item
je Kachel und ggf. die Collection. Der DEM-Rohdownload und die CDSE-Ergebnisse
bleiben lokal.

**Zeit:** `preprocessing_time = t_reproject + t_scp_asset + t_stac` (Z. 2915).

**DB-Label:** mit `--dem-cache` wird `local_pp_cached` statt
`local_preprocessing` als `crs_strategy` geschrieben (Z. 2681). Es ist derselbe
Codepfad, kein eigener Runner.

### 3.3 `full_preprocessing` — beide Raster extern

**Funktion:** `run_strategy_full_pp` (Z. 3009–3296), im Code sieben Schritte.

1. **S2 von CDSE laden** (`build_s2_download_scenario`, Z. 1591; bei
   `--workflow mask` zusätzlich Band SCL).
2. **DEM von CDSE laden** (`build_dem_download_scenario`).
3. **DEM lokal reprojizieren:** ohne `--target-crs` wird das Gitter aus dem
   ersten S2-TIF gelesen (`read_s2_grid`, Z. 1626) und das DEM **exakt** darauf
   gebracht (`reproject_dem_to_grid`, Z. 1643) — CDSE muss dann gar nicht mehr
   resampeln. Mit `--target-crs` stattdessen `reproject_dem_local` (Z. 979).
4. **S2 lokal reprojizieren** — nur mit `--target-crs` **und** `--reproject-s2`
   (`reproject_s2_local`, Z. 1676).
5. **Clean-Rewrite** aller TIFs nach `--fullpp-upload-profile`
   (`_rewrite_tif_clean`, Z. 2274), danach vollständige lokale Dekodierprüfung
   (`_verify_tif_readable`, Z. 2338).
6. **Uploads** mit Größenabgleich (`scp_upload_verified`, Z. 2481).
7. **STAC:** je S2-Datum ein Item, gebündelt in einer Collection
   (`build_s2_stac_item` / `build_s2_stac_collection`); für das DEM ein
   Einzel-Item.
8. **CDSE-Job** mit **zwei** `load_stac` (`build_full_pp_scenario`, Z. 1778),
   danach Persistenz und nginx-Logs.

**Transformation:** DEM und optional S2 lokal; im Backend bleibt nur das Laden
der beiden externen Quellen plus die Workflow-Operation. CDSE greift nicht mehr
auf seinen eigenen S2-Katalog zu.

**Prozessgraph:**

```
load_stac(http://<host>/s2_collection_….json)    ─ loadstac1  (S2)
load_stac(http://<host>/full_pp_dem_stac_….json) ─ loadstac2  (DEM)
   → rename_labels(source=[], target=["B04"]) → reduce_dimension(t, first)
merge_cubes(cube1=loadstac1, cube2=reducedimension_dem) ─ merge1
   → [workflow-spezifisch] → save_result(GTiff | netCDF)
```

**Hochgeladen werden:** alle S2-TIFs, das DEM-TIF, alle S2-Items, die
S2-Collection und das DEM-Item. Das ist der Grund für die hohe Zahl an
Range-Requests bei großen Extents (siehe Auto-Skip, Abschnitt 6).

**Zeit:** `preprocessing_time` = Reprojektion + Clean-Rewrite + Uploads +
STAC-Bau/-Upload (Z. 3235–3236); die beiden Downloads werden separat als
`s2_download_time` und `dem_download_time` geführt.

### 3.4 `local_reference` — vollständig lokale Ground-Truth

**Funktion:** `run_strategy_local_reference` (Z. 3460–3676).

1. **Marker-Szenario** `local_reference_{region}.json` schreiben: enthält den
   äquivalenten onthefly-Prozessgraphen plus einen `_local_reference`-Block mit
   `target_crs`, `resampling`, `target_resolution_m` und `workflow`. Dieser Graph
   wird **nie ausgeführt** — er dient nur dazu, dass die Auto-Erkennung
   (`_detect_folder_region`, `_detect_folder_workflow`) den Ordner später
   zuordnen kann.
2. **S2 von CDSE laden**, **DEM von CDSE laden** (je ein reiner Download-Job).
3. **Lokale Reprojektion in fester Reihenfolge:** zuerst jedes S2-TIF nach
   `target_crs` @ 10 m — das definiert das Gitter —, dann das DEM exakt auf
   dieses Gitter (`reproject_dem_to_grid`).
4. **Workflow lokal rechnen** mit rasterio und numpy (`_apply_local_workflow`,
   Z. 3318), Ausgabe unter denselben Dateinamen wie die S2-Eingaben, damit der
   Accuracy-Vergleich per Dateiname matcht.
5. Eine minimale `results.json` selbst schreiben (`backend_name =
   "local_rasterio"`), dann `import_run`.

**Transformation:** ausschließlich lokal. Vom Backend kommen nur die beiden
Rohdaten-Downloads. **Es gibt keinen CDSE-Workflow-Job**, keine Uploads und keine
nginx-Erfassung.

**Zeit:** Als einzige Strategie rechnet sie die Downloads in
`preprocessing_time` ein (Z. 3611–3612); `total_time = preprocessing_time`.

**Lokale Workflow-Semantik** (`_apply_local_workflow`, Z. 3376–3434):
`merge_add` und `resample` → `S2 + DEM`; `subtract` → `S2 − DEM`; `mask` →
`np.isin(SCL, (4,5))`, sonst NaN, dann `+ DEM`; `focal` → `_box3_mean` mit
Edge-Padding; `filter_bbox` → Slicing auf die mittleren 50 %; `aggregation` →
`np.nanmean` über alle Datumsstände, unter jedem Datums-Dateinamen abgelegt.

### 3.5 Vergleich

| | `onthefly` | `local_preprocessing` | `full_preprocessing` | `local_reference` |
|---|---|---|---|---|
| Runner (Zeile) | 2506 | 2598 | 3009 | 3460 |
| Ordner-Suffix | `_onthefly` | `_local_pp` | `_full_pp` | `_local_reference` |
| Ergebnis-TIFs in | Run-Root | `step3_main/` | `step5_main/` | `step4_result/` |
| S2-Quelle | CDSE-Katalog | CDSE-Katalog | eigener HTTP-Server | CDSE-Download, dann lokal |
| DEM-Quelle | CDSE-Katalog | eigener HTTP-Server | eigener HTTP-Server | CDSE-Download, dann lokal |
| Lokale Reprojektion | keine | DEM | DEM (+ optional S2) | S2 **und** DEM |
| CDSE-Workflow-Job | ja | ja | ja | **nein** |
| Uploads | keine | DEM + STAC | S2 + DEM + STAC | keine |
| Downloads in `preprocessing_time` | – | nein | nein | **ja** |
| nginx-Logs | nein | ja | ja | nein |

### 3.6 Genauigkeitsvergleich

Zwei Modi, beide über `run_accuracy_check` (Z. 4291):

| Flag | Referenz | Testkandidaten |
|---|---|---|
| `--accuracy-check` | neuester `onthefly`-Lauf | **eine** Strategie aus der Session (`local_preprocessing` oder `full_preprocessing`) |
| `--reference-check` | neuester `local_reference`-Lauf | alle drei CDSE-Strategien, sofern ein passender Ordner existiert |

**Auswahl des Referenzlaufs** (`_find_latest_run_dir`, Z. 3869): Ordner-Glob
`outputs/run_*_{suffix}`, dann kumulativ gefiltert nach Region, **exaktem**
Extent (Toleranz 1e-4° ≈ 10 m auf allen vier Kanten) und Workflow; aus den
verbliebenen Kandidaten der mit der neuesten mtime. Passt eines der Kriterien
nicht, gibt es keinen Vergleich — keinen Näherungstreffer.

**Alignment** (`accuracy_calculator.align_rasters`, Z. 62): Sind beide Raster
pixelidentisch (gleiches CRS, gleiche Größe, identischer Transform), werden sie
1:1 gelesen — bewusst **ohne** Resampling, damit kein Warp echte Pixeldifferenzen
glättet. Sonst wird das **Test**-Raster auf das **Referenz**-Gitter reprojiziert,
mit der Methode aus `--local-resampling`.

**Metriken** (`accuracy_calculator.calculate_metrics`, Z. 131), je Band mit
`diff = test − reference`:

| Metrik | Formel |
|---|---|
| RMSE | `sqrt(mean(diff²))` |
| MAE | `mean(abs(diff))` |
| ME_bias | `mean(diff)` |
| RMSE/MAE relativ | `metrik / reference_mean * 100` |
| correlation | `corrcoef(ref, test)[0,1]` |
| coverage_percent | `valid_pixels / total_pixels * 100` |
| min_diff / max_diff | `min(diff)` / `max(diff)` |

Aggregation in zwei Stufen: arithmetisches Mittel über die Bänder je TIF-Paar
(`_compare_tif_pair`, Z. 3905), dann **Median** über alle Datumsstände
(`run_accuracy_check`, Z. 4414–4415).

**Persistiert wird nur ein Teil davon.** `_persist_accuracy` (Z. 3976) schreibt
ausschließlich `run_id`, `reference_file`, `reference_run_id`, `rmse` und `mae`.
Coverage, Korrelation, ME_bias und max_diff werden berechnet und geloggt, landen
aber **nicht** in der Datenbank.

**NoData:** Im Benchmark-Pfad wird `calculate_metrics` ohne `nodata`-Argument
gerufen (Z. 3924) — es greift die Regel „nur NaN und Inf sind ungültig". Ein
NoData-Sentinel aus den GeoTIFF-Metadaten wird nicht ausgewertet.
Die Optionen `--nodata` und `--exclude-zeros` existieren nur im Standalone-CLI
von `accuracy_calculator.py`.

---

## 4. Matrix der lauffähigen Kombinationen

Alle Aufrufe aus dem Repo-Root. „Implementiert" bezieht sich ausschließlich auf
den Code — **nicht** darauf, ob CDSE die Kombination akzeptiert. Die letzte
Spalte lässt sich aus dem Code nicht ableiten und bleibt daher `TBD`.

| # | Experiment | CLI-Aufruf | Implementiert | Einschränkungen | Status auf CDSE |
|---|---|---|---|---|---|
| 1 | Strategievergleich (Basis) | `python run_benchmark.py --strategy all --region berlin --extent-size medium --workflow merge_add --repeat 5 --run-type auto` | ja | `all` enthält `full_preprocessing`, aber nicht `local_reference` | TBD |
| 2 | Einzelstrategie on-the-fly | `python run_benchmark.py --strategy onthefly --region berlin --repeat 5` | ja | ignoriert alle DEM- und `--target-crs`-Flags | TBD |
| 3 | Regionsvergleich | `python run_benchmark.py --strategy all --region tokio --extent-size medium --repeat 3` | ja | 9 Regionen; je Region ein eigenes Template nötig | TBD |
| 4 | Südhalbkugel / UTM-Süd | `python run_benchmark.py --strategy all --region kapstadt --repeat 3` | ja | EPSG:32734; keine Sonderbehandlung im Code | TBD |
| 5 | Extent-Skalierung | `python run_benchmark.py --strategy all --region berlin --extent-size large --repeat 3` | ja | `medium` liefert den festen Regions-Extent, nicht exakt 10 km | TBD |
| 6 | Extent xlarge mit full_pp | `python run_benchmark.py --strategy full_preprocessing --region berlin --extent-size xlarge --repeat 1` | ja | bei `--strategy all` würde full_pp hier automatisch übersprungen | TBD |
| 7 | Extent xxlarge | `python run_benchmark.py --strategy onthefly --region berlin --extent-size xxlarge --repeat 1` | ja | 200 km überschreitet laut Kommentar die CDSE-Tile-Grenze von 120 km | TBD |
| 8 | Workflow-Variation | `python run_benchmark.py --strategy all --workflow focal --region berlin --repeat 3` | ja | 7 Workflows | TBD |
| 9 | Workflow `mask` | `python run_benchmark.py --strategy all --workflow mask --region berlin --repeat 3` | ja | lädt zusätzlich Band SCL; lokal braucht das S2-TIF ≥ 2 Bänder | TBD |
| 10 | Workflow `resample` | `python run_benchmark.py --strategy all --workflow resample --region berlin --repeat 3` | ja | lokal **nicht** nachgebildet → für `--reference-check` ungeeignet | TBD |
| 11 | DEM-Layout-Experiment | `python run_benchmark.py --strategy local_preprocessing --dem-layout cog --region berlin --repeat 3` | ja | nur `local_preprocessing`, nur `--dem-format gtiff` | TBD |
| 12 | DEM-Format Zarr | `python run_benchmark.py --strategy local_preprocessing --dem-format zarr --region berlin --repeat 1` | ja | braucht `xarray` + `zarr`; `load_stac` zeigt auf eine Collection | TBD |
| 13 | DEM-Format NetCDF | `python run_benchmark.py --strategy local_preprocessing --dem-format netcdf --region berlin --repeat 1` | ja | braucht `xarray` + `netCDF4` | TBD |
| 14 | DEM-Kachelung | `python run_benchmark.py --strategy local_preprocessing --dem-tiles 4 --region berlin --repeat 1` | ja | **nur** mit `--dem-format gtiff` (harter Abbruch sonst); Kachelraster muss ins DEM passen | TBD |
| 15 | Gitter-Snapping auf S2 | `python run_benchmark.py --strategy local_preprocessing --snap-dem-to-s2 --region berlin --repeat 3` | ja | nur bei UTM-Ziel-CRS; bricht ab, wenn das DEM den Extent nicht voll abdeckt | TBD |
| 16 | Nicht-UTM-Ziel-CRS | `python run_benchmark.py --strategy local_preprocessing --target-crs EPSG:3035 --region berlin --repeat 3` | ja | kein 10-m-Snap; `--snap-dem-to-s2` wird dann ignoriert | TBD |
| 17 | Resampling-Richtung umkehren | `python run_benchmark.py --strategy local_preprocessing --resample-s2-to-dem --region berlin --repeat 3` | ja | nur `local_preprocessing`; mit `--workflow resample` semantisch inkonsistent | TBD |
| 18 | Resampling-Methode | `python run_benchmark.py --strategy local_preprocessing --local-resampling bilinear --region berlin --repeat 3` | ja | wirkt auch auf `full_pp`, `local_reference` und den Accuracy-Vergleich | TBD |
| 19 | UTM-Zonengrenze erzwingen | `python run_benchmark.py --strategy onthefly --force-target-crs --region berlin --repeat 3` | ja | nur `onthefly`; benutzt immer das Regions-EPSG, nie `--target-crs` | TBD |
| 20 | full_pp Upload-Profil | `python run_benchmark.py --strategy full_preprocessing --fullpp-upload-profile tiled_deflate --region berlin --repeat 1` | ja | nur `full_preprocessing` | TBD |
| 21 | full_pp Ausgabeformat | `python run_benchmark.py --strategy full_preprocessing --fullpp-save-format netCDF --region berlin --repeat 1` | ja | nur `full_preprocessing`; `options` wird dabei geleert | TBD |
| 22 | full_pp mit S2-Reprojektion | `python run_benchmark.py --strategy full_preprocessing --target-crs EPSG:3035 --reproject-s2 --region berlin --repeat 1` | ja | `--reproject-s2` ohne `--target-crs` ist wirkungslos | TBD |
| 23 | DEM-Cache-Amortisation | `python run_benchmark.py --strategy local_preprocessing --dem-cache --region berlin --repeat 5` | ja | Cache pro (Region, Extent); DB-Label wird zu `local_pp_cached` | TBD |
| 24 | Cold/Hot-Vergleich | `python run_benchmark.py --strategy all --run-type auto --repeat 5` | ja | reines Label, kein Cache wird geleert | TBD |
| 25 | Genauigkeit gegen onthefly | `python run_benchmark.py --strategy all --region berlin --workflow merge_add --repeat 1 --accuracy-check` | ja | vergleicht nur **eine** Teststrategie (die letzte in `all_results`) | TBD |
| 26 | Ground-Truth erzeugen | `python run_benchmark.py --strategy local_reference --region berlin --extent-size medium --workflow merge_add --repeat 1` | ja | kein CDSE-Workflow-Job; nicht in `--strategy all` enthalten | TBD |
| 27 | Genauigkeit gegen Ground-Truth | `python run_benchmark.py --strategy all --region berlin --workflow merge_add --repeat 1 --reference-check` | ja | braucht einen `local_reference`-Lauf mit identischer Region/Extent/Workflow | TBD |
| 28 | Accuracy standalone | `python run_benchmark.py --repeat 0 --region berlin --extent-size medium --workflow merge_add --accuracy-check` | ja | wertet vorhandene Ordner aus, ohne neue Läufe | TBD |
| 29 | Cleanup-Probelauf | `python run_benchmark.py --repeat 0 --dry-run-cleanup` | ja | löscht nichts; listet nur auf | TBD |
| 30 | Lauf mit Aufräumen | `python run_benchmark.py --strategy all --repeat 3 --accuracy-check --cleanup-after-accuracy` | ja | löscht nur `*.tif`; Metadaten bleiben | TBD |
| 31 | Eigener Webserver | `python run_benchmark.py --strategy local_preprocessing --host user@example.org --web-path /var/www/data/ --url-base http://example.org/data/` | ja | nur für `local_pp` und `full_pp` relevant | TBD |
| 32 | Anderes Backend | `python run_benchmark.py --api-url https://openeo.example.org/openeo/1.2 --strategy onthefly` | ja | Regionen-Templates verweisen auf CDSE-Collections | TBD |

Zusätzliche Auswertungsschritte (keine Benchmark-Läufe):

| Zweck | Aufruf |
|---|---|
| Datenbank anlegen | `python database.py create` |
| Läufe anzeigen | `python database.py show` |
| Statistische Auswertung | `python analyze.py` |
| Netzwerklatenz zu CDSE | `python latency_check.py` |
| Uhren-Offset gegenprüfen | `python ntp_check.py` |
| Genauigkeit zweier Dateien | `python accuracy_calculator.py <referenz.tif> <test.tif>` |

---

## 5. Abgrenzung zum Upstream-Fork

Verglichen wurden die Arbeitsverzeichnisse `openeobench` und
`openeobench-original` per Dateisystem-Vergleich (nicht per Git-Historie).

**Ergebnis des Vergleichs:** 142 Dateien existieren in beiden Repos. Davon sind
**genau drei** inhaltlich verändert: `openeotest.py`, `requirements.txt` und
`.gitignore`. Alles andere ist byte-identisch — einschließlich der
`openeobench`-CLI, `openeo_checker.py`, `process_checker.py`,
`process_summary.py`, `pyproject.toml` und `README.md`. Eine Datei existiert nur
im Upstream (`scenarios/bratislava_10km_2018_cdse.json`).

Die eigene Arbeit liegt also fast vollständig in **zusätzlichen Dateien neben**
dem Upstream, nicht in dessen Umbau.

### 5.1 Vom Upstream übernommen

Der Fork ist ein generisches openEO-Prüfwerkzeug für **Backend-Vergleiche**,
keine CRS- oder Genauigkeitsuntersuchung:

| Bestandteil | Inhalt |
|---|---|
| `openeobench` (CLI) | 8 Subcommands: `service`, `service-summary`, `run`, `run-summary`, `result-summary`, `process`, `process-summary`, `visualize` |
| `openeo_checker.py` | Endpoint-Verfügbarkeit und Antwortzeiten |
| `process_checker.py`, `process_summary.py` | Prozess-Compliance gegen die openEO-Level L1–L4 |
| `openeotest.py` (Basis) | Szenario-Ausführung, Zusammenfassung, GeoTIFF-Visualisierung und -Vergleich |
| `scenarios/`, `advanced_scenarios/` | 56 Backend-Vergleichsszenarien (Vienna/Bratislava × 3 Jahre × 6 Backends, plus NDVI-/Reducer-Varianten) |
| `docs/` | Sphinx-Dokumentation |

Upstream-Abhängigkeiten: `requests`, `openeo`, `pyjson5`.

### 5.2 Im Rahmen der Arbeit ergänzt

| Thema | Datei / zentrale Funktionen |
|---|---|
| **Benchmark-Runner für CRS-Strategien** | `run_benchmark.py` (4857 Z., neu): `run_strategy_onthefly`, `run_strategy_local_pp`, `run_strategy_full_pp`, `run_strategy_local_reference`; eigener Einstiegspunkt, **nicht** in die `openeobench`-CLI integriert |
| **Szenario-Generierung zur Laufzeit** | `_build_workflow_pg`, `build_onthefly_scenario`, `build_local_pp_scenario`, `build_full_pp_scenario`, `build_dem_download_scenario`, `build_s2_download_scenario`, `_compute_extent` — statt statischer JSONs wie im Upstream |
| **DEM-Reprojektion** | `_reproject_dem_to_array`, `reproject_dem_local`, `reproject_dem_to_grid`, `reproject_s2_local` |
| **Layout-Experiment** | `_write_dem_with_layout`, `_compute_overview_factors`, `_inspect_tif_layout` |
| **Format-Experiment** | `_write_dem_as_zarr`, `_write_dem_as_netcdf`, `_build_xarray_dataset`, `_apply_geozarr_metadata`, `_check_dem_format_deps` |
| **Gitter-Snapping** | `_s2_grid_from_extent`, `_crop_to_grid`, `read_s2_grid`, `_verify_snap_grid`, `_verify_snap_crop_identity` |
| **Kachelung** | `_tile_grid_layout`, `_split_dem_into_tiles`, `_verify_tile_union_identity` (bitgenaue Prüfung vor dem Upload) |
| **STAC-Selbstbereitstellung** | `build_stac_item`, `build_s2_stac_item`, `build_s2_stac_collection`, `build_dem_stac_collection`, `build_dem_tiles_collection`; Upload per `scp_upload`, `scp_upload_dir`, `scp_upload_verified` |
| **Genauigkeitsvergleich** | `accuracy_calculator.py` (neu): `align_rasters`, `calculate_metrics`; Orchestrierung in `run_benchmark.run_accuracy_check`, `_compare_tif_pair`, `_find_latest_run_dir`, `_persist_accuracy` |
| **DuckDB-Persistenz** | `database.py` (neu): 4 Tabellen, `import_run` mit 14 Versuchsparametern, `import_nginx_access_log`, idempotente Schema-Migration |
| **Statistische Auswertung** | `analyze.py` (neu): `bootstrap_median_ci` (2000 Iterationen), `print_significance_tests` (Mann-Whitney-U), `print_nginx_stats` |
| **Zeit- und Latenzmessung** | `latency_check.py`, `ntp_check.py` (beide neu): HTTP-`Date`-basierter Uhren-Offset zu CDSE, gegengeprüft per echtem NTP |
| **Robuster Download** (in `openeotest.py`) | Eigener Streaming-Download mit Content-Length-Prüfung, `Range:`-Resume, Schreiben nach `.part` und atomarem Rename erst nach rasterio-Verifikation; plattformunabhängiger harter Timeout; Byte-Level-TIFF-Header-Diagnose |
| **Messgenauigkeit** (in `openeotest.py`) | Festes 5-s-Polling statt exponentiellem Backoff |
| **Kostenerfassung** (in `openeotest.py`) | `job.describe()` liefert `credits`, `cpu_seconds`, `duration_backend`, `input_pixels_mp`, `max_memory_gb` — genau die neuen Spalten in `runs` |
| **Platten- und Aufräum-Management** | `check_disk_space`, `cleanup_after_accuracy`, `delete_run_tifs`, `_run_has_accuracy` |
| **Tests** | 5 neue Skripte: `test_streaming_download.py`, `test_dem_format.py`, `test_dem_layout.py`, `test_fullpp_upload_profile.py`, `test_cleanup.py` (Upstream hatte nur `test_commands.py`) |
| **Neue Szenarien** | CRS-Experimentreihe, `load_stac`-Tests, Genauigkeitsszenarien und 9 `bench_onthefly_*`-Templates |
| **Abhängigkeiten** | `requirements.txt` von 3 auf 43 Zeilen: `rasterio`, `duckdb`, `scipy`, `rioxarray`, `xarray`, `matplotlib`, `Pillow`; `numpy==1.26.4` und `scipy==1.11.4` gepinnt, weil die CPU des Zielservers kein x86-64-v2 unterstützt |

**In einem Satz:** Aus einem generischen openEO-Backend-Prüfwerkzeug wurde ein
CDSE-fokussierter CRS-Strategie-Messstand — vier Strategien über 9 Regionen ×
5 Ausdehnungen × 7 Workflows, mit lokaler DEM-Verarbeitung inklusive Layout-,
Format-, Kachel- und Snapping-Varianten, STAC-Selbstbereitstellung,
RMSE/MAE-Vergleich gegen eine lokale Ground-Truth, DuckDB-Persistenz und
median-basierter Auswertung mit Konfidenzintervallen.

### 5.3 Nicht angepasst

- **`README.md` ist byte-identisch zum Upstream.** Keines der neuen Werkzeuge
  (`run_benchmark.py`, `analyze.py`, `database.py`, `accuracy_calculator.py`,
  `latency_check.py`) ist dort dokumentiert.
- **`pyproject.toml` ist unverändert** und fordert weiterhin `numpy>=2.3.0` und
  `requires-python >=3.13` — im Widerspruch zum Pinning in `requirements.txt`.
  Die Installation muss folglich über `requirements.txt` laufen, nicht über
  `pip install -e .`.
- Ein SeaweedFS-/Prometheus-Stack liegt als `docker-compose.yml` und
  `prometheus/prometheus.yml` vor. **Ob er in den Messungen genutzt wurde, lässt
  sich aus dem Code nicht belegen** — `run_benchmark.py` enthält keinen S3- oder
  boto-Code, sondern lädt ausschließlich per `scp` hoch.

---

## 6. Bekannte Einschränkungen und ausgeschlossene Kombinationen

### 6.1 Erzwungene Regeln (harter Abbruch)

Über die CLI gibt es nur **vier** auslösbare Abbruchbedingungen, die von
Flagwerten abhängen:

| Bedingung | Fundstelle | Reichweite |
|---|---|---|
| `--dem-tiles > 1` zusammen mit `--dem-format != gtiff` | `run_strategy_local_pp`, Z. 2632–2635 | ganzer Prozess |
| `--dem-tiles` negativ | `run_strategy_local_pp`, Z. 2630–2631 | ganzer Prozess |
| `--target-crs` nicht als EPSG-Code parsebar | `_normalize_crs`, Z. 110–115 | ganzer Prozess |
| optionales Paket für `--dem-format zarr`/`netcdf` fehlt | `_check_dem_format_deps`, Z. 348–353 | ganzer Prozess |

Das ist damit die **einzige explizit erzwungene Flag-Kombinationsregel** im
gesamten Programm: Kachelung nur mit GeoTIFF.

**Wichtig zur Reichweite:** Die `try`-Blöcke der Runner beginnen erst **nach**
dem Setup (onthefly Z. 2514, local_pp Z. 2694, full_pp Z. 3056, local_reference
Z. 3511), und `main()` umschließt den Runner-Aufruf mit keinem `try`. Alles was
im Setup wirft, beendet daher den **gesamten Benchmark**, nicht nur den einzelnen
Lauf. Bei `--strategy all --dem-format zarr` ohne installiertes `zarr` läuft also
`onthefly` durch, dann stirbt der Prozess in `local_preprocessing`, bevor
`full_preprocessing` startet.

### 6.2 Kombinationen mit Warnung

Genau zwei Stellen geben eine Warnung aus und arbeiten dann ohne den Effekt
weiter:

| Kombination | Verhalten | Fundstelle |
|---|---|---|
| `--dem-layout != striped` bei `--dem-format != gtiff` | `[warn]`, Layout wirkungslos | Z. 2622–2624 |
| `--snap-dem-to-s2` bei Nicht-UTM-Ziel-CRS | `[warn]`, `snap` wird abgeschaltet | Z. 2654–2657 |

### 6.3 Kommentarlos ignorierte Kombinationen

Es gibt **keine** Prüfung „Flag X passt nicht zu Strategie Y". Wird ein Flag zur
falschen Strategie gesetzt, passiert schlicht nichts — ohne Meldung:

| Flag | Wirkt nur bei | Bei anderen Strategien |
|---|---|---|
| `--dem-cache`, `--dem-format`, `--dem-layout`, `--dem-tiles`, `--snap-dem-to-s2`, `--resample-s2-to-dem` | `local_preprocessing` | still ignoriert |
| `--force-target-crs` | `onthefly` | still ignoriert |
| `--reproject-s2`, `--fullpp-upload-profile`, `--fullpp-save-format` | `full_preprocessing` | still ignoriert |
| `--target-crs` | `local_pp`, `full_pp`, `local_reference` | bei `onthefly` ignoriert — `main()` druckt aber trotzdem „Target-CRS: … (Override)" |
| `--local-resampling` | `local_pp`, `full_pp`, `local_reference`, Accuracy | bei `onthefly` gegenstandslos |
| `--include-full-pp` | `--strategy all` | wirkungslos |

Weitere stille Fälle:

- `--dem-tiles 0` wird durch `int(… or 1)` (Z. 2629) still auf `1` gesetzt und
  wirft **nicht**.
- `--repeat 0` oder negativ führt zu einer leeren Schleife — keine Läufe, keine
  Meldung. Für `--accuracy-check` standalone ist genau das der dokumentierte Weg.
- `--min-free-gb <= 0` deaktiviert die Plattenprüfung ohne Hinweis.
- `--dem-layout` wirkt auch bei `full_preprocessing` **nicht**, obwohl dort ein
  DEM geschrieben wird: `reproject_dem_local` wird ohne `layout=` gerufen (Z. 3104)
  und benutzt seinen Default `striped`; anschließend überschreibt der
  Clean-Rewrite das Layout ohnehin mit `--fullpp-upload-profile`.

### 6.4 Automatisches Überspringen

`LARGE_EXTENTS_FOR_FULL_PP = ("xlarge", "xxlarge")` (Z. 52). Begründung im Code:
rund 1170 Range-Requests pro xlarge-Lauf, gemessen im `nginx_access_log`, wodurch
der CDSE-Job regelmäßig in einen Timeout läuft.

| `--include-full-pp` | `--extent-size` | Ergebnis |
|---|---|---|
| `no` | beliebig | `full_preprocessing` entfällt |
| `auto` (Default) | `small`, `medium`, `large` | läuft mit |
| `auto` | `xlarge`, `xxlarge` | entfällt, mit Warnung |
| `yes` | beliebig | läuft immer mit |

Die Logik greift **nur bei `--strategy all`**. Bei explizitem
`--strategy full_preprocessing` läuft die Strategie auch bei `xxlarge`.

`local_reference` ist in `--strategy all` grundsätzlich nicht enthalten und muss
immer explizit angefordert werden.

### 6.5 Inhaltliche Einschränkungen der Auswertung

1. **`--workflow resample` ist lokal nicht nachgebildet.**
   `_apply_local_workflow` behandelt es identisch zu `merge_add` (Z. 3376–3379).
   Ein `--reference-check` mit diesem Workflow vergleicht den CDSE-seitigen
   3035@30m→UTM@10m-Roundtrip gegen ein schlichtes lokales `B04 + DEM`; die
   gemessene Abweichung ist konstruktionsbedingt.
2. **`--accuracy-check` prüft immer nur eine Teststrategie.** Die Auswahlschleife
   (Z. 4794–4802) überschreibt den Kandidaten; liefen `local_preprocessing` und
   `full_preprocessing` beide erfolgreich, gewinnt der letzte Eintrag. Ob das
   Absicht ist, geht aus dem Code nicht hervor.
3. **`--reference-check` ist nicht auf die Session beschränkt** — entgegen dem
   Help-Text (Z. 4498–4501). Der Code iteriert über eine fest verdrahtete
   Dreierliste und prüft per Dateisystem-Lookup, ob ein passender Ordner
   existiert, unabhängig von der Session.
4. **Das Run-Matching ist streng.** Region, exakter Extent (Toleranz ≈ 10 m) und
   Workflow müssen übereinstimmen, sonst findet kein Vergleich statt.
5. **DB-Spalten sind je nach Strategie leer.** `run_strategy_onthefly` übergibt
   an `import_run` weder `local_resampling` noch `target_crs`;
   `full_preprocessing` und `local_reference` übergeben keine `dem_*`-Felder. Nur
   `local_preprocessing` füllt alle DEM-Spalten. Auswertungen, die nach
   `dem_layout` oder `dem_format` gruppieren, sehen deshalb ausschließlich
   local_pp-Läufe.
6. **Coverage, Korrelation, ME_bias und max_diff werden nicht persistiert** — sie
   erscheinen nur in der Konsolenausgabe.
7. **Schema-Diskrepanz bei `accuracy`:** `accuracy_calculator._ensure_accuracy_schema`
   legt die Tabelle **ohne** `reference_run_id` an, `database.create_database`
   **mit**. Wer die Tabelle zuerst über den Standalone-CLI erzeugt, bekommt die
   Spalte erst beim nächsten Benchmark-Lauf nachmigriert.
8. **`--run-type cold/hot` ist ein reines Label.** Es wird kein Backend-Cache
   geleert; die Bedeutung hängt allein davon ab, in welcher Reihenfolge die Läufe
   stattfinden.
9. **`--dem-cache` ändert das DB-Label** auf `local_pp_cached`. Auswertungen, die
   nach `crs_strategy = 'local_preprocessing'` filtern, übersehen diese Läufe.

### 6.6 Betriebsvoraussetzungen

- **Das Skript muss aus dem Repo-Root gestartet werden.** `run_openeo` ruft
  `openeotest.py` über einen relativen Pfad auf (Z. 1029) und
  `_load_bench_template` liest `Path("scenarios")` relativ (Z. 1145).
- `rasterio` ist ein Top-Level-Import — ohne das Paket startet gar nichts.
- `ssh` und `scp` müssen im PATH liegen; nötig für `local_preprocessing` und
  `full_preprocessing`. `onthefly` und `local_reference` brauchen sie nicht.
- Fehlt `duckdb`, gibt es beim Persistieren nur eine Warnung, keinen Abbruch.
- Der Webserver muss unter `--host` / `--web-path` / `--url-base` erreichbar sein
  und das nginx-Access-Log unter `/var/log/nginx/access.log` führen, sonst
  bleiben die `nginx_access_log`-Zeilen leer (der Lauf gilt trotzdem als
  erfolgreich).

### 6.7 Veraltete oder irreführende Help-Texte

Diese Abweichungen zwischen Help-Text und Code sind belegt und sollten beim Lesen
von `--help` beachtet werden:

| Flag | Help-Text sagt | Code tut |
|---|---|---|
| `--strategy` | „`all` = onthefly + local_preprocessing" | `all` enthält **auch** `full_preprocessing` |
| `--dem-cache` | Cache `dem_{region}.tif`, „einmal pro Region" | Cache `dem_{region}_{extent_size}.tif`, pro Region **und** Ausdehnung |
| `--local-resampling` | „nur local_preprocessing" | wirkt auch bei `full_pp`, `local_reference` und im Accuracy-Vergleich |
| `--dry-run-cleanup` | „beides muss explizit gesetzt sein" | dieses Flag allein startet den Cleanup im Probelauf (folgenlos, da nichts gelöscht wird) |
| `--extent-size medium` | „10 km" | fester Regions-Extent von 0.10° Höhe; je nach Region 9,9–13,8 km breit |
| `--target-crs` | nennt nur `local_pp` und `full_pp` | wirkt auch bei `local_reference`, **nicht** bei `onthefly` |
| `--dem-tiles` | „nur local_preprocessing, nur gtiff" | die Formatbedingung wird erzwungen, die Strategiebedingung nicht |
| `--reference-check` | „jede in dieser Session gelaufene Strategie" | feste Dreierliste, per Dateisystem-Lookup auch über Sessions hinweg |

### 6.8 Nicht aus dem Code klärbar

- **Ob eine Kombination auf CDSE tatsächlich durchläuft.** Deshalb ist die Spalte
  „Status auf CDSE" in Abschnitt 4 durchgehend `TBD`.
- Ob der SeaweedFS-/S3-Stack in den Messungen genutzt wurde.
- Warum `scenarios/bratislava_10km_2018_cdse.json` im Arbeitsstand fehlt (ohne
  Git-Historie nicht klärbar).
- Ob die Null-Initialisierung im Accuracy-Alignment beabsichtigt ist:
  `align_rasters` initialisiert das Zielarray mit `np.zeros` und übergibt
  `reproject` weder `src_nodata` noch `dst_nodata` — Bereiche außerhalb des
  Test-Extents bleiben damit 0 und zählen als gültige Pixel. Im Code steht kein
  Kommentar dazu.
- Ob die Kombination `--resample-s2-to-dem` mit `--workflow resample`
  beabsichtigt ist: `resamplecubespatial1.target` zeigt fest auf
  `reducedimension_dem`, während `merge1.cube2` bei diesem Workflow auf
  `resamplespatial2` zeigt — S2 wird also auf ein anderes Gitter gezogen als das,
  welches in den Merge eingeht.

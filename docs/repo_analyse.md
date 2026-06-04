# Repository-Analyse: openeobench-original vs. Fork

Vergleich der beiden Repositorys

- **Original**: `C:\Users\Yanic\Documents\openEO\openeobench-original` (ITC/CRIB `openeobench`)
- **Fork**: `C:\Users\Yanic\Documents\openEO\openeobench`

Stand: 2026-06-05

---

## 1. ORIGINAL-REPO – Datei für Datei

Das Original besteht im Wesentlichen aus sechs Python-Modulen plus einem ausführbaren `openeobench`-Skript (gleiches Format wie ein Python-Modul, ohne `.py`-Endung). Zusätzlich liegen Hilfsskripte in `utils/`.

### 1.1 `openeobench` (CLI-Einstieg, ~395 Zeilen)

Reine Argparse-Hülle. Importiert Funktionen aus den anderen Modulen und vermittelt zwischen CLI und Implementierung.

| Funktion | Aufgabe |
|----------|---------|
| `main()` | Definiert sieben Subcommands und delegiert. Kein eigenes Geschäftsverhalten. |

**CLI-Befehle:**

| Subcommand | Zweck |
|-----------|-------|
| `service` | Prüft OpenEO-Service-Endpoints (HTTP-Status, Antwortzeit, Body-Size) – CSV-Input oder einzelne URL. |
| `run` | Führt ein OpenEO-Szenario (Process-Graph-JSON) gegen ein Backend aus. |
| `run-summary` | Aggregiert Timing-Metriken aus mehreren `results.json`-Läufen (CSV oder Markdown). |
| `result-summary` | Statistische Auswertung der Output-GeoTIFFs (min/max/mean/stddev pro Datei). |
| `service-summary` | Erfolgs-Ratio und Antwortzeit-Statistiken aus den `service`-CSV-Dateien. |
| `process` | Prüft Prozess-Verfügbarkeit eines Backends und vergleicht gegen OpenEO-Level L1–L4. |
| `process-summary` | Aggregat-Report über mehrere Backends hinweg (Compliance pro Level). |
| `visualize` | Erzeugt PNG-Matrix und Markdown-Statistik aus GeoTIFF-Ergebnissen. |

### 1.2 `openeo_checker.py` (~1.406 Zeilen)

Kern für Service-Verfügbarkeits-Checks UND Result-Summary-Logik.

| Funktion | Aufgabe |
|----------|---------|
| `parse_json_content(content)` | JSON-Parsing-Helper, gibt `(ok, parsed)` zurück. |
| `check_url(url)` | Einzel-GET mit Timing; liefert Antwortzeit (ms), Status, Reason, Validität und Body-Größe. |
| `process_single_url(...)` | Misst genau eine URL und schreibt das Ergebnis als CSV-Zeile (mit `--append`-Option). |
| `process_csv(...)` | Liest eine CSV-Datei mit URLs, prüft sie der Reihe nach und schreibt eine Ergebnis-CSV. |
| `parse_date(s)` / `is_file_in_date_range(...)` | Helfer für die `stats`-Funktion (Datums-Filter). |
| `calculate_statistics_from_files(...)` | Aggregiert über alle CSVs eines Ordners (Erfolgs-Ratio, Avg-Antwortzeit, Stddev, normalisierte Zeit ms/KByte). |
| `calculate_statistics_from_single_file(...)` | Gleiche Statistik wie oben, aber für eine einzelne CSV; schreibt CSV oder Markdown. |
| `calculate_statistics_flexible(...)` | Wrapper, der je nach Input-Pfad (Datei oder Ordner) eine der beiden Statistik-Funktionen wählt. |
| `run_openeo_scenario(api_url, input, out)` | Dünner Wrapper, importiert `run_task` aus `openeotest.py` und ruft ihn auf. |
| `run_summary_task(...)` | Sucht rekursiv alle `results.json`, gruppiert nach (Szenario, Backend) und schreibt Timing-Statistik (submit, queue, processing, download, total) als CSV oder Markdown. |
| `write_run_summary_csv(...)` / `write_run_summary_markdown(...)` | Formatierte Ausgabe der Timing-Statistik (Mean ± Stddev). |
| `result_summary_task(...)` | Liest `results.json` und ruft pro gefundenes GeoTIFF `gdalinfo -stats` auf; sammelt Min/Max/Mean/Stddev plus Metadaten (CRS, Raster-Size, Pixel-Size, NoData). |
| `get_file_statistics(path)` / `parse_gdalinfo_stats(text)` | GDAL-basierte Statistikgewinnung mit Regex-Parser; Fallback-Werte wenn `gdalinfo` fehlt. |
| `write_file_statistics_csv/_markdown(...)` | Tabellarische File-Statistiken inkl. Run-vs-Backend-Matrix. |
| `write_run_backend_matrix(...)` | Heuristische Backend-Erkennung aus Run-Namen, Matrix mit Datei-Zählern. |
| `has_geospatial_files(dir)` | Erkennt Raster-Dateien (per Extension + `gdalinfo`-Probe). |
| `main()` | Eigener Mini-CLI (`check`, `stats`) – wird aber im Produktiv-CLI nicht verwendet. |

### 1.3 `openeotest.py` (~3.080 Zeilen)

Komplette Szenario-Ausführung gegen OpenEO-Backends, Summarize, Visualize, Compare.

| Funktion | Aufgabe |
|----------|---------|
| `get_geotiff_files(dir)` | Findet GeoTIFFs per `gdalinfo`/`file`-Probe; ignoriert Aux-Dateien. |
| `load_backends(file)` / `load_process_graphs(dir)` | Liest Backends-JSON und Process-Graph-Verzeichnis ein. |
| `connect_to_backend(b)` / `authenticate(conn, name)` | OpenEO-Connection-Aufbau; Sonderfall Earth Engine (Basic-Auth `group3/test123`), sonst OIDC oder Env-Vars `{BACKEND}_USERNAME`/`_PASSWORD`. |
| `run_task(api_url, scenario, out)` | **Kernfunktion**: legt Output-Ordner an, kopiert Process-Graph als `processgraph.json`, erstellt Job, überwacht Status mit zunehmenden Poll-Intervallen, lädt Ergebnisse runter und schreibt `results.json` mit umfangreichen Timings (`submit_time`, `queue_time`, `processing_time`, `download_time`, `total_time`, `job_status_history`). |
| `_save_results(...)` | Schreibt `results.json`. |
| `summarize_task(patterns, out)` | Globt Output-Ordner, sammelt `results.json`-Inhalte, zählt TIFFs, schreibt Folder-Tabelle plus Backend-gruppierte Statistik (CSV/Markdown). |
| `visualize_task(...)` (zweite Definition überschreibt die erste) | Erstellt eine Matrix-Visualisierung (Ordner als Spalten, Dateien als Zeilen) als PNG und/oder Markdown plus Statistik-Markdown. |
| `load_geotiff_enhanced(...)` | Lädt GeoTIFFs mit rioxarray/GDAL und mehreren Fallbacks. |
| `contrast_stretch(arr, lo, hi)` | Perzentil-Streckung für Visualisierung. |
| `save_high_quality_png(...)` | DPI-gesteuerte PNG-Ausgabe mit optionaler Colorbar. |
| `load_geotiff_as_array(path)` | Robustes Laden eines Arrays für die Matrix-Plots. |
| `create_png_matrix_visualization(...)` / `create_single_png_visualization(...)` | Matplotlib-Subplot-Matrix bzw. Einzelplot. |
| `_create_matrix_visualization(...)` | Markdown-Tabelle mit eingebetteten PNGs. |
| `_write_stats_markdown(...)` / `_write_statistics_csv(...)` | Statistik-Reports (Min/Max/Mean/Stddev, DataType). |
| `_create_geotiff_thumbnail(...)` | Thumbnail-Erzeugung für Markdown. |
| `_analyze_geotiff_bands(...)` / `_get_geotiff_statistics(...)` | Detailstatistik pro Band (auch Histogramm). |
| `_create_placeholder_image(...)` | Fehler-Platzhalter wenn Dateien nicht ladbar. |
| `get_tiff_files(folder)` | Wie oben, einfache Extension-Suche. |
| `compare_geotiffs(ref, comp, tol)` | Pixelvergleich zweier GeoTIFFs (Bounds, Auflösung, Datenwerte); berichtet Anzahl identischer Pixel und Differenzen. |
| `group_folders_by_platform(folders)` | Gruppiert nach erkanntem Backend. |
| `compare_task(patterns, ref, out, tol)` | Vergleicht alle Plattformen gegen eine Referenz (z. B. VITO) und schreibt Compare-Report. |
| `main()` | Eigene CLI mit Subcommands `run`, `summarize`, `visualize`, `compare`. |

**Eigene CLI:** `openeotest.py run|summarize|visualize|compare`.

### 1.4 `process_checker.py` (~856 Zeilen)

Prozess-Compliance-Prüfung gegen OpenEO-Level (L1–L4).

| Funktion | Aufgabe |
|----------|---------|
| `load_process_profiles_from_csv(file)` | Liest `openeo-process-levels.csv`; gruppiert Sub-Level (l2a/l3-ml/...) auf L1–L4. |
| `get_legacy_profiles()` | Konvertiert in das alte „Set pro Level"-Format. |
| `get_backend_processes(api_url)` | Holt `/processes` vom Backend, gibt Liste plus Roh-Details zurück. |
| `check_profile_compliance(...)` / `check_profile_compliance_detailed(...)` | Berechnet Coverage gegen ein Profil (auch experimentell vs. stable). |
| `check_backend_processes(name, url)` | Kompletter Compliance-Check eines Backends gegen alle Profile. |
| `write_process_details_csv(...)` | Detaillierte Tabelle pro Prozess (verfügbar/fehlend/experimentell) als CSV. |
| `write_raw_processes_json(url, file)` | Speichert die Roh-Prozessliste eines Backends als JSON. |
| `process_single_backend(...)` / `process_backends_from_csv(...)` | Einstiegspunkte für Einzel- bzw. Mehrfach-Backend-Tests. |
| `write_results_to_csv(...)` | Zusammenfassungs-CSV über alle Backends. |
| `load_official_process_specs(...)` | Lädt offizielle Specs aus `combined_processes.json`. |
| `compare_process_schemas(...)` / `compare_parameter_schemas(...)` / `compare_return_schemas(...)` | Vergleicht Backend-Schemata mit der offiziellen Spec (Parameter, Returns). |
| `main()` | Eigene CLI für Standalone-Aufruf. |

### 1.5 `process_summary.py` (~716 Zeilen)

Cross-Backend-Aggregator für die `process`-Ergebnisse.

| Funktion | Aufgabe |
|----------|---------|
| `load_process_results(path)` | Sammelt CSV/JSON-Ergebnisse aus Datei oder Ordner. |
| `load_csv_file(p)` / `load_json_file(p)` | Format-spezifische Loader. |
| `generate_process_summary(results)` | Erzeugt das Aggregat-Datenmodell für Markdown/CSV-Output. |
| `write_csv_summary(...)` / `write_markdown_summary(...)` | Tabellarische Ausgabe (Pro-Backend, Pro-Level). |
| `detect_csv_format(file)` | Heuristik: alte Backend-Summary vs. neue Per-Prozess-Tabelle. |
| `load_process_levels_data()` / `aggregate_process_level_data(file)` | Aggregation auf Level-Ebene. |
| `count_mismatches_from_csv(file)` | Zählt Schema-Mismatches pro Backend. |
| `extract_platform_name(url)` / `extract_version_from_url(url)` | URL-Heuristiken. |
| `format_profile_data(d)` | Hilfsformatierer. |
| `main()` | Standalone-CLI. |

### 1.6 `openeo-checker.py` (~504 Zeilen)

Älterer Vorgänger von `openeo_checker.py` (mit Bindestrich!) – inhaltlich praktisch identisch mit dessen `check`/`stats`-Subkommandos, dient als Drop-In für Cron-Jobs.

### 1.7 `crontab.py` (47 Zeilen)

`create_crontab(filename, offset, period)`: Liest eine Liste von Skriptpfaden und erzeugt Cron-Zeilen, die alle 3 Stunden mit einem 5-Min-Offset pro Skript laufen.

### 1.8 `test_commands.py` (408 Zeilen)

Klasse `CommandTester` mit einer Liste vorbereiteter Subprozess-Aufrufe für alle `openeobench`-Subcommands aus dem README. Loggt Erfolg/Fehler in eine Tagesdatei.

### 1.9 `utils/` (nicht primär CLI, Helper-Skripte)

- `fetch_process_tests.py` – Lädt offizielle Process-Tests aus dem OpenEO-Repo.
- `generate_process_levels.py` – Erzeugt die `openeo-process-levels.csv`.
- `count_processes_by_level.py` – Zählt Prozesse pro Level.
- `calculate_statistics.py` / `analyze_timing_statistics.py` – Auswertungs-Skripte für CSVs.
- `convert_to_summary.py` – Konvertiert ältere Reports.

### 1.10 Mitgelieferte Szenarien

**`scenarios/`** – 36 Process-Graphs nach dem Schema `{stadt}_10km_{jahr}_{backend}.json`:

- Städte: Bratislava, Vienna
- Jahre: 2018, 2020, 2024
- Backends: CDSE, Earth Engine, EODC, openEO Platform, SentinelHub, VITO

Pro Kombination ein einfacher NDVI/Band-Process-Graph (Sentinel-2 L2A, 10 km Buffer um Stadt).

**`advanced_scenarios/`** – 20 fortgeschrittene Graphs:

- `ndvi_median_*` – Mediane NDVI-Komposite
- `reducer_mean_*` – Reducer-Demo
- 2 Städte × 5 Backends (SentinelHub fehlt hier) × 2 Operationen

### 1.11 Ergebnis-Speicherung im Original

| Output | Format | Pfad/Bedeutung |
|--------|--------|----------------|
| Service-Checks | CSV (`;`-getrennt) | `outputs/YYYY-MM-DD.csv` mit Spalten URL, Timestamp, Response Time (ms), HTTP Code, Errors, Body Size (bytes) |
| Szenario-Lauf | JSON + GeoTIFF | Pro Lauf ein Ordner mit `processgraph.json`, heruntergeladene `*.tif`/`*.nc`, `results.json` (Timings + Job-Historie) |
| Run-Summary | CSV oder Markdown | Mean ± Stddev für submit/queue/processing/download/total |
| Result-Summary | CSV oder Markdown | Pro Datei: Min/Max/Mean/Stddev plus Metadaten (CRS, Raster-Size, NoData, Pixel-Size) |
| Service-Summary | CSV oder Markdown | Success-Ratio, Avg-Antwortzeit, normalisierte Zeit (ms/KByte) |
| Process | CSV + JSON | Pro Backend Compliance-Tabelle und Roh-Prozessliste |
| Process-Summary | CSV oder Markdown | Backend-vs-Level-Matrix |
| Visualize | PNG + Markdown | Matrix-Plot, eingebettete Thumbnails, Statistik-Tabelle |

---

## 2. FORK – Neue Dateien, die im Original NICHT existieren

### 2.1 `database.py` (349 Zeilen) – DuckDB-Layer

Persistiert Benchmark-Läufe in `benchmark_results.duckdb` mit drei Tabellen.

| Funktion | Aufgabe |
|----------|---------|
| `create_database()` | Legt Tabellen `runs`, `band_statistics`, `accuracy` an; führt idempotente Spaltenmigration für `mae`/`correlation` durch. |
| `get_next_id(conn, table, col)` | Liefert die nächste PK-ID (kein AUTOINCREMENT in DuckDB). |
| `import_run(out_dir, crs_strategy, run_type, preprocessing_time, extent_size)` | Liest `results.json` + `job-results.json` aus einem Run-Ordner, berechnet fehlende `queue_time`/`processing_time` aus `job_status_history`, fügt Run + Band-Statistiken ein, summiert lokales Preprocessing in `total_time`. |
| `fix_runs()` | Migrations-Helper: setzt fehlendes `processing_time` aus `duration_backend` und korrigiert `queue_time`. |
| `show_runs()` | Tabellarische Konsolen-Anzeige aller Runs. |

**Warum hinzugefügt:** Original speichert jeden Lauf nur als isolierte JSON-Datei. Für statistische Auswertungen über CRS-Strategien braucht es einen einheitlichen, abfragbaren Store. DuckDB ist file-basiert (kein Server) und Pandas-/SQL-freundlich.

### 2.2 `accuracy_calculator.py` (342 Zeilen) – CRS-Roundtrip-Genauigkeit

Berechnet RMSE/MAE zwischen Referenz- und Roundtrip-Raster.

| Funktion | Aufgabe |
|----------|---------|
| `load_raster(path)` | rasterio-Read mit Profil/Bounds/CRS/Transform. |
| `align_rasters(ref, test)` | Reprojiziert Test-Raster auf das Referenz-Grid (gleiche oder fremde CRS, Nearest-Resampling). |
| `calculate_metrics(ref, test, nodata)` | Pro Band: gültige Maske (kein NoData, keine Nullen), Diff, RMSE, MAE, ME (Bias), relativ in %, Korrelation, Min/Max-Diff; Gesamt-Aggregat. |
| `print_results(...)` | Konsolen-Report. |
| `_ensure_accuracy_schema(conn)` | Schema-Migration für die accuracy-Tabelle. |
| `save_to_db(results, run_id, db, ref_file)` | Schreibt Overall-Metriken + aggregierte Korrelation in DuckDB. |
| `main()` | CLI mit `--save-db`, `--run-id`, `--db`, `--nodata`, `--output`. |

**Warum hinzugefügt:** Im Original gibt es keine quantitative Vergleichs-Metrik zwischen Referenz- und reprojizierten Rastern; nur ein pixelidentitäts-Check (`compare_geotiffs`) mit Toleranz, ohne RMSE/MAE/Bias/Korrelation.

### 2.3 `analyze.py` (275 Zeilen) – Bootstrap-Analyse über DuckDB

Vergleicht CRS-Strategien (onthefly vs. local_preprocessing vs. backend_preprocessing) anhand robuster Mediane plus Bootstrap-95-%-CIs.

| Funktion | Aufgabe |
|----------|---------|
| `detect_region(scenario)` | Leitet Region aus dem letzten Token des Szenarionamens ab (berlin, hamburg, ...). |
| `fetch_runs(db)` / `fetch_accuracy(db)` | DuckDB-Queries; bei Accuracy pro Run der Median bei mehreren Einträgen. |
| `bootstrap_median_ci(values, iters, conf)` | Perzentil-Bootstrap-CI (Seed 42, 2000 Iter). |
| `summarize(runs, accuracy_map)` | Mediane für `total_time`/`queue`/`processing`/`preprocessing`, Cold/Hot-Zählung, Credits, RMSE-/MAE-Median pro Strategie. |
| `fmt(v, prec)` | Tabellen-Formatierer. |
| `print_table(label, metrics)` | Side-by-side-Strategie-Vergleich. |
| `write_csv(path, results)` | Optionaler CSV-Export der Gruppen-Statistiken. |
| `main()` | CLI mit `--db`, `--csv`, `--group-by region`, `--region`. |

**Warum hinzugefügt:** Original liefert nur Mittelwert ± Stddev. Cloud-Varianz erfordert Median + Bootstrap-CI, um Strategien fair zu vergleichen, sowie eine pro-Region-Gruppierung.

### 2.4 `run_benchmark.py` (337 Zeilen) – Multi-Strategy-Runner

Orchestriert die drei CRS-Strategien (`onthefly`, `backend_preprocessing`, `local_preprocessing`) mit Wiederholungen und Cold/Hot-Klassifizierung.

| Funktion | Aufgabe |
|----------|---------|
| `_ts()` / `_make_outdir(base, strategy)` | Zeitstempel-Verzeichnisse pro Run. |
| `reproject_dem_local(in, out, dst_crs)` | Lokale rasterio-Reprojektion (Bilinear) eines DEM, Laufzeit-Messung. |
| `run_openeo(api, scenario, out)` | Subprozess-Aufruf von `openeotest.py run`, prüft, dass `results.json` geschrieben wurde. |
| `_run_type_for(idx, mode)` | `auto`: erster Lauf = cold, alle weiteren = hot. |
| `run_strategy_onthefly(args, i)` | Single-Step: nur CDSE-Job. |
| `run_strategy_backend_pp(args, i)` | Single-Step: CDSE mit `resample_spatial` vor merge. |
| `run_strategy_local_pp(args, i)` | Drei Schritte: DEM-Download → lokale Reprojektion → `load_stac`-Hauptlauf. Summiert Preprocessing-Zeit. |
| `print_summary(results)` | Konsolen-Aggregat. |
| `main()` | CLI mit `--strategy`, `--repeat`, `--run-type cold|hot|auto`, `--api-url`, Szenario-Override-Flags. |

**Warum hinzugefügt:** Im Original muss jedes Szenario manuell mit `openeobench run` gestartet werden. Für eine MSc-/Benchmark-Studie mit n Wiederholungen × 3 Strategien × m Regionen ist eine automatisierte Schleife mit konsistenter Cold/Hot-Markierung und automatischem DB-Import nötig.

### 2.5 `latency_check.py` (249 Zeilen) – Netzwerklatenz + Uhrenoffset

NTP-ähnliches Verfahren über den HTTP-`Date`-Header der CDSE-API.

| Funktion | Aufgabe |
|----------|---------|
| `_ensure_latency_table(conn)` | Tabelle `latency_measurements` in DuckDB. |
| `_probe_once(url, verify_ssl)` | Eine Probe: `t_send`, HTTP-Response, `t_recv`, Server-Zeit aus `Date`-Header, Roundtrip/Latenz/Offset. |
| `measure(url, n, verify_ssl)` | n Probes mit Pause, aggregiert (Mean/Median/Stddev von Latenz und Offset, Best-Probe-Offset). |
| `save_to_db(stats)` | Persistiert Aggregat + Raw-Probes als JSON. |
| `print_summary(stats)` | Konsolen-Report mit Interpretation der Uhrenrichtung. |
| `main()` | CLI mit `--url`, `--n`, `--no-db`, `--no-ssl-verify`. |

**Warum hinzugefügt:** Für die Vergleichbarkeit von lokal gemessenem Preprocessing (`local_preprocessing`-Strategie) und Backend-Timings muss der Uhrenoffset zwischen lokalem Rechner und CDSE bekannt sein. Im Original existiert kein Netzwerk-/Clock-Modul.

### 2.6 `ntp_check.py` (126 Zeilen) – NTP-vs.-HTTP-Date-Vergleich

Doppelmessung: NTP-Offset (über `ntplib`, gegen `pool.ntp.org`) und HTTP-Date-Offset gegen CDSE im gleichen Loop. Funktionen: `measure_ntp_offset()`, `measure_http_offset()`, `main()`.

**Warum hinzugefügt:** Validiert die HTTP-Date-Methode aus `latency_check.py` gegen eine echte NTP-Quelle (Plausibilitätscheck).

### 2.7 `reproject_dem.py` (41 Zeilen) – Ad-hoc-Skript

Eigenständiges Skript ohne Funktionen: öffnet ein fest verdrahtetes DEM (`outputs/dem_download/openEO_2011-01-06Z.tif`), reprojiziert nach EPSG:32633 (UTM 33N), misst Zeit. Wurde später als `reproject_dem_local()` in `run_benchmark.py` integriert.

**Warum hinzugefügt:** Erste manuelle Validierung der lokalen Reprojektion, bevor sie automatisiert wurde.

### 2.8 Weitere Fork-only Artefakte (kein Python)

| Datei | Zweck |
|-------|-------|
| `benchmark_results.duckdb` | Persistente DuckDB-Datenbank mit Runs/Accuracy/Band-Stats/Latency. |
| `docker-compose.yml` + `prometheus/prometheus.yml` | Prometheus-Setup (vermutlich für Monitoring während Benchmark-Läufen). |
| `accuracy_results.json` | Ausgabe von `accuracy_calculator.py`. |
| `scenario_runner.log` | Laufprotokoll aus `openeotest.py`-Aufrufen. |
| `data/dem_reprojected_32633.tif`, `data/dem_stac_item.json` | Lokal reprojiziertes DEM + STAC-Item für `load_stac`. |
| 31 neue Process-Graphs in `scenarios/` (siehe Abschnitt 4). |

---

## 3. FORK – Geänderte Dateien (im Original ebenfalls vorhanden)

`diff` zwischen den beiden Repos ergibt nur EINE inhaltliche Code-Änderung: `openeotest.py`. Alle anderen gemeinsamen Python-Dateien (`openeo_checker.py`, `process_checker.py`, `process_summary.py`, `crontab.py`, `test_commands.py`, `openeo-checker.py`, das `openeobench`-Skript), `README.md`, `requirements.txt`, `pyproject.toml`, `backends.csv`, `backends.json`, beide JSON-Process-Listen, `combined_processes.json`, `docs/`, `utils/`, `advanced_scenarios/` sind **byte-identisch**.

### 3.1 `openeotest.py` – einzige Code-Änderung

In `run_task()`, direkt nach erfolgreichem Job-Download und vor dem Fehler-Branch, wurden 12 Zeilen eingefügt, die zusätzliche Kosten- und Verbrauchsmetriken vom Backend abrufen:

```
# Fetch job details for credits and usage info
try:
    job_info = job.describe()
    results["credits"] = job_info.get("costs")
    usage = job_info.get("usage", {})
    results["cpu_seconds"] = usage.get("cpu", {}).get("value")
    results["duration_backend"] = usage.get("duration", {}).get("value")
    results["input_pixels_mp"] = usage.get("input_pixel", {}).get("value")
    results["max_memory_gb"] = usage.get("max_executor_memory", {}).get("value")
    logger.info(f"Job credits: {results['credits']}, CPU: {results['cpu_seconds']}s")
except Exception as e:
    logger.warning(f"Could not fetch job details: {e}")
```

**Was sich ändert:**

- `results.json` enthält jetzt fünf neue Schlüssel: `credits`, `cpu_seconds`, `duration_backend`, `input_pixels_mp`, `max_memory_gb`.
- `database.py` und `analyze.py` lesen diese Felder; im Original werden sie nicht geschrieben und können daher dort nicht ausgewertet werden.

**Warum geändert:** CDSE rechnet pro Lauf in Credits ab; CPU-Sekunden und Speicher-Maximum sind für Kosten-/Effizienz-Vergleiche zwischen den drei CRS-Strategien essenziell. `duration_backend` dient als Fallback in `database.py`, wenn `job_status_history` keinen `running`-Status liefert.

### 3.2 Process-Graphs in `scenarios/`

- **Entfernt im Fork:** `bratislava_10km_2018_cdse.json` (1 Datei).
- **Übernommen identisch:** 35 weitere Bratislava-/Vienna-Graphs sowie alle 20 advanced-Scenarios.
- **Neu im Fork:** 31 zusätzliche Process-Graphs (s. Abschnitt 4).

### 3.3 Output-Ordner

`outputs/` enthält im Fork weiterhin die vier Original-CSVs (2025-06-18..21), zusätzlich aber neun strukturierte Test-Verzeichnisse (`01_baseline` ... `08_reducer_mean`) und zahlreiche tatsächlich heruntergeladene GeoTIFFs/JSONs. Diese sind reine Laufdaten, kein geänderter Code.

---

## 4. VERGLEICH – tabellarisch

### 4.1 Übernommen wie im Original

| Bereich | Status |
|---------|--------|
| `openeobench`-CLI mit `service`/`run`/`run-summary`/`result-summary`/`service-summary`/`process`/`process-summary`/`visualize` | identisch |
| `openeo_checker.py` (Service-Checks + Result-Summary) | identisch |
| `process_checker.py` + `process_summary.py` (L1–L4-Compliance) | identisch |
| `openeo-checker.py` (Cron-Variante) | identisch |
| `crontab.py`, `test_commands.py` | identisch |
| `utils/`, `docs/`, `combined_processes.json`, `openeo_process_levels.json`, `openeo_processes_1.0.json`, `openeo-process-levels.csv` | identisch |
| `backends.csv`, `backends.json` | identisch |
| 35/36 Original-Scenarios + alle 20 advanced-Scenarios | identisch |
| `pyproject.toml`, `requirements.txt`, `README.md` | identisch |

### 4.2 Geändert gegenüber Original

| Datei | Änderung |
|-------|----------|
| `openeotest.py` | +12 Zeilen in `run_task()`: Credits + CPU + Backend-Duration + Input-Pixel + Max-Memory aus `job.describe()` ins `results.json` schreiben. Sonst keine Änderung. |
| `scenarios/` | 1 Datei entfernt, 31 hinzugefügt (siehe 4.3). |

### 4.3 Komplett neu im Fork

**Neue Python-Module** (alle benchmark-/CRS-fokussiert):

- `database.py` – DuckDB-Persistenz für Runs, Band-Stats, Accuracy
- `accuracy_calculator.py` – RMSE/MAE/Bias/Korrelation zwischen Rastern
- `analyze.py` – Bootstrap-Median-Vergleich CRS-Strategien
- `run_benchmark.py` – Strategy-Orchestrator (onthefly / backend_pp / local_pp)
- `latency_check.py` – HTTP-Date-Offset gegen CDSE
- `ntp_check.py` – NTP-vs-HTTP-Offset-Validierung
- `reproject_dem.py` – Stand-alone-Reprojektions-Skript

**Neue Szenarien (31)** in `scenarios/`:

- CRS-Baselines: `01_baseline_no_transform`, `02_reproject_to_laea`
- Cross-CRS-Merge: `06_*`, `07_cross_crs_merge_berlin`, `07c_local_preprocessing_merge_berlin`, `07d_load_url_test`, `07e_load_stac_test`, `crs_cross_mask_scenario_b`, `crs_cross_merge_scenario_b2`
- DEM-Pipeline: `08_download_dem`, `09_reproject_dem_cdse`, `10b_download_dem_hamburg`
- Accuracy: `accuracy_reference_utm`, `accuracy_roundtrip_3035`
- On-the-fly über 9 Städte: `10a_onthefly_hamburg`, `11a_onthefly_muenchen`, `12a_onthefly_dresden`, `13a_onthefly_wien`, `14a_onthefly_amsterdam`, `15a_onthefly_prag`, `16a_onthefly_zuerich`, `17a_onthefly_kopenhagen`, `18a_onthefly_rom`, `10c_s2_zonegrenze_test`
- Bench-Pendants (kompakter): `bench_onthefly_amsterdam`, `bench_onthefly_berlin`, `bench_onthefly_hamburg`, `bench_onthefly_rom`, `bench_onthefly_wien`, `bench_onthefly_zuerich`

**Neue Infrastruktur:**

- `benchmark_results.duckdb` (Datenbank)
- `docker-compose.yml` + `prometheus/`
- `data/` mit lokal reprojiziertem DEM + STAC-Item
- `accuracy_results.json`, `scenario_runner.log`

### 4.4 Vom Original NICHT genutzt im Fork

| Original-Feature | Status im Fork-Workflow |
|------------------|--------------------------|
| `openeobench service` / `service-summary` (Endpoint-Verfügbarkeit) | Code vorhanden, aber für CRS-Benchmark irrelevant – wird vermutlich nicht aufgerufen. |
| `openeobench process` / `process-summary` (L1–L4-Compliance) | Code vorhanden, im neuen Workflow nicht eingesetzt – CRS-Themen brauchen keine Compliance-Tabelle. |
| `openeobench visualize` (PNG-Matrix) | Vermutlich punktuell genutzt; durch `analyze.py`/DuckDB ersetzt für quantitative Auswertung. |
| `openeobench result-summary` (`gdalinfo`-Regex-Parsing) | Im Fork ersetzt durch `database.py` → `band_statistics`-Tabelle (STAC-Asset-Metadaten, kein gdalinfo-Subprocess). |
| `openeobench run-summary` (Mean ± Stddev pro Backend) | Im Fork ersetzt durch `analyze.py` (Median + Bootstrap-CI, pro CRS-Strategie). |
| `advanced_scenarios/` (NDVI-Median, Reducer-Mean) | Im Fork unverändert mitgeschleppt, aber für die CRS-Studie nicht aufgerufen. |
| `compare_task` in `openeotest.py` (Pixel-Vergleich mit Toleranz) | Übernommen, aber durch `accuracy_calculator.py` (RMSE/MAE statt bool-Tolerance) ergänzt/überholt. |
| `crontab.py`, `openeo-checker.py`, `test_commands.py` | Liegen herum, sind aber Teil des Monitoring-Workflows des Originals, nicht der Benchmark-Studie. |

---

## 5. FEHLENDE FEATURES – im Original UND im Fork

### 5.1 CRS-bezogen

| Feature | Original | Fork |
|---------|----------|------|
| Liste aller verwendeten CRS pro Run (Input/Output) als First-Class-Spalte | nein | nur `output_crs` als EPSG-Code, kein Input-CRS-Tracking |
| CRS-Transformations-Graph (welche Schritte transformieren wohin) | nein | nein |
| Vergleich `resample_spatial` vs. echte Reprojektion (PROJ-Pipeline) | nein | nein |
| Validierung der Achsenreihenfolge (lat/lon vs. lon/lat) bei Cross-CRS-Merge | nein | nein |
| Erkennung von Pixel-Misalignment nach Reprojektion (Subpixel-Shift) | nein | nein |
| UTM-Zonen-Grenze: automatischer Test über mehrere Zonen, Vergleich der Stitch-Naht | nein | manuell als `10c_s2_zonegrenze_test`, keine Auswertung |
| Resampling-Methoden-Sweep (nearest/bilinear/cubic) und deren RMSE-Effekt | nein | nein – `accuracy_calculator.py` nutzt fest `Resampling.nearest` |
| Round-trip-Accuracy mit mehreren Ziel-CRS (3035/32632/32633/3857/4326) als Matrix | nein | nur einzelne Paarvergleiche möglich |
| CRS-Metadaten aus STAC-Items (proj:transform, proj:wkt2) systematisch loggen | nein | partiell (`proj:epsg`, `proj:shape`, `proj:bbox`) – `proj:transform`/`wkt2` fehlen |
| Datum/Ellipsoid-Konsistenz prüfen (ETRS89 vs. WGS84-Mischfälle) | nein | nein |

### 5.2 Statistisch

| Feature | Original | Fork |
|---------|----------|------|
| Bootstrap-CIs | nein | ja (nur Median, nur `total_time`) |
| Verteilungstests (Normalität, Wilcoxon, Mann-Whitney zwischen Strategien) | nein | nein |
| Outlier-Detection (IQR/MAD) vor der Aggregation | nein | nein |
| Effektgröße (Cliff's δ, Cohen's d) für Strategie-Vergleiche | nein | nein |
| Trim/Winsorize-Optionen | nein | nein |
| Mehrfachvergleichs-Korrektur (Bonferroni/BH) | nein | nein |
| Mixed-Effects oder Regression über (Region, Strategie, Bewölkung) | nein | nein |
| Cold-/Hot-Run-Statistik getrennt (nicht nur Zählung) | nein | nein – `analyze.py` zählt Cold/Hot, vergleicht aber nicht |
| Pro-Band-Aggregation in `analyze.py` (aktuell wird Korrelation/MAE über Bänder gemittelt) | nein | rudimentär |
| Histogramme/Boxplots als CSV-Export | nein | nein |
| Reproduzierbarkeit: Bootstrap-Seed dokumentiert, aber kein Run-Manifest mit Software-Versionen | nein | nur Seed fixiert |

### 5.3 Auswertung

| Feature | Original | Fork |
|---------|----------|------|
| HTML-/PDF-Reports | nein (nur MD/CSV) | nein |
| Plot-Output (Boxplots, Violin, ECDF) aus `analyze.py` | nein | nein – nur Konsolentabellen und CSV |
| Vergleich Backend-Costs (Credits) gegen Wallclock-Zeit als Pareto-Plot | nein | nein – `credits_total` wird nur summiert |
| Zeitliche Drift (Performance über Wochen) | nein | nein |
| Cross-Region-Heatmap (Region × Strategie) | nein | nein – nur Tabellen pro Region |
| Verknüpfung Latency-Messung ↔ Run (welcher Run lief unter welcher Latenz?) | nein | nein – `latency_measurements` ist isoliert |
| Job-Status-Anomalien (mehrfach `queued`, Retries) automatisch markieren | nein | nein |
| Vergleich Backend-Versionen (`processing:software`) zwischen Läufen | wird gespeichert (`backend_version` Original) | wird in DuckDB gespeichert, aber nirgends ausgewertet |
| Wetterdaten-/Bewölkungs-Korrelation (S2-Cloud-Probability als Confounder) | nein | nein |
| Failure-Mode-Analyse: welche Szenarien scheitern wann (Timeout vs. CRS-Mismatch vs. Auth) | nein | nein – Fehler stehen nur in `results.json`, kein Aggregat |
| Memory-/CPU-Effizienz: `cpu_seconds`/`max_memory_gb` werden gespeichert, aber nicht in `analyze.py` ausgewertet | nein | gespeichert, nicht ausgewertet |
| Statistische Power-Analyse („wie viele Wiederholungen brauche ich noch?") | nein | nein |
| Vergleich Backend-eigene Zeit (`duration_backend`) vs. Wallclock-Zeit (Diff = Netzwerk + Queue) | nein | nein |
| Datei-Größen-/Komprimierungs-Statistik der Outputs | nein | nein |
| Geometrie-Validierung: tatsächlich abgedeckte Bbox = angeforderte Bbox? | nein | nein |

---

## Kurzfazit

Der Fork lässt das CRIB-`openeobench` weitgehend unverändert (nur **eine** Code-Änderung in `openeotest.py`, +12 Zeilen für Credits/Usage). Die eigentliche Arbeit liegt **neben** dem Original: sieben neue Python-Module mit DuckDB-Persistenz (`database.py`), CRS-Accuracy-Berechnung (`accuracy_calculator.py`), Bootstrap-Auswertung (`analyze.py`), Multi-Strategy-Runner (`run_benchmark.py`) und Netzwerk-/Uhren-Profiling (`latency_check.py`, `ntp_check.py`). Plus 31 neue Process-Graphs für CRS-, Cross-CRS-Merge- und DEM-Pipeline-Tests über zehn europäische Städte und drei CRS-Strategien.

Hauptlücken in beiden Repos: keine inferenzstatistischen Tests (nur Mediane + Bootstrap-CI), keine Plot-Ausgabe, kein systematischer Resampling-/Ziel-CRS-Sweep, keine Verknüpfung von Latenz/Bewölkung mit Run-Metriken, und die im Fork gespeicherten Felder `cpu_seconds`, `max_memory_gb`, `backend_version` werden noch nicht ausgewertet.

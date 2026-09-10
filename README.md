# openeobench

Benchmark zu CRS-Transformationsstrategien für Rasteroperationen auf openEO-Backends.

Dieses Repository enthält den Code, die Messdaten und die Auswertungsskripte zu meiner
Bachelorarbeit an der TU Berlin, Fachgebiet Database Systems and Information Management.

## Worum es geht

Werden zwei Raster mit unterschiedlichen Koordinatenreferenzsystemen verrechnet, muss eines
transformiert werden. Diese Transformation kann vorab beim Nutzer stattfinden oder erst im
Backend während der Ausführung. Der Benchmark vergleicht beide Wege nach Laufzeit und
Ergebnisgenauigkeit, gemessen gegen eine lokal kontrollierte Referenzpipeline.

Gemessen wurde auf dem Copernicus Data Space Ecosystem und auf Terrascope, mit sieben
Raster-Raster-Operationen, fünf Gebietsgrößen von 5 bis 200 Kilometern Kantenlänge und
neun Regionen in acht UTM-Zonen.

## Strategien

| Name | Ort der Transformation | Datenquelle des Höhenmodells |
|---|---|---|
| `onthefly` | Backend, während der Ausführung | `load_collection` |
| `local_pp` | lokal, vor dem Batch-Job | `load_stac` |
| `full_pp` | lokal, beide Eingangsraster | `load_stac` |
| `local_reference` | vollständig lokal, ohne Backend | Referenzpipeline |

## Aufbau

    run_benchmark.py       Harness, startet einen vollständigen Messlauf
    openeotest.py          Ausführung der Prozessgraphen über die openEO-API
    backfill_accuracy.py   Genauigkeitsvergleich für vorhandene Läufe nachtragen
    blockzuordnung.py      Zuordnung der Läufe zu den Blöcken des Versuchsplans
    plots.py               Erzeugt alle Abbildungen der Arbeit
    show_credits.py        Abgerechnete Recheneinheiten je Lauf
    scenarios/             Vorlagen der Prozessgraphen
    data/                  STAC-Items für die extern bereitgestellten Raster
    abbildungen/           Erzeugte Abbildungen als PDF

## Installation

    python3 -m venv venv
    source venv/bin/activate
    pip install openeo rasterio numpy duckdb matplotlib pandas

Getestet mit Python 3.12.3, rasterio 1.3.11 auf GDAL 3.9.2 und PROJ 9.4.1,
NumPy 1.26.4, openEO-Client 0.49.0 und DuckDB 1.5.2 unter Ubuntu 24.04 LTS.

Für den Zugriff auf das CDSE ist eine Anmeldung über OIDC nötig. Der openEO-Client
fragt sie beim ersten Aufruf ab und legt das Token unter `~/.config/openeo` ab.

## Einen Messlauf starten

    python3 run_benchmark.py \
      --backend cdse \
      --strategy local_preprocessing \
      --region berlin \
      --extent-size medium \
      --workflow merge_add \
      --local-resampling bilinear

Weitere Parameter steuern Format und Layout des Höhenmodells, die Zielauflösung
und die Wiederholungszahl. `python3 run_benchmark.py --help` zeigt alle Optionen.

## Messdaten

`benchmark_results_public.duckdb` enthält alle ausgewerteten Läufe.

    runs                   Konfiguration, Zeitanteile, Verbrauchskennzahlen je Lauf
    accuracy               Genauigkeitswerte gegen die Referenzpipeline
    band_statistics        Wertebereiche der Ergebnisraster
    latency_measurements   Erreichbarkeit der Backends

Das Zugriffsprotokoll des Bereitstellungsservers mit 3,7 Millionen Zeilen ist aus
Größengründen nicht enthalten. Es belegt den Formatvergleich der Arbeit und liegt
der Datenabgabe an die TU Berlin bei.

Ein Beispiel für eine Abfrage:

    import duckdb
    con = duckdb.connect("benchmark_results_public.duckdb", read_only=True)
    con.execute("""
        SELECT crs_strategy, workflow, median(total_time) AS zeit
        FROM runs
        WHERE archived = false AND status = 'success' AND extent_size = 'medium'
        GROUP BY 1, 2 ORDER BY 2, 1
    """).fetchdf()

## Abbildungen erzeugen

    python3 plots.py tradeoff
    python3 plots.py skalierung
    python3 plots.py genauigkeit

`python3 plots.py` ohne Argument listet alle verfügbaren Abbildungen auf.

## Hinweise

Der Benchmark baut auf dem Framework von Gohil, Bhawiyuga und Girgin auf und erweitert
es um die Transformationsstrategien, die Referenzpipeline und den Genauigkeitsvergleich.

Die Konfigurationsdatei für den Objektspeicher ist nicht Teil des Repositories.
Wer die Bereitstellung nachbauen will, braucht einen eigenen HTTP-Server, der die
transformierten Raster und die zugehörigen STAC-Items ausliefert.

## Lizenz

Der Code steht unter der MIT-Lizenz. Die Messdaten stehen unter CC BY 4.0.

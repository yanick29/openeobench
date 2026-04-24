import sqlite3
import json
import os
from datetime import datetime

DB_PATH = "benchmark_results.db"

def create_database():
    """Erstellt die Datenbank und die Tabellen."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        
        -- Aus results.json
        backend_url TEXT,
        backend_name TEXT,
        scenario TEXT,
        job_id TEXT,
        status TEXT,
        submit_time REAL,
        queue_time REAL,
        processing_time REAL,
        job_execution_time REAL,
        download_time REAL,
        total_time REAL,
        timestamp TEXT,
        error TEXT,
        job_status_history TEXT,
        
        -- Aus job-results.json
        output_crs INTEGER,
        pixel_shape TEXT,
        bounding_box TEXT,
        num_output_files INTEGER,
        backend_version TEXT,
        
        -- Vom Nutzer
        crs_strategy TEXT,
        run_type TEXT,
        preprocessing_time REAL,
        extent_size TEXT,
        
        -- Extra
        credits REAL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS band_statistics (
        stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        filename TEXT,
        band_name TEXT,
        minimum REAL,
        maximum REAL,
        mean REAL,
        stddev REAL,
        valid_percent REAL,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS accuracy (
        accuracy_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        reference_file TEXT,
        rmse REAL,
        max_diff REAL,
        mean_diff REAL,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )''')
    
    conn.commit()
    conn.close()
    print(f"Datenbank erstellt: {DB_PATH}")


def import_run(output_directory, crs_strategy=None, run_type=None, preprocessing_time=None, extent_size=None):
    """Importiert einen Run aus results.json und job-results.json in die DB."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # results.json lesen
    results_path = os.path.join(output_directory, "results.json")
    with open(results_path, 'r') as f:
        results = json.load(f)
    
    # queue_time und processing_time berechnen falls null
    queue_time = results.get("queue_time")
    processing_time = results.get("processing_time")
    
    if queue_time is None and "job_status_history" in results:
        history = results["job_status_history"]
        if "queued" in history and "running" in history:
            queued = datetime.fromisoformat(history["queued"])
            running = datetime.fromisoformat(history["running"])
            queue_time = (running - queued).total_seconds()
        elif "queued" in history and "finished" in history:
            queued = datetime.fromisoformat(history["queued"])
            finished = datetime.fromisoformat(history["finished"])
            queue_time = (finished - queued).total_seconds()
    
    if processing_time is None and "job_status_history" in results:
        history = results["job_status_history"]
        if "running" in history and "finished" in history:
            running = datetime.fromisoformat(history["running"])
            finished = datetime.fromisoformat(history["finished"])
            processing_time = (finished - running).total_seconds()
    
    # job-results.json lesen
    job_results_path = os.path.join(output_directory, "job-results.json")
    output_crs = None
    pixel_shape = None
    bounding_box = None
    num_output_files = 0
    backend_version = None
    band_stats_list = []
    
    if os.path.exists(job_results_path):
        with open(job_results_path, 'r') as f:
            job_results = json.load(f)
        
        assets = job_results.get("assets", {})
        num_output_files = len(assets)
        
        # Erstes Asset fuer CRS und Shape
        for filename, asset in assets.items():
            if output_crs is None:
                output_crs = asset.get("proj:epsg")
                pixel_shape = json.dumps(asset.get("proj:shape"))
                bounding_box = json.dumps(asset.get("proj:bbox"))
            
            # Band-Statistiken sammeln
            for band in asset.get("raster:bands", []):
                stats = band.get("statistics", {})
                band_stats_list.append({
                    "filename": filename,
                    "band_name": band.get("name"),
                    "minimum": stats.get("minimum"),
                    "maximum": stats.get("maximum"),
                    "mean": stats.get("mean"),
                    "stddev": stats.get("stddev"),
                    "valid_percent": stats.get("valid_percent")
                })
        
        # Backend-Version
        for provider in job_results.get("providers", []):
            software = provider.get("processing:software", {})
            if software:
                backend_version = json.dumps(software)
    
    # In die runs-Tabelle einfuegen
    c.execute('''INSERT INTO runs (
        backend_url, backend_name, scenario, job_id, status,
        submit_time, queue_time, processing_time, job_execution_time,
        download_time, total_time, timestamp, error, job_status_history,
        output_crs, pixel_shape, bounding_box, num_output_files, backend_version,
        crs_strategy, run_type, preprocessing_time, extent_size
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
        results.get("backend_url"),
        results.get("backend_name"),
        results.get("process_graph"),
        results.get("job_id"),
        results.get("status"),
        results.get("submit_time"),
        queue_time,
        processing_time,
        results.get("job_execution_time"),
        results.get("download_time"),
        results.get("total_time"),
        results.get("timestamp"),
        results.get("error"),
        json.dumps(results.get("job_status_history")),
        output_crs,
        pixel_shape,
        bounding_box,
        num_output_files,
        backend_version,
        crs_strategy,
        run_type,
        preprocessing_time,
        extent_size
    ))
    
    run_id = c.lastrowid
    
    # Band-Statistiken einfuegen
    for stat in band_stats_list:
        c.execute('''INSERT INTO band_statistics (
            run_id, filename, band_name, minimum, maximum, mean, stddev, valid_percent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', (
            run_id,
            stat["filename"],
            stat["band_name"],
            stat["minimum"],
            stat["maximum"],
            stat["mean"],
            stat["stddev"],
            stat["valid_percent"]
        ))
    
    conn.commit()
    conn.close()
    print(f"Run importiert: {results.get('process_graph')} (run_id={run_id})")
    return run_id


def show_runs():
    """Zeigt alle Runs in der DB."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT run_id, scenario, crs_strategy, status, 
                 queue_time, processing_time, total_time, output_crs, timestamp 
                 FROM runs ORDER BY run_id''')
    
    rows = c.fetchall()
    if not rows:
        print("Keine Runs in der Datenbank.")
        return
    
    print(f"{'ID':<4} {'Szenario':<30} {'CRS-Strategie':<15} {'Status':<8} {'Queue':<8} {'Process':<8} {'Total':<8} {'CRS':<8} {'Zeitpunkt'}")
    print("-" * 120)
    for row in rows:
        run_id, scenario, crs_strategy, status, qt, pt, tt, crs, ts = row
        qt_str = f"{qt:.1f}s" if qt else "-"
        pt_str = f"{pt:.1f}s" if pt else "-"
        tt_str = f"{tt:.1f}s" if tt else "-"
        crs_str = str(crs) if crs else "-"
        crs_strat = crs_strategy or "-"
        print(f"{run_id:<4} {scenario:<30} {crs_strat:<15} {status:<8} {qt_str:<8} {pt_str:<8} {tt_str:<8} {crs_str:<8} {ts[:19]}")
    
    conn.close()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Nutzung:")
        print("  python database.py create                          - Datenbank erstellen")
        print("  python database.py import <ordner> [optionen]      - Run importieren")
        print("  python database.py show                            - Alle Runs anzeigen")
        print("")
        print("Import-Optionen:")
        print("  --strategy baseline|preprocessing|onthefly")
        print("  --run-type cold|hot")
        print("  --preprocessing-time <sekunden>")
        print("  --extent-size <groesse>")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "create":
        create_database()
    
    elif command == "import":
        if len(sys.argv) < 3:
            print("Fehler: Ordner angeben")
            sys.exit(1)
        
        output_dir = sys.argv[2]
        
        # Optionale Parameter parsen
        strategy = None
        run_type = None
        preproc_time = None
        extent = None
        
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--strategy" and i + 1 < len(sys.argv):
                strategy = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--run-type" and i + 1 < len(sys.argv):
                run_type = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--preprocessing-time" and i + 1 < len(sys.argv):
                preproc_time = float(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--extent-size" and i + 1 < len(sys.argv):
                extent = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        
        import_run(output_dir, strategy, run_type, preproc_time, extent)
    
    elif command == "show":
        show_runs()
    
    else:
        print(f"Unbekannter Befehl: {command}")
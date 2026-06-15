import duckdb
import json
import os
import re
import subprocess
from datetime import datetime

DB_PATH = "benchmark_results.duckdb"

# nginx default access log: '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent ...'
_NGINX_LOG_RE = re.compile(
    r'\[(?P<time>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+) [^"]*" '
    r'(?P<status>\d+) (?P<bytes>\d+)'
)

def create_database():
    """Erstellt die Datenbank und die Tabellen."""
    conn = duckdb.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY,
        
        -- Aus results.json
        backend_url TEXT,
        backend_name TEXT,
        scenario TEXT,
        job_id TEXT,
        status TEXT,
        submit_time DOUBLE,
        queue_time DOUBLE,
        processing_time DOUBLE,
        job_execution_time DOUBLE,
        download_time DOUBLE,
        total_time DOUBLE,
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
        preprocessing_time DOUBLE,
        dem_download_time DOUBLE,
        extent_size TEXT,
        
        -- Extra
        credits DOUBLE,
        cpu_seconds DOUBLE,
        duration_backend DOUBLE,
        input_pixels_mp DOUBLE,
        max_memory_gb DOUBLE
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS band_statistics (
        stat_id INTEGER PRIMARY KEY,
        run_id INTEGER,
        filename TEXT,
        band_name TEXT,
        minimum DOUBLE,
        maximum DOUBLE,
        mean DOUBLE,
        stddev DOUBLE,
        valid_percent DOUBLE,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS accuracy (
        accuracy_id INTEGER PRIMARY KEY,
        run_id INTEGER,
        reference_file TEXT,
        rmse DOUBLE,
        max_diff DOUBLE,
        mean_diff DOUBLE,
        mae DOUBLE,
        correlation DOUBLE,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS nginx_access_log (
        log_id INTEGER PRIMARY KEY,
        run_id INTEGER,
        access_timestamp TEXT,
        http_method TEXT,
        http_status INTEGER,
        bytes_sent BIGINT,
        request_path TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )''')

    # Idempotente Migration für bereits existierende DBs
    existing_cols = {r[1] for r in c.execute("PRAGMA table_info('accuracy')").fetchall()}
    for col in ("mae", "correlation"):
        if col not in existing_cols:
            c.execute(f"ALTER TABLE accuracy ADD COLUMN {col} DOUBLE")

    existing_run_cols = {r[1] for r in c.execute("PRAGMA table_info('runs')").fetchall()}
    if "dem_download_time" not in existing_run_cols:
        c.execute("ALTER TABLE runs ADD COLUMN dem_download_time DOUBLE")

    conn.commit()
    conn.close()
    print(f"Datenbank erstellt: {DB_PATH}")


def get_next_id(conn, table, id_column):
    """Holt die naechste freie ID fuer eine Tabelle."""
    result = conn.execute(f"SELECT COALESCE(MAX({id_column}), 0) + 1 FROM {table}").fetchone()
    return result[0]


def import_run(output_directory, crs_strategy=None, run_type=None,
               preprocessing_time=None, extent_size=None, dem_download_time=None):
    """Importiert einen Run aus results.json und job-results.json in die DB."""
    conn = duckdb.connect(DB_PATH)
    
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
        elif "created" in history and "running" in history:
            created = datetime.fromisoformat(history["created"])
            running = datetime.fromisoformat(history["running"])
            queue_time = (running - created).total_seconds()
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

    # Fallback: duration_backend nutzen wenn kein "running" Status kam
    duration_backend = results.get("duration_backend")
    if processing_time is None and duration_backend is not None:
        processing_time = duration_backend
        if queue_time is not None:
            queue_time = max(queue_time - duration_backend, 0)

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
        
        for filename, asset in assets.items():
            if output_crs is None:
                output_crs = asset.get("proj:epsg")
                pixel_shape = json.dumps(asset.get("proj:shape"))
                bounding_box = json.dumps(asset.get("proj:bbox"))
            
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
        
        for provider in job_results.get("providers", []):
            software = provider.get("processing:software", {})
            if software:
                backend_version = json.dumps(software)
    
    run_id = get_next_id(conn, "runs", "run_id")

    cdse_total_time = results.get("total_time")
    total_time = (
        preprocessing_time + cdse_total_time
        if preprocessing_time is not None and cdse_total_time is not None
        else cdse_total_time
    )

    conn.execute('''INSERT INTO runs (
        run_id, backend_url, backend_name, scenario, job_id, status,
        submit_time, queue_time, processing_time, job_execution_time,
        download_time, total_time, timestamp, error, job_status_history,
        output_crs, pixel_shape, bounding_box, num_output_files, backend_version,
        crs_strategy, run_type, preprocessing_time, dem_download_time, extent_size,
        credits, cpu_seconds, duration_backend, input_pixels_mp, max_memory_gb
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
        run_id,
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
        total_time,
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
        dem_download_time,
        extent_size,
        results.get("credits"),
        results.get("cpu_seconds"),
        results.get("duration_backend"),
        results.get("input_pixels_mp"),
        results.get("max_memory_gb")
    ))
    
    for stat in band_stats_list:
        stat_id = get_next_id(conn, "band_statistics", "stat_id")
        conn.execute('''INSERT INTO band_statistics (
            stat_id, run_id, filename, band_name, minimum, maximum, mean, stddev, valid_percent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            stat_id,
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


def _ensure_nginx_access_log_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS nginx_access_log (
        log_id INTEGER PRIMARY KEY,
        run_id INTEGER,
        access_timestamp TEXT,
        http_method TEXT,
        http_status INTEGER,
        bytes_sent BIGINT,
        request_path TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )''')


def import_nginx_access_log(run_id, filenames, ssh_host="root@46.224.62.97",
                            log_path="/var/log/nginx/access.log"):
    """Holt nginx access-log Eintraege per ssh+grep und speichert sie pro run_id.

    filenames: Iterable von Dateinamen die in den nginx Logs gegrept werden
               (z.B. TIF und STAC-Item Dateiname).
    """
    conn = duckdb.connect(DB_PATH)
    _ensure_nginx_access_log_table(conn)

    total = 0
    for fname in filenames:
        if not fname:
            continue
        # Filename darf keine Quotes/Shell-Metas enthalten (kommt aus _ts() + region)
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", ssh_host,
            f"grep '{fname}' {log_path}",
        ]
        print(f"  [ssh] grep '{fname}' {log_path}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        # grep: exit 0 = match, 1 = kein match, >1 = fehler
        if result.returncode > 1:
            print(f"  WARNUNG: ssh-grep fehlgeschlagen ({result.returncode}): {result.stderr.strip()}")
            continue
        if not result.stdout.strip():
            print(f"  Keine Log-Eintraege fuer {fname}")
            continue

        for line in result.stdout.splitlines():
            m = _NGINX_LOG_RE.search(line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("time"), "%d/%b/%Y:%H:%M:%S %z").isoformat()
            except ValueError:
                ts = m.group("time")
            log_id = get_next_id(conn, "nginx_access_log", "log_id")
            conn.execute('''INSERT INTO nginx_access_log
                (log_id, run_id, access_timestamp, http_method, http_status, bytes_sent, request_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)''', (
                log_id, run_id, ts,
                m.group("method"), int(m.group("status")), int(m.group("bytes")),
                m.group("path"),
            ))
            total += 1

    conn.commit()
    conn.close()
    print(f"  Nginx-Logs gespeichert: {total} Eintraege fuer run_id={run_id}")
    return total


def fix_runs():
    """Korrigiert Runs wo processing_time None ist aber duration_backend vorhanden."""
    conn = duckdb.connect(DB_PATH)
    affected = conn.execute('''SELECT run_id, queue_time, duration_backend FROM runs
                               WHERE processing_time IS NULL
                               AND duration_backend IS NOT NULL''').fetchall()
    conn.execute('''UPDATE runs
                    SET processing_time = duration_backend,
                        queue_time = GREATEST(queue_time - duration_backend, 0)
                    WHERE processing_time IS NULL
                    AND duration_backend IS NOT NULL''')
    conn.commit()
    conn.close()
    print(f"Korrigierte Runs: {len(affected)}")
    for run_id, qt, db in affected:
        print(f"  run_id={run_id}: processing_time={db}, queue_time={max((qt or 0) - db, 0):.1f}")


def show_runs():
    """Zeigt alle Runs in der DB."""
    conn = duckdb.connect(DB_PATH)
    result = conn.execute('''SELECT run_id, scenario, crs_strategy, status, 
                 queue_time, processing_time, total_time, output_crs, timestamp 
                 FROM runs ORDER BY run_id''').fetchall()
    
    if not result:
        print("Keine Runs in der Datenbank.")
        conn.close()
        return
    
    print(f"{'ID':<4} {'Szenario':<30} {'CRS-Strategie':<15} {'Status':<8} {'Queue':<8} {'Process':<8} {'Total':<8} {'CRS':<8} {'Zeitpunkt'}")
    print("-" * 120)
    for row in result:
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
        print("  python database.py fix                             - Runs mit fehlendem processing_time korrigieren")
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
        
        strategy = None
        run_type = None
        preproc_time = None
        extent = None
        dem_dl_time = None

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
            elif sys.argv[i] == "--dem-download-time" and i + 1 < len(sys.argv):
                dem_dl_time = float(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--extent-size" and i + 1 < len(sys.argv):
                extent = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        import_run(output_dir, strategy, run_type, preproc_time, extent,
                   dem_download_time=dem_dl_time)
    
    elif command == "show":
        show_runs()

    elif command == "fix":
        fix_runs()
    
    else:
        print(f"Unbekannter Befehl: {command}")
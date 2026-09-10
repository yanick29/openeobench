#!/bin/bash
cd ~/openeobench
source venv/bin/activate
LOG=logs/nacht_referenzen_$(date +%Y%m%d_%H%M%S).log

echo "$(date '+%F %T') Schritt 1: Referenzen neu rechnen" | tee -a "$LOG"
./nacht_referenzen.sh >> "$LOG" 2>&1

echo "$(date '+%F %T') Schritt 2: alte Referenzen archivieren, accuracy leeren" | tee -a "$LOG"
python3 - << 'PYEOF' >> "$LOG" 2>&1
import duckdb
con = duckdb.connect("benchmark_results.duckdb")
con.execute("UPDATE runs SET archived = true WHERE crs_strategy='local_reference' AND run_id < 1311")
n = con.execute("SELECT count(*) FROM accuracy WHERE reference_run_id IN (SELECT run_id FROM runs WHERE crs_strategy='local_reference')").fetchone()[0]
con.execute("DELETE FROM accuracy WHERE reference_run_id IN (SELECT run_id FROM runs WHERE crs_strategy='local_reference')")
print(f"{n} accuracy-Zeilen geloescht")
PYEOF

echo "$(date '+%F %T') Schritt 3: Nachtrag aller Genauigkeitswerte" | tee -a "$LOG"
python3 backfill_accuracy.py --resampling bilinear >> "$LOG" 2>&1

echo "$(date '+%F %T') FERTIG" | tee -a "$LOG"

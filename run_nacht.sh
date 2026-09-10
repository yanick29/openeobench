#!/bin/bash
cd ~/openeobench
source venv/bin/activate

maxid () {
  python3 -c "import duckdb; print(duckdb.connect('benchmark_results.duckdb', read_only=True).execute('SELECT COALESCE(MAX(run_id),0) FROM runs').fetchone()[0])"
}
ref_ok () {
  python3 -c "
import duckdb, sys
c = duckdb.connect('benchmark_results.duckdb', read_only=True)
n = c.execute('SELECT COUNT(*) FROM runs WHERE run_id > ? AND crs_strategy = ? AND status = ?', [$1, 'local_reference', 'success']).fetchone()[0]
sys.exit(0 if n else 1)"
}

lauf () {
  echo "=== $1 / $2 / $3 Referenz ==="
  BEFORE=$(maxid)
  python3 run_benchmark.py --strategy local_reference --region "$1" --extent-size "$2" \
    --workflow "$3" --local-resampling bilinear --min-free-gb 40 $5
  if ! ref_ok "$BEFORE"; then
    echo "!!! Referenz fehlgeschlagen, Messung fuer $1 / $2 / $3 uebersprungen"
    return
  fi
  echo "=== $1 / $2 / $3 Messung ==="
  python3 run_benchmark.py --strategy all --include-full-pp no --region "$1" \
    --extent-size "$2" --workflow "$3" --dem-format gtiff --dem-layout cog \
    --local-resampling bilinear --repeat "$4" --run-type auto --reference-check \
    --min-free-gb 40 $5
}

for REG in wien newyork zuerich; do
  lauf "$REG" medium merge_add 3 ""
done
echo "=== NACHLAUF FERTIG ==="

lauf berlin small  merge_add 5 ""
lauf berlin large  merge_add 5 "--job-timeout 7200"
lauf berlin xlarge merge_add 5 "--job-timeout 7200"
lauf berlin xxlarge merge_add 5 "--force-target-crs --dem-tiles 4 --job-timeout 7200"
echo "=== BLOCK C FERTIG ==="

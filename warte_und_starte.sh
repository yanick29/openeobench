#!/bin/bash
# Wartet auf die Abschlusszeile des G-Blocks und startet danach den
# Terrascope-Operationsvergleich. Die Abschlusszeile ist zuverlaessiger als
# eine Prozesspruefung, weil zwischen zwei Aufrufen innerhalb von G kurz kein
# Python-Prozess laeuft.
cd ~/openeobench
source venv/bin/activate

echo "$(date '+%F %T') warte auf Abschluss von logs/G_netcdf_20260829_201033.log"
while ! grep -q "G netCDF FERTIG" "logs/G_netcdf_20260829_201033.log" 2>/dev/null; do
  sleep 60
done
echo "$(date '+%F %T') G ist fertig, warte 60 s Sicherheitsabstand"
sleep 60

for W in merge_add subtract mask filter_bbox focal resample aggregation; do
  echo "=== ${W} ==="
  python3 run_benchmark.py --backend terrascope --strategy all --include-full-pp no \
    --region berlin --extent-size medium --workflow $W \
    --dem-format gtiff --dem-layout cog --local-resampling bilinear \
    --repeat 3 --run-type auto --reference-check \
    --min-free-gb 40 --job-timeout 2700
done
echo "=== T Operationen FERTIG ==="

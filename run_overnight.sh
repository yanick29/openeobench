#!/bin/bash
# Übernacht-Batch: 4 Regionen x 4 Extents x 6 Workflows
# Strategien: onthefly + local_pp (via --strategy all, full_pp auto-skip bei xlarge)
# Pro Workflow: erst local_reference (Ground Truth), dann all mit --reference-check

source venv/bin/activate

REGIONS="berlin hamburg wien zuerich"
EXTENTS="small medium large xlarge"
WORKFLOWS="merge_add subtract mask aggregation focal filter_bbox"
REPEAT=3

LOG="logs/overnight_$(date +%Y%m%d_%H%M%S).log"
mkdir -p logs

echo "Start: $(date)" | tee -a "$LOG"

for region in $REGIONS; do
  for extent in $EXTENTS; do
    for wf in $WORKFLOWS; do
      echo "" | tee -a "$LOG"
      echo "=== $region | $extent | $wf ===" | tee -a "$LOG"
      
      # 1. Lokale Referenz (Ground Truth)
      python run_benchmark.py --strategy local_reference --repeat 1 \
        --region "$region" --extent-size "$extent" --workflow "$wf" \
        --local-resampling bilinear --include-full-pp no \
        >> "$LOG" 2>&1
      
      # 2. onthefly + local_pp gegen Referenz
      python run_benchmark.py --strategy all --repeat $REPEAT --run-type auto \
        --region "$region" --extent-size "$extent" --workflow "$wf" \
        --reference-check --include-full-pp no \
        >> "$LOG" 2>&1
    done
  done
done

echo "" | tee -a "$LOG"
echo "Fertig: $(date)" | tee -a "$LOG"

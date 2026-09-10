#!/bin/bash
cd ~/openeobench
source venv/bin/activate

echo "=== Referenz 879: berlin medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 886: berlin medium subtract dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow subtract --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 893: berlin medium mask dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow mask --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 900: berlin medium aggregation dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow aggregation --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 907: berlin medium focal dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow focal --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 914: berlin medium resample dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow resample --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 921: berlin medium filter_bbox dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow filter_bbox --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 930: hamburg medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 937: hamburg medium subtract dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow subtract --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 944: hamburg medium mask dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow mask --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 951: hamburg medium aggregation dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow aggregation --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 958: hamburg medium focal dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow focal --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 965: hamburg medium resample dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow resample --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 972: hamburg medium filter_bbox dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region hamburg --extent-size medium --workflow filter_bbox --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 985: amsterdam medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region amsterdam --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1010: kapstadt medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region kapstadt --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1017: tokio medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region tokio --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1024: wien medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region wien --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1031: newyork medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region newyork --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1038: zuerich medium merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region zuerich --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1045: berlin small merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size small --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1062: berlin large merge_add dem cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size large --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1126: berlin medium lc_overlay landcover cdse ==="
python3 run_benchmark.py --backend cdse --strategy local_reference --region berlin --extent-size medium --workflow lc_overlay --dataset landcover --local-resampling nearest --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1204: berlin medium merge_add dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow merge_add --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1205: berlin medium subtract dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow subtract --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1206: berlin medium focal dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow focal --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1207: berlin medium aggregation dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow aggregation --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1208: berlin medium mask dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow mask --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1209: berlin medium filter_bbox dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow filter_bbox --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenz 1210: berlin medium resample dem terrascope ==="
python3 run_benchmark.py --backend terrascope --strategy local_reference --region berlin --extent-size medium --workflow resample --local-resampling bilinear --min-free-gb 40 --job-timeout 2700

echo "=== Referenzen FERTIG ==="
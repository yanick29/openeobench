import re, csv

logs = {
    "A1": "logs/A1_20260819_160728.log",
    "A2": "logs/A2_20260821_150542.log",
    "B":  "logs/B_20260823_163325.log",
}

kopf = re.compile(
    r"Strategie:\s*(\S+)\s*\|\s*Region:\s*(\S+)\s*\|\s*Extent:\s*(\S+)"
    r"\s*\|\s*Workflow:\s*(\S+)\s*\|\s*Run\s*(\d+)/(\d+)\s*\|\s*(\S+)")
ende = re.compile(r"run_id=(\d+)")

zeilen = []
for block, pfad in logs.items():
    aktuell = None
    for zeile in open(pfad, encoding="utf-8", errors="replace"):
        m = kopf.search(zeile)
        if m:
            aktuell = m.groups()
            continue
        m = ende.search(zeile)
        if m and aktuell and "Run importiert" in zeile:
            strat, region, extent, wf, nr, ges, typ = aktuell
            zeilen.append([m.group(1), block, region, strat, wf, extent, nr, typ])
            aktuell = None

with open("blockzuordnung.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["run_id","block","region","crs_strategy","workflow",
                "extent_size","wiederholung","run_type"])
    w.writerows(sorted(zeilen, key=lambda z: int(z[0])))

print(len(zeilen), "Läufe zugeordnet")
for b in logs:
    print(b, sum(1 for z in zeilen if z[1] == b))

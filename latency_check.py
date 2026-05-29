#!/usr/bin/env python3
"""
latency_check.py – Netzwerklatenz und Uhrenoffset zwischen lokalem Server und CDSE

NTP-ähnliches Verfahren (vereinfacht, ein Server-Zeitstempel aus HTTP Date-Header):

  t_send   = lokale Zeit unmittelbar vor dem HTTP-Request          [Unix-Sekunden]
  t_server = CDSE-Zeit aus HTTP Date-Response-Header               [Unix-Sekunden]
  t_recv   = lokale Zeit unmittelbar nach Empfang der Response     [Unix-Sekunden]

  roundtrip = t_recv  - t_send
  latency   = roundtrip / 2                 (symmetrische Laufzeit)
  offset    = t_server - (t_send + latency)
            = t_server - t_send - latency

Interpretation:
  offset > 0  →  CDSE-Uhr geht offset Sekunden VOR der lokalen Uhr
  offset < 0  →  CDSE-Uhr geht offset Sekunden NACH der lokalen Uhr

Korrektur für lokale Zeitstempel (Pre-Processing):
  t_lokal_auf_CDSE-Basis = t_lokal + offset

Hinweis zur Genauigkeit: HTTP Date-Header hat nur 1-Sekunden-Auflösung.
Der „Best-Probe"-Offset (Probe mit minimalstem Roundtrip) minimiert den Fehler.
"""

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import duckdb
import requests

CDSE_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2"
DB_PATH = "benchmark_results.duckdb"
N_PROBES = 20
PROBE_INTERVAL_S = 0.5


def _ensure_latency_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS latency_measurements (
            measurement_id  INTEGER PRIMARY KEY,
            timestamp       TEXT,
            target_url      TEXT,
            n_probes        INTEGER,
            mean_latency_ms DOUBLE,
            median_latency_ms DOUBLE,
            min_latency_ms  DOUBLE,
            max_latency_ms  DOUBLE,
            stddev_latency_ms DOUBLE,
            mean_offset_s   DOUBLE,
            median_offset_s DOUBLE,
            stddev_offset_s DOUBLE,
            best_offset_s   DOUBLE,
            raw_probes      TEXT
        )
    """)


def _probe_once(url: str, verify_ssl: bool = True) -> dict | None:
    """Einzelne Latenz-Messung. Gibt None zurueck bei Fehler."""
    try:
        t_send = time.time()
        resp = requests.get(url, timeout=10, verify=verify_ssl)
        t_recv = time.time()
    except requests.RequestException as exc:
        print(f"  Anfrage fehlgeschlagen: {exc}")
        return None

    date_str = resp.headers.get("Date")
    if not date_str:
        print("  Kein Date-Header in der Response – Probe übersprungen.")
        return None

    try:
        t_server = parsedate_to_datetime(date_str).timestamp()
    except Exception as exc:
        print(f"  Date-Header nicht parsebar ({exc}) – Probe übersprungen.")
        return None

    roundtrip = t_recv - t_send
    latency = roundtrip / 2
    offset = t_server - (t_send + latency)

    return {
        "t_send": t_send,
        "t_recv": t_recv,
        "t_server": t_server,
        "roundtrip_ms": roundtrip * 1000,
        "latency_ms": latency * 1000,
        "offset_s": offset,
        "http_status": resp.status_code,
    }


def measure(url: str = CDSE_URL, n: int = N_PROBES, verify_ssl: bool = True) -> dict:
    """Fuehrt n Probes durch und berechnet Statistiken."""
    print(f"\nMesse Latenz zu: {url}")
    print(f"Probes: {n}  (Pause zwischen Probes: {PROBE_INTERVAL_S} s)")
    if not verify_ssl:
        print("WARNUNG: SSL-Verifizierung deaktiviert (nur fuer lokale Tests)")
    print("-" * 62)

    probes: list[dict] = []
    for i in range(n):
        result = _probe_once(url, verify_ssl=verify_ssl)
        if result:
            probes.append(result)
            print(
                f"  #{i+1:2d}/{n}  "
                f"roundtrip={result['roundtrip_ms']:6.1f} ms  "
                f"latency={result['latency_ms']:6.1f} ms  "
                f"offset={result['offset_s']:+.3f} s"
            )
        if i < n - 1:
            time.sleep(PROBE_INTERVAL_S)

    if not probes:
        raise RuntimeError("Alle Probes fehlgeschlagen - keine Ergebnisse.")

    latencies = [p["latency_ms"] for p in probes]
    offsets = [p["offset_s"] for p in probes]
    best = min(probes, key=lambda p: p["latency_ms"])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target_url": url,
        "n_probes": len(probes),
        "mean_latency_ms": statistics.mean(latencies),
        "median_latency_ms": statistics.median(latencies),
        "min_latency_ms": min(latencies),
        "max_latency_ms": max(latencies),
        "stddev_latency_ms": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
        "mean_offset_s": statistics.mean(offsets),
        "median_offset_s": statistics.median(offsets),
        "stddev_offset_s": statistics.stdev(offsets) if len(offsets) > 1 else 0.0,
        "best_offset_s": best["offset_s"],  # Probe mit geringstem Roundtrip
        "raw_probes": probes,
    }


def save_to_db(stats: dict) -> int:
    conn = duckdb.connect(DB_PATH)
    _ensure_latency_table(conn)

    next_id = conn.execute(
        "SELECT COALESCE(MAX(measurement_id), 0) + 1 FROM latency_measurements"
    ).fetchone()[0]

    conn.execute(
        """INSERT INTO latency_measurements VALUES
           (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            next_id,
            stats["timestamp"],
            stats["target_url"],
            stats["n_probes"],
            stats["mean_latency_ms"],
            stats["median_latency_ms"],
            stats["min_latency_ms"],
            stats["max_latency_ms"],
            stats["stddev_latency_ms"],
            stats["mean_offset_s"],
            stats["median_offset_s"],
            stats["stddev_offset_s"],
            stats["best_offset_s"],
            json.dumps(stats["raw_probes"]),
        ],
    )
    conn.commit()
    conn.close()
    return next_id


def print_summary(stats: dict) -> None:
    print("\n" + "=" * 62)
    print("ERGEBNIS")
    print("=" * 62)
    print(f"Probes erfolgreich :  {stats['n_probes']}")
    print(f"Latenz Median      :  {stats['median_latency_ms']:.1f} ms")
    print(
        f"Latenz Mean +/- Std:  "
        f"{stats['mean_latency_ms']:.1f} +/- {stats['stddev_latency_ms']:.1f} ms"
    )
    print(
        f"Latenz Min / Max   :  "
        f"{stats['min_latency_ms']:.1f} / {stats['max_latency_ms']:.1f} ms"
    )
    print()
    print(f"Offset Median      :  {stats['median_offset_s']:+.3f} s")
    print(
        f"Offset Mean +/- Std:  "
        f"{stats['mean_offset_s']:+.3f} +/- {stats['stddev_offset_s']:.3f} s"
    )
    print(f"Offset Best-Probe  :  {stats['best_offset_s']:+.3f} s")
    print()

    off = stats["median_offset_s"]
    stddev = stats["stddev_offset_s"]
    """
    if stddev > 5:
        print(f"WARNUNG: Offset-Stddev={stddev:.1f} s - CDSE-Loadbalancer mit "
              f"unterschiedlichen Uhren oder instabile Verbindung.")
        print(f"  Empfehlung: Mehr Probes (--n 50) und Ergebnisse kritisch pruefen.")
    if abs(off) < 0.001:
        print("-> Uhren sind praktisch synchron (Abweichung < 1 ms).")
    else:
        richtung = "vor" if off > 0 else "nach"
        print(f"-> CDSE-Uhr geht {abs(off):.3f} s {richtung} der lokalen Uhr.")
        print(f"   Zeitkorrektur fuer lokales Pre-Processing:")
        print(f"   t_lokal_korrigiert = t_lokal + ({off:+.3f} s)")

    print()
    if stats["stddev_latency_ms"] > 100:
        print("Hinweis: Hohe Latenzvarianz - Netz instabil.")
    print("Hinweis: HTTP Date-Header hat 1-Sekunden-Aufloesung. Restfehler <= 0.5 s.")
    """
    print("=" * 62)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Latenz und Uhrenoffset zwischen lokalem Server und CDSE messen"
    )
    parser.add_argument("--url", default=CDSE_URL, help="Ziel-URL (Standard: CDSE OpenEO)")
    parser.add_argument("--n", type=int, default=N_PROBES, help="Anzahl Probes (Standard: 20)")
    parser.add_argument(
        "--no-db", action="store_true", help="Ergebnisse nicht in DuckDB speichern"
    )
    parser.add_argument(
        "--no-ssl-verify", action="store_true",
        help="SSL-Verifizierung deaktivieren (nur fuer lokale Tests auf Windows)"
    )
    args = parser.parse_args()

    stats = measure(url=args.url, n=args.n, verify_ssl=not args.no_ssl_verify)
    print_summary(stats)

    if not args.no_db:
        mid = save_to_db(stats)
        print(f"\nIn Datenbank gespeichert: {DB_PATH}  (measurement_id={mid})")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ntp_check.py – Vergleich: NTP-Offset des lokalen Servers vs. HTTP Date-Offset zu CDSE

Aufruf:
  python ntp_check.py --n 20 --pause 0.5
"""

import argparse
import statistics
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import ntplib
import requests

CDSE_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2"
NTP_HOST = "pool.ntp.org"


def measure_ntp_offset() -> float | None:
    try:
        c = ntplib.NTPClient()
        resp = c.request(NTP_HOST, version=3)
        return resp.offset
    except ntplib.NTPException as exc:
        print(f"  NTP-Fehler: {exc}")
        return None


def measure_http_offset() -> dict | None:
    try:
        t5 = time.time()
        resp = requests.get(CDSE_URL, timeout=10)
        t7 = time.time()
    except requests.RequestException as exc:
        print(f"  HTTP-Fehler: {exc}")
        return None

    date_str = resp.headers.get("Date")
    if not date_str:
        print("  Kein Date-Header – übersprungen.")
        return None

    try:
        t_server = parsedate_to_datetime(date_str).replace(tzinfo=timezone.utc).timestamp()
    except Exception as exc:
        print(f"  Date-Header nicht parsebar: {exc} – übersprungen.")
        return None

    latency = (t7 - t5) / 2
    t_estimated = t5 + latency
    offset = t_server - t_estimated

    return {
        "roundtrip_ms": (t7 - t5) * 1000,
        "latency_ms": latency * 1000,
        "offset_s": offset,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="NTP vs. HTTP Date Offset Vergleich")
    parser.add_argument("--n", type=int, default=20, help="Anzahl Wiederholungen (default: 20)")
    parser.add_argument("--pause", type=float, default=0.5, help="Pause zwischen Requests in Sekunden (default: 0.5)")
    args = parser.parse_args()

    ntp_offsets: list[float] = []
    http_offsets: list[float] = []

    print(f"Messe {args.n}x NTP + HTTP Date Offset ...\n")
    print(f"{'#':>3}  {'NTP Offset':>12}  {'HTTP Offset':>12}  {'Latenz':>10}")
    print("-" * 46)

    for i in range(args.n):
        ntp_off = measure_ntp_offset()
        http = measure_http_offset()

        ntp_str = f"{ntp_off:+.3f} s" if ntp_off is not None else "     -"
        http_str = f"{http['offset_s']:+.3f} s" if http else "     -"
        lat_str = f"{http['latency_ms']:.1f} ms" if http else "     -"

        print(f"{i+1:>3}  {ntp_str:>12}  {http_str:>12}  {lat_str:>10}")

        if ntp_off is not None:
            ntp_offsets.append(ntp_off)
        if http:
            http_offsets.append(http["offset_s"])

        if i < args.n - 1:
            time.sleep(args.pause)

    print("\n" + "=" * 62)
    print("ERGEBNIS")
    print("=" * 62)

    if ntp_offsets:
        print(f"\nNTP-Offset lokaler Server (gegen {NTP_HOST}):")
        print(f"  Median : {statistics.median(ntp_offsets):+.4f} s")
        print(f"  Mean   : {statistics.mean(ntp_offsets):+.4f} s")
        print(f"  Min    : {min(ntp_offsets):+.4f} s")
        print(f"  Max    : {max(ntp_offsets):+.4f} s")
    else:
        print("\nNTP: Keine Messungen erfolgreich.")

    if http_offsets:
        stdev = statistics.stdev(http_offsets) if len(http_offsets) > 1 else 0.0
        median_off = statistics.median(http_offsets)
        print(f"\nHTTP Date-Offset zu CDSE ({CDSE_URL}):")
        print(f"  Median : {median_off:+.4f} s")
        print(f"  Mean   : {statistics.mean(http_offsets):+.4f} s")
        print(f"  Min    : {min(http_offsets):+.4f} s")
        print(f"  Max    : {max(http_offsets):+.4f} s")
        print(f"  Stdev  : {stdev:.4f} s")


    else:
        print("\nHTTP: Keine Messungen erfolgreich.")

    print("=" * 62)



if __name__ == "__main__":
    main()

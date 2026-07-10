#!/usr/bin/env python3
"""
test_streaming_download.py

Standalone-Tests fuer den robusten Streaming-Downloader in openeotest.py.
Ein lokaler HTTP-Server serviert eine echte GeoTIFF-Datei (rasterio-lesbar)
und imitiert das CDSE-Fehlerbild:
  - Content-Length + Accept-Ranges korrekt
  - Vor dem letzten Byte wird die Verbindung abgebrochen -> Client muss
    per Range-Header nachladen.

Getestet wird:
  1. Volltreffer: eine intakte Datei -> ein Versuch, Groesse passt,
     rasterio-Check gruen.
  2. Truncation + Range-Resume: erster GET liefert zu wenige Bytes,
     zweiter GET (mit Range-Header) vervollstaendigt die Datei -> nach
     max. 5 Versuchen liegt die Datei komplett und korrekt vor.
  3. Permanenter Fehler: Server liefert dauerhaft zu wenig -> RuntimeError
     nach 5 Versuchen mit klarer Fehlermeldung.

Der Test benutzt requests.Session direkt gegen 127.0.0.1 - keine
Netzwerkverbindung nach draussen, kein CDSE.
"""
from __future__ import annotations

import http.server
import io
import random
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.transform import from_bounds

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from openeotest import (  # noqa: E402
    _streaming_download_asset,
    _validate_downloaded_tifs,
)


# --------------------------------------------------------------------------
# Test-Server: serviert eine feste Datei ueber HTTP mit Range-Support und
# optionaler "truncate" Simulation.
# --------------------------------------------------------------------------

class _TruncatingHandler(http.server.BaseHTTPRequestHandler):
    """Serviert self.server.payload mit korrekten Range-Headern.

    Wenn self.server.mode == 'truncate_first_n':
      - Nur beim ERSTEN GET wird die Response nach truncate_bytes Bytes
        abgebrochen (Verbindung geschlossen), so als waere die Leitung
        weg. Danach normales Verhalten -> Range-Resume komplettiert.

    Wenn self.server.mode == 'always_truncate':
      - Jeder GET wird truncated -> permanenter Fehler, RuntimeError
        nach 5 Versuchen.

    Wenn self.server.mode == 'ok':
      - Vollstaendige Auslieferung.
    """

    # Kein Logging in stderr
    def log_message(self, *_args, **_kwargs):  # noqa: D401
        pass

    def do_HEAD(self):
        payload = self.server.payload
        self.send_response(200)
        self.send_header("Content-Type", "image/tiff")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        payload = self.server.payload
        total = len(payload)

        # Range-Header parsen
        rng = self.headers.get("Range")
        start, end = 0, total - 1
        is_range = False
        if rng and rng.startswith("bytes="):
            try:
                spec = rng[len("bytes="):]
                s, e = spec.split("-", 1)
                start = int(s) if s else 0
                end = int(e) if e else total - 1
                is_range = True
            except ValueError:
                pass

        body = payload[start:end + 1]

        if is_range:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        else:
            self.send_response(200)

        self.send_header("Content-Type", "image/tiff")
        # WICHTIG: wir senden Content-Length so wie er sein SOLLTE,
        # auch wenn wir gleich weniger Bytes schreiben. Dadurch merkt
        # der Client den Mismatch.
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        # Mode-Behandlung: entscheidet ob wir truncaten.
        truncate = False
        if self.server.mode == "always_truncate":
            truncate = True
        elif self.server.mode == "truncate_first_n":
            # Nur der ERSTE GET (nicht HEAD) wird truncated.
            with self.server.lock:
                self.server.get_count += 1
                if self.server.get_count == 1:
                    truncate = True

        if truncate:
            cutoff = max(1, int(len(body) * self.server.truncate_ratio))
            try:
                self.wfile.write(body[:cutoff])
                # Verbindung hart schliessen -> client sieht incomplete read.
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                # Kein sauberes Ende - Socket abrupt schliessen.
                self.close_connection = True
                self.wfile.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.wfile.write(body)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_test_tif(path: Path) -> bytes:
    """Kleines aber echtes GeoTIFF (rasterio-lesbar) - Payload fuer Server."""
    width, height = 300, 300
    data = np.arange(width * height, dtype=np.int16).reshape(height, width) % 32000
    transform = from_bounds(13.30, 52.45, 13.45, 52.55, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "int16",
        "count": 1,
        "width": width,
        "height": height,
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -32768,
        "tiled": True,
        "blockxsize": 128,
        "blockysize": 128,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path.read_bytes()


class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_server(payload: bytes, mode: str, truncate_ratio: float = 0.6):
    port = _pick_free_port()
    server = _Srv(("127.0.0.1", port), _TruncatingHandler)
    server.payload = payload
    server.mode = mode
    server.truncate_ratio = truncate_ratio
    server.get_count = 0
    server.lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def _run_test(name, payload, mode, tmp, truncate_ratio=0.6,
              expect_success=True):
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    server, port, thread = _start_server(payload, mode, truncate_ratio)
    session = requests.Session()
    target = tmp / f"downloaded_{mode}.tif"
    url = f"http://127.0.0.1:{port}/asset.tif"

    try:
        try:
            stats = _streaming_download_asset(
                url, target, session,
                max_attempts=5,
                connect_timeout=5, read_timeout=15,
                chunk_size=32 * 1024,
                backoff_base_s=0.2,
            )
            if not expect_success:
                raise AssertionError(
                    f"Erwartete RuntimeError, aber Download war erfolgreich: {stats}"
                )
            print(f"  stats: {stats}")
            print(f"  server GETs: {server.get_count}")
            assert target.exists(), "Zieldatei fehlt"
            assert not target.with_suffix(".tif.part").exists(), \
                ".part-Datei sollte nach Erfolg weg sein"
            assert target.stat().st_size == len(payload), \
                f"Groesse {target.stat().st_size} != erwartet {len(payload)}"

            # Content-Byte-Identitaet
            assert target.read_bytes() == payload, \
                "Byte-Inhalt der Datei != Server-Payload"

            # Rasterio-Check
            _validate_downloaded_tifs(str(tmp))

            return stats

        except RuntimeError as exc:
            if expect_success:
                raise
            print(f"  Erwarteter RuntimeError: {exc}")
            # .part sollte auch nach Fehler aufgeraeumt sein
            assert not target.with_suffix(".tif.part").exists(), \
                ".part-Datei sollte nach Fehler-Abbruch weg sein"
            return None
    finally:
        server.shutdown()
        server.server_close()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="streamdl_test_"))
    print(f"[test] tmp dir: {tmp}")

    src_tif = tmp / "source.tif"
    payload = _make_test_tif(src_tif)
    print(f"[test] Payload: {len(payload):,} Bytes")

    try:
        # Test 1: unbeschaedigt
        stats1 = _run_test(
            "TEST 1: sauberer Download, Content-Length OK, kein Resume",
            payload, mode="ok", tmp=tmp,
        )
        assert stats1["attempts"] == 1, f"Erwartete 1 Versuch, tat {stats1['attempts']}"
        assert stats1["used_resume"] is False
        assert stats1["actual_size"] == len(payload)
        assert stats1["expected_size"] == len(payload)
        print("[test1] OK: 1 Versuch, kein Resume, Groesse passt")

        # Zwischen den Tests aufraeumen
        for p in tmp.glob("downloaded_*.tif"):
            p.unlink()

        # Test 2: erster GET wird truncated, zweiter (mit Range-Header)
        # vervollstaendigt die Datei.
        stats2 = _run_test(
            "TEST 2: mid-stream abort -> Range-Resume komplettiert",
            payload, mode="truncate_first_n", tmp=tmp,
            truncate_ratio=0.5,
        )
        assert stats2["attempts"] >= 2, \
            f"Erwartete >=2 Versuche, tat {stats2['attempts']}"
        assert stats2["used_resume"] is True, "Resume wurde nicht genutzt"
        assert stats2["actual_size"] == len(payload)
        assert stats2["expected_size"] == len(payload)
        print(f"[test2] OK: {stats2['attempts']} Versuche, Resume genutzt, "
              f"Datei komplett + rasterio-lesbar")

        for p in tmp.glob("downloaded_*.tif"):
            p.unlink()

        # Test 3: permanenter Fehler -> RuntimeError nach 5 Versuchen
        _run_test(
            "TEST 3: dauerhafte Truncation -> RuntimeError nach 5 Versuchen",
            payload, mode="always_truncate", tmp=tmp,
            truncate_ratio=0.4,
            expect_success=False,
        )
        print("[test3] OK: RuntimeError korrekt geworfen, keine .part-Leichen")

        print("\n" + "=" * 60)
        print("ALLE 3 TESTS OK - Streaming-Downloader ist robust:")
        print("  - Content-Length wird verifiziert")
        print("  - Range-Resume komplettiert abgebrochene Downloads")
        print("  - .part-Rename ist atomar, .part-Leichen werden aufgeraeumt")
        print("  - Permanent-Fehler wirft RuntimeError mit Fehlerdetails")
        print("=" * 60)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

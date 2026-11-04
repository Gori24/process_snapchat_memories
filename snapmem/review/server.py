"""Lokale Review-Oberfläche (nur 127.0.0.1) zum Entscheiden von Konflikten.

* Standort-Konflikt: mehrere JSON-Einträge mit gleicher Sekunde, verschiedene Orte -> Kandidat,
  eigener Punkt auf der Karte oder "kein Standort".
* Fit-Konflikt: Overlay-Seitenverhältnis passt nicht zum Medium -> Variante A-D wählen.
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

from snapmem import export_reader as er
from snapmem.catalog import CONFLICT_FIT, CONFLICT_LOCATION, Catalog, Memory
from snapmem.config import FIT_LABELS, FIT_MODES, Settings
from snapmem.decisions import Decisions
from snapmem.media.images import composite_image, encode_jpeg
from snapmem.media.overlay import load_overlay
from snapmem.media.videos import extract_frame

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
CONTEXT_WINDOW_S = 6 * 3600
CONTEXT_MAX = 4


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")


class ReviewApp:
    def __init__(self, settings: Settings, cat: Catalog):
        self.s = settings
        self.cat = cat
        self.decisions = Decisions.load(settings.decisions_path)
        self.items = [m for m in cat.active() if m.pending_conflicts or m.resolved_conflicts]
        self.by_id = {m.id: m for m in self.items}
        self._cache: dict[tuple, bytes] = {}
        self._lock = threading.Lock()
        # zeitlich sortierte Memories mit GPS als Kontext für die Karte
        self.gps_timeline = sorted((m for m in cat.active() if m.lat is not None and not m.pending_conflicts),
                                   key=lambda m: m.utc_ts)

    # ------------------------------------------------ Daten
    def context_points(self, m: Memory) -> list[dict]:
        pts = [x for x in self.gps_timeline if abs(x.utc_ts - m.utc_ts) <= CONTEXT_WINDOW_S and x.id != m.id]
        pts.sort(key=lambda x: abs(x.utc_ts - m.utc_ts))
        out = []
        for x in pts[:CONTEXT_MAX * 2]:
            dt = x.utc_ts - m.utc_ts
            out.append({"id": x.id, "lat": x.lat, "lon": x.lon, "kind": x.kind,
                        "delta_min": round(dt / 60), "time": _utc(x.utc_ts)})
        return out

    def state(self) -> dict:
        items = []
        for m in self.items:
            d = self.decisions.get(m.id)
            types = sorted(set(m.pending_conflicts + m.resolved_conflicts))
            items.append({
                "id": m.id, "kind": m.kind, "types": types, "utc": _utc(m.utc_ts), "date": m.date,
                "main_size": m.main_size, "overlay_size": m.overlay_size, "aspect_dev": m.aspect_dev,
                "has_overlay": m.has_overlay, "overlay_format": m.overlay_format,
                "candidates": m.location_candidates if CONFLICT_LOCATION in types else [],
                "context": self.context_points(m) if CONFLICT_LOCATION in types else [],
                "decision": {k: v for k, v in d.items() if k in ("location", "fit")},
                "notes": m.notes,
            })
        return {"items": items, "fit_modes": [{"id": f, "label": FIT_LABELS[f]} for f in FIT_MODES]}

    def decide(self, payload: dict) -> dict:
        mid = payload.get("id")
        if mid not in self.by_id:
            raise ValueError(f"Unbekannte ID {mid}")
        m = self.by_id[mid]
        if "location" in payload:
            if CONFLICT_LOCATION not in m.pending_conflicts + m.resolved_conflicts:
                raise ValueError("Kein Standort-Konflikt")
            loc = payload["location"]
            if loc is not None and not loc.get("none"):
                lat, lon = float(loc["lat"]), float(loc["lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Koordinaten außerhalb des gültigen Bereichs")
                loc = {"lat": round(lat, 7), "lon": round(lon, 7), "label": str(loc.get("label", ""))[:80]}
            self.decisions.set(mid, "location", loc)
        if "fit" in payload:
            if CONFLICT_FIT not in m.pending_conflicts + m.resolved_conflicts:
                raise ValueError("Kein Fit-Konflikt")
            fit = payload["fit"]
            if fit is not None and fit not in FIT_MODES:
                raise ValueError(f"Ungültiger Fit {fit}")
            self.decisions.set(mid, "fit", fit)
        log.info("Entscheidung %s: %s", mid, {k: v for k, v in payload.items() if k != "id"})
        return self.decisions.get(mid)

    # ------------------------------------------------ Medien
    def preview(self, mid: str, fit: str | None) -> bytes:
        m = self.by_id[mid]
        key = (mid, fit)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        main_bytes = er.read_bytes(m.zip_path, m.main_name)
        if m.kind == "image":
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(main_bytes)))
        else:
            tmp = self.s.tmp_dir / f"review_{mid}"
            tmp.mkdir(parents=True, exist_ok=True)
            src = tmp / "v.mp4"
            src.write_bytes(main_bytes)
            extract_frame(src, tmp / "f.png", 0.5)
            im = Image.open(tmp / "f.png")
            im.load()
        if m.has_overlay:
            layers = load_overlay(er.read_bytes(m.zip_path, m.overlay_name))
            im = composite_image(im, layers, fit or m.fit)
        im = im.convert("RGB")
        im.thumbnail((900, 900))
        data = encode_jpeg(im, 85)
        with self._lock:
            self._cache[key] = data
        return data

    def raw_main(self, mid: str) -> tuple[bytes, str]:
        m = self.by_id[mid]
        ctype = {"jpeg": "image/jpeg", "mov": "video/quicktime"}.get(m.main_format, "video/mp4")
        return er.read_bytes(m.zip_path, m.main_name), ctype


def make_handler(app: ReviewApp, server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "snapmem-review"

        def log_message(self, fmt, *args):  # ins Log statt stderr
            log.debug("HTTP %s", fmt % args)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._json(app.state())
                m = re.fullmatch(r"/api/preview/([\w-]+)\.jpg", u.path)
                if m and m.group(1) in app.by_id:
                    fit = parse_qs(u.query).get("fit", [None])[0]
                    if fit is not None and fit not in FIT_MODES:
                        return self._json({"error": "ungültiger fit"}, 400)
                    return self._send(200, app.preview(m.group(1), fit), "image/jpeg")
                m = re.fullmatch(r"/api/media/([\w-]+)", u.path)
                if m and m.group(1) in app.by_id:
                    data, ctype = app.raw_main(m.group(1))
                    return self._send_range(data, ctype)
                return self._json({"error": "nicht gefunden"}, 404)
            except Exception as exc:
                log.exception("Fehler bei GET %s", self.path)
                return self._json({"error": str(exc)}, 500)

        def _send_range(self, data: bytes, ctype: str):
            rng = self.headers.get("Range")
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
            if not m:
                return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            if start > end:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{len(data)}"})
            return self._send(206, data[start:end + 1], ctype,
                              {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(data)}"})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if u.path == "/api/decision":
                    return self._json({"ok": True, "decision": app.decide(payload)})
                if u.path == "/api/shutdown":
                    self._json({"ok": True})
                    threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
                    return
                return self._json({"error": "nicht gefunden"}, 404)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:
                log.exception("Fehler bei POST %s", self.path)
                return self._json({"error": str(exc)}, 500)

    return Handler


def serve(settings: Settings, cat: Catalog, port: int = 8765, open_browser: bool = True) -> int:
    app = ReviewApp(settings, cat)
    if not app.items:
        log.info("Keine Konflikte vorhanden – nichts zu entscheiden.")
        return 0
    ref: dict = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, ref))
    ref["server"] = httpd
    url = f"http://127.0.0.1:{port}/"
    pending = sum(1 for m in app.items if m.pending_conflicts)
    log.info("Review-Oberfläche: %s  (%d Konflikt-Memories, davon %d offen). Beenden: Button 'Fertig' oder Strg+C.",
             url, len(app.items), pending)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    log.info("Review beendet. Entscheidungen: %s – weiter mit `python -m snapmem process`/`run`.",
             settings.decisions_path)
    return 0

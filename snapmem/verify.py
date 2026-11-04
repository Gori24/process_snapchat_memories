"""Prüft die Ausgabedateien: Vollständigkeit und zurückgelesene Metadaten (Zeit, Offset, GPS)."""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from snapmem.catalog import Catalog, Memory
from snapmem.config import Settings
from snapmem.metadata import ExifToolPool, read_metadata

log = logging.getLogger(__name__)
GPS_TOL = 1e-5


def expected(m: Memory) -> dict:
    local = datetime.fromtimestamp(m.capture_ts, ZoneInfo(m.tz_name))
    utc = datetime.fromtimestamp(m.capture_ts, timezone.utc)
    off = local.strftime("%z")
    return {"local": local.strftime("%Y:%m:%d %H:%M:%S"), "offset": f"{off[:3]}:{off[3:]}",
            "utc": utc.strftime("%Y:%m:%d %H:%M:%S")}


def check_one(m: Memory, md: dict) -> list[str]:
    e = expected(m)
    probs = []

    def eq(tag, want):
        got = md.get(tag)
        if str(got) != want:
            probs.append(f"{tag}: erwartet {want!r}, gefunden {got!r}")

    if m.kind == "image":
        eq("ExifIFD:DateTimeOriginal", e["local"])
        eq("ExifIFD:OffsetTimeOriginal", e["offset"])
    else:
        eq("Keys:CreationDate", e["local"] + e["offset"])
        eq("QuickTime:CreateDate", e["utc"])
        eq("Track1:MediaCreateDate", e["utc"])
    lat, lon = md.get("Composite:GPSLatitude"), md.get("Composite:GPSLongitude")
    if m.lat is None:
        if lat is not None:
            probs.append(f"GPS vorhanden, aber nicht erwartet ({lat},{lon})")
    else:
        if lat is None or lon is None:
            probs.append("GPS fehlt")
        elif abs(float(lat) - m.lat) > GPS_TOL or abs(float(lon) - m.lon) > GPS_TOL:
            probs.append(f"GPS {lat},{lon} != {m.lat},{m.lon}")
    return probs


def verify(s: Settings, cat: Catalog, limit: int = 0, batch: int = 100) -> int:
    active = [m for m in cat.active() if not m.pending_conflicts]
    present = [m for m in active if (s.output_dir / m.output_name).exists()]
    missing = [m for m in active if m not in present]
    expected_names = {m.output_name.lower() for m in active}
    extra = [p.name for p in s.output_dir.iterdir() if p.is_file() and p.name.lower() not in expected_names]
    todo = present[:limit] if limit else present
    log.info("Verify: %d Dateien im Output, %d erwartet aber fehlend, %d unbekannte Dateien; prüfe %d",
             len(present), len(missing), len(extra), len(todo))

    pool = ExifToolPool(s.exiftool)
    problems: list[tuple[Memory, list[str]]] = []
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            mds = read_metadata(pool, [s.output_dir / m.output_name for m in chunk])
            by_name = {Path(d.get("SourceFile", "")).name.lower(): d for d in mds}
            for m in chunk:
                md = by_name.get(m.output_name.lower(), {})
                p = check_one(m, md) if md else ["keine Metadaten lesbar"]
                if p:
                    problems.append((m, p))
            if len(todo) > batch:
                log.info("  ... %d/%d geprüft", min(i + batch, len(todo)), len(todo))
    finally:
        pool.close()

    rep = s.reports_dir / f"verify_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with rep.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "output", "problem"])
        for m, ps in problems:
            for p in ps:
                w.writerow([m.id, m.output_name, p])
        for m in missing:
            w.writerow([m.id, m.output_name, "fehlt im Output"])
        for x in extra:
            w.writerow(["", x, "unbekannte Datei im Output"])
    for m, ps in problems[:20]:
        log.error("Verify %s (%s): %s", m.id, m.output_name, "; ".join(ps))
    log.info("Verify-Ergebnis: %d OK, %d mit Problemen, %d fehlend. Report: %s",
             len(todo) - len(problems), len(problems), len(missing), rep)
    return 0 if not problems else 3

"""CSV-Reports (bewusst NICHT im output-Ordner, damit sie nicht in die Fotobibliothek gelangen)."""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from snapmem.catalog import Catalog

log = logging.getLogger(__name__)


def _utc(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def write_catalog_reports(cat: Catalog, reports_dir: Path) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    p = reports_dir / "catalog.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "status", "duplicate_of", "kind", "main_format", "overlay_format", "main_size",
                    "overlay_size", "aspect_dev", "fit", "fit_source", "snapchat_utc", "video_embedded_utc",
                    "capture_utc", "date_source", "local_time", "tz", "lat", "lon", "location_source",
                    "pending_conflicts", "output_name", "zip", "main_name", "notes"])
        for m in cat.memories:
            w.writerow([m.id, m.status, m.duplicate_of or "", m.kind, m.main_format, m.overlay_format or "",
                        "x".join(map(str, m.main_size)), "x".join(map(str, m.overlay_size or [])),
                        m.aspect_dev, m.fit if m.has_overlay else "", m.fit_source if m.has_overlay else "",
                        _utc(m.utc_ts), _utc(m.video_creation_ts), _utc(m.capture_ts), m.date_source,
                        m.local_iso, m.tz_name, m.lat if m.lat is not None else "",
                        m.lon if m.lon is not None else "", m.location_source, ",".join(m.pending_conflicts),
                        m.output_name, Path(m.zip_path).name, m.main_name, " | ".join(m.notes)])
    paths["catalog"] = p

    p = reports_dir / "json_ohne_datei.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["json_index", "date", "type", "location", "reason"])
        for u in cat.unmatched_json:
            w.writerow([u["index"], u["date"], u["type"], u["location"], u["reason"]])
    paths["unmatched"] = p

    p = reports_dir / "duplikate.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["behalten", "behalten_utc", "uebersprungen", "uebersprungen_utc"])
        ids = cat.by_id()
        for m in cat.memories:
            if m.status == "duplicate":
                k = ids[m.duplicate_of]
                w.writerow([k.id, _utc(k.utc_ts), m.id, _utc(m.utc_ts)])
    paths["duplicates"] = p
    for k, v in paths.items():
        log.debug("Report %s: %s", k, v)
    return paths


def print_stats(cat: Catalog) -> None:
    s = cat.stats
    lines = [
        f"Zips: {s['zips']} | Mediendateien: {s['media_files']} | JSON-Einträge: {s['json_entries']}",
        f"Memories: {s['memories_total']} gesamt, {s['memories_active']} aktiv, "
        f"{s['duplicates_skipped']} Duplikate übersprungen",
        f"  Bilder: {s['images']} | Videos: {s['videos']} | mit Overlay: {s['with_overlay']} "
        f"{s['overlay_formats']}",
        f"  Container: {s['main_formats']}",
        f"  mit GPS: {s['with_gps']} | Datum aus Video übernommen: {s['embedded_video_date']}",
        f"  JSON-Einträge ohne Datei/mehrdeutig: {s['json_unmatched']} | Dateien ohne JSON: {s['files_without_json']}",
        f"  Offene Konflikte: {s['pending_conflicts'] or 'keine'} | gelöst: {s['resolved_conflicts'] or 'keine'}",
    ]
    for line in lines:
        log.info(line)

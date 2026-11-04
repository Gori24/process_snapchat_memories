"""Verarbeitung: Medien zusammenfügen, Metadaten schreiben, in output/ ablegen."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from snapmem import export_reader as er
from snapmem.catalog import (CONFLICT_FIT, CONFLICT_LOCATION, Catalog, Memory, apply_defaults,
                             assign_output_names, assign_time)
from snapmem.config import Settings
from snapmem.media.images import composite_image, encode_jpeg
from snapmem.media.overlay import load_overlay
from snapmem.media.videos import burn_overlay, prepare_overlay_png, probe_duration
from snapmem.metadata import ExifToolPool, set_file_times, write_metadata

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- Auswahl

def sample_scenarios(mems: list[Memory]) -> dict[str, Memory]:
    """Je ein Beispiel pro Szenario – deterministisch, damit Testläufe reproduzierbar sind."""
    rnd = random.Random(42)
    scen = {
        "Bild ohne Overlay": lambda m: m.kind == "image" and not m.has_overlay and m.lat is not None,
        "Bild ohne Overlay, ohne GPS": lambda m: m.kind == "image" and not m.has_overlay and m.lat is None,
        "Bild + PNG-Overlay": lambda m: m.kind == "image" and m.overlay_format == "png",
        "Bild + WebP-Overlay": lambda m: m.kind == "image" and m.overlay_format == "webp",
        "Bild + SCOF (Lens + Text)": lambda m: m.kind == "image" and m.overlay_format == "scof",
        "Bild + Overlay mit 3-6% Seitenabweichung": lambda m: m.kind == "image" and 0.03 < m.aspect_dev <= 0.06,
        "Bild Fit-Konflikt (Letterbox)": lambda m: CONFLICT_FIT in (m.pending_conflicts + m.resolved_conflicts),
        "Bild Standort-Konflikt": lambda m: m.kind == "image" and CONFLICT_LOCATION in (m.pending_conflicts + m.resolved_conflicts),
        "Bild außerhalb Europas (Zeitzone)": lambda m: m.kind == "image" and m.tz_name.startswith(("Africa", "America", "Asia")),
        "Bild mit Duplikaten": lambda m: m.kind == "image" and bool(m.duplicates),
        "Video ohne Overlay (MP4)": lambda m: m.kind == "video" and not m.has_overlay and m.main_format == "mp4",
        "Video ohne Overlay (QuickTime/MOV)": lambda m: m.kind == "video" and not m.has_overlay and m.main_format == "mov",
        "Video + Overlay, gedreht (alt, H.264)": lambda m: m.kind == "video" and m.has_overlay and m.video_rotation in (90, 270) and m.date < "2019-06",
        "Video + Overlay, gedreht (HEVC)": lambda m: m.kind == "video" and m.has_overlay and m.video_rotation in (90, 270) and m.date > "2021",
        "Video + Overlay, ungedreht": lambda m: m.kind == "video" and m.has_overlay and m.video_rotation == 0,
        "Video + WebP-Overlay": lambda m: m.kind == "video" and m.overlay_format == "webp",
        "Video mit eingebettetem Datum": lambda m: m.date_source == "embedded_video",
        "Video außerhalb Europas (Zeitzone)": lambda m: m.kind == "video" and m.tz_name.startswith(("Africa", "America", "Asia")),
        "Video Standort-Konflikt": lambda m: m.kind == "video" and CONFLICT_LOCATION in (m.pending_conflicts + m.resolved_conflicts),
    }
    picked: dict[str, Memory] = {}
    used: set[str] = set()
    ordered = sorted(mems, key=lambda m: m.id)
    for name, pred in scen.items():
        cands = [m for m in ordered if pred(m) and m.id not in used]
        if cands:
            m = rnd.choice(cands)
            picked[name] = m
            used.add(m.id)
        else:
            log.info("Szenario ohne Treffer: %s", name)
    return picked


# ---------------------------------------------------------------- Manifest

class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.done: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("status") == "ok":
                    self.done[r["id"]] = r
                elif r.get("status") == "removed":
                    self.done.pop(r["id"], None)

    def append(self, rec: dict) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if rec.get("status") == "ok":
                self.done[rec["id"]] = rec


# ---------------------------------------------------------------- Einzelverarbeitung

@dataclass
class Result:
    id: str
    status: str
    output: str = ""
    seconds: float = 0.0
    detail: str = ""
    error: str = ""


def process_one(m: Memory, s: Settings, pool: ExifToolPool, video_sem: threading.Semaphore) -> Result:
    t0 = time.monotonic()
    tmp = s.tmp_dir / m.id
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    detail = []
    try:
        main_bytes = er.read_bytes(m.zip_path, m.main_name)
        layers = load_overlay(er.read_bytes(m.zip_path, m.overlay_name)) if m.has_overlay else None
        out = tmp / ("out" + Path(m.output_name).suffix)

        if m.kind == "image":
            if layers is None:
                out.write_bytes(main_bytes)
                detail.append("Bild kopiert (bitgenau)")
            else:
                with Image.open(io.BytesIO(main_bytes)) as im:
                    im = ImageOps.exif_transpose(im)
                    res = composite_image(im, layers, m.fit)
                out.write_bytes(encode_jpeg(res, s.jpeg_quality))
                detail.append(f"Overlay {m.overlay_format} eingebrannt, fit={m.fit}, {res.width}x{res.height}")
        else:
            src = tmp / ("main." + ("mov" if m.main_format == "mov" else "mp4"))
            src.write_bytes(main_bytes)
            del main_bytes
            if layers is None:
                src.replace(out)
                detail.append("Video kopiert (verlustfrei)")
            else:
                ov_png = tmp / "overlay.png"
                display = (m.main_size[0], m.main_size[1])
                canvas = prepare_overlay_png(layers, display, m.fit, ov_png)
                with video_sem:
                    enc = burn_overlay(src, ov_png, out, display, canvas, m.fit, s)
                d_in, d_out = probe_duration(src), probe_duration(out)
                if d_in and d_out and abs(d_in - d_out) > max(0.5, d_in * 0.05):
                    raise RuntimeError(f"Dauer weicht ab: Quelle {d_in:.2f}s, Ergebnis {d_out:.2f}s")
                detail.append(f"Overlay eingebrannt mit {enc}, {canvas[0]}x{canvas[1]}, "
                              f"Dauer {d_in}s -> {d_out}s, fit={m.fit}")

        write_metadata(pool, out, m.kind, m.capture_ts, m.tz_name, m.lat, m.lon)
        set_file_times(out, m.capture_ts)
        final = s.output_dir / m.output_name
        try:
            os.replace(out, final)
        except OSError:
            shutil.move(str(out), str(final))
        return Result(m.id, "ok", m.output_name, time.monotonic() - t0, "; ".join(detail))
    except Exception as exc:
        log.debug("Fehler bei %s", m.id, exc_info=True)
        return Result(m.id, "error", "", time.monotonic() - t0, "; ".join(detail), f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- Gesamtablauf

def process(s: Settings, cat: Catalog, limit: int | None = None, only: list[str] | None = None,
            sample: bool = False, use_defaults: bool = False, force: bool = False, dry_run: bool = False) -> int:
    mems = cat.active()

    if use_defaults:
        changed = [m for m in mems if m.pending_conflicts]
        for m in changed:
            apply_defaults(m)
            assign_time(m, s)
        if changed:
            assign_output_names(cat.memories)
            cat.save(s.catalog_path)
            log.info("%d offene Konflikte mit Standardwerten gelöst (kein GPS / Letterbox)", len(changed))

    labels: dict[str, str] = {}
    if only:
        wanted = {o.lower() for o in only}
        mems = [m for m in mems if m.id.lower() in wanted or m.uuid in wanted]
        missing = wanted - {m.id.lower() for m in mems} - {m.uuid for m in mems}
        if missing:
            log.warning("Nicht gefunden (oder Duplikat): %s", ", ".join(sorted(missing)))
    elif sample:
        picked = sample_scenarios(mems)
        labels = {m.id: name for name, m in picked.items()}
        mems = list(picked.values())
    if limit:
        mems = mems[:limit]

    pending = [m for m in mems if m.pending_conflicts]
    if pending:
        log.warning("%d Memories mit offenen Konflikten werden übersprungen (review oder --use-defaults): %s",
                    len(pending), ", ".join(m.id for m in pending[:10]) + (" ..." if len(pending) > 10 else ""))
    mems = [m for m in mems if not m.pending_conflicts]

    manifest = Manifest(s.manifest_path)
    todo: list[Memory] = []
    skipped = 0
    for m in mems:
        prev = manifest.done.get(m.id)
        if prev and not force and prev.get("output") == m.output_name and (s.output_dir / m.output_name).exists():
            skipped += 1
            continue
        if prev and prev.get("output") and prev["output"] != m.output_name:
            old = s.output_dir / prev["output"]
            if old.exists() and not dry_run:
                log.info("%s: Dateiname hat sich geändert (%s -> %s), alte Datei wird entfernt",
                         m.id, prev["output"], m.output_name)
                old.unlink()
                manifest.append({"id": m.id, "status": "removed", "output": prev["output"]})
        todo.append(m)

    log.info("Zu verarbeiten: %d | bereits erledigt (übersprungen): %d", len(todo), skipped)
    if dry_run:
        for m in todo:
            log.info("[dry-run] %s -> %s (%s%s, fit=%s, tz=%s, gps=%s)", m.id, m.output_name, m.kind,
                     "+overlay" if m.has_overlay else "", m.fit, m.tz_name,
                     f"{m.lat},{m.lon}" if m.lat is not None else "-")
        return 0
    if not todo:
        return 0

    pool = ExifToolPool(s.exiftool)
    video_sem = threading.Semaphore(s.video_workers)
    results: list[Result] = []
    t_start = time.monotonic()
    # Videos mit Overlay zuerst einplanen (dauern am längsten)
    todo.sort(key=lambda m: (not (m.kind == "video" and m.has_overlay), m.id))
    try:
        with ThreadPoolExecutor(max_workers=s.workers, thread_name_prefix="w") as ex:
            futs = {ex.submit(process_one, m, s, pool, video_sem): m for m in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                m = futs[fut]
                r = fut.result()
                results.append(r)
                manifest.append({"id": r.id, "status": r.status, "output": r.output, "seconds": round(r.seconds, 2),
                                 "detail": r.detail, "error": r.error, "time": datetime.now().isoformat(timespec="seconds")})
                tag = f" [{labels[m.id]}]" if m.id in labels else ""
                if r.status == "ok":
                    log.info("[%d/%d] OK   %s -> %s (%.1fs) %s%s", i, len(todo), m.id, r.output, r.seconds, r.detail, tag)
                else:
                    log.error("[%d/%d] FEHLER %s: %s%s", i, len(todo), m.id, r.error, tag)
    finally:
        pool.close()

    errors = [r for r in results if r.status != "ok"]
    rep = s.reports_dir / f"process_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with rep.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "szenario", "status", "output", "sekunden", "detail", "fehler"])
        for r in results:
            w.writerow([r.id, labels.get(r.id, ""), r.status, r.output, round(r.seconds, 2), r.detail, r.error])
    log.info("Fertig in %.0fs: %d OK, %d Fehler. Report: %s", time.monotonic() - t_start,
             len(results) - len(errors), len(errors), rep)
    return 0 if not errors else 2

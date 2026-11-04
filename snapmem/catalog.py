"""Katalog aller Memories: Zuordnung Datei <-> JSON, Duplikate, Datum, Ort, Konflikte."""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from snapmem import export_reader as er
from snapmem.config import FIT_LETTERBOX, FIT_MODES, FIT_STRETCH, Settings
from snapmem.decisions import Decisions
from snapmem.media.overlay import aspect_deviation, load_overlay

log = logging.getLogger(__name__)

CONFLICT_LOCATION = "location"
CONFLICT_FIT = "fit"
MIN_PLAUSIBLE_TS = int(datetime(2005, 1, 1, tzinfo=timezone.utc).timestamp())


@dataclass
class Memory:
    id: str
    uuid: str
    date: str
    kind: str                         # image | video
    zip_path: str
    main_name: str
    main_sig: str
    main_format: str                  # jpeg | mp4 | mov
    main_size: list[int]              # Anzeigegröße (nach Rotation)
    utc_ts: int                       # Snapchat-Datum (JSON == Zip-Zeitstempel)
    overlay_name: str | None = None
    overlay_zip: str | None = None     # kann in einem anderen Zip liegen als die Hauptdatei!
    overlay_sig: str | None = None
    overlay_format: str | None = None  # png | webp | scof
    overlay_size: list[int] | None = None
    video_creation_ts: int | None = None
    video_rotation: int = 0

    status: str = "ok"                # ok | duplicate
    duplicate_of: str | None = None
    duplicates: list[str] = field(default_factory=list)

    json_indices: list[int] = field(default_factory=list)
    capture_ts: int = 0
    date_source: str = "json"         # json | embedded_video
    lat: float | None = None
    lon: float | None = None
    location_source: str = "none"     # json | decision | none | dropped_embedded | default_none
    location_candidates: list[list[float] | None] = field(default_factory=list)

    aspect_dev: float = 0.0
    fit: str = FIT_STRETCH
    fit_source: str = "auto"          # auto | decision | default

    pending_conflicts: list[str] = field(default_factory=list)
    resolved_conflicts: list[str] = field(default_factory=list)

    tz_name: str = ""
    local_iso: str = ""               # z. B. 2026-10-25T18:20:34+03:00
    output_name: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_overlay(self) -> bool:
        return self.overlay_name is not None

    def read_overlay_bytes(self) -> bytes:
        return er.read_bytes(self.overlay_zip or self.zip_path, self.overlay_name)

    @property
    def is_active(self) -> bool:
        return self.status == "ok"


@dataclass
class Catalog:
    created: str
    source_json: str
    memories: list[Memory]
    unmatched_json: list[dict]
    other_files: list[str]
    stats: dict

    def active(self) -> list[Memory]:
        return [m for m in self.memories if m.is_active]

    def by_id(self) -> dict[str, Memory]:
        return {m.id: m for m in self.memories}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["memories"] = [Memory(**m) for m in d["memories"]]
        return cls(**d)


# ---------------------------------------------------------------- Probe-Cache

class ProbeCache:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("Probe-Cache %s unlesbar – wird neu aufgebaut", path)

    @staticmethod
    def key(zm: er.ZipMedia) -> str:
        return f"{Path(zm.zip_path).name}|{zm.name}|{zm.sig}"

    def get(self, zm: er.ZipMedia) -> dict | None:
        return self.data.get(self.key(zm))

    def put(self, zm: er.ZipMedia, value: dict) -> None:
        with self.lock:
            self.data[self.key(zm)] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)


def probe(zm: er.ZipMedia, cache: ProbeCache) -> dict:
    cached = cache.get(zm)
    if cached is not None:
        return cached
    if zm.role == "overlay":
        data = er.read_bytes(zm.zip_path, zm.name)
        try:
            layers = load_overlay(data)
            res = {"fmt": layers.fmt, "size": list(layers.size), "has_replacement": layers.replacement is not None}
        except Exception as exc:
            res = {"fmt": "error", "error": str(exc)}
    elif zm.ext == "jpg":
        data = er.read_bytes(zm.zip_path, zm.name)
        fmt = er.sniff_format(data[:32])
        from io import BytesIO
        with Image.open(BytesIO(data)) as im:
            size = list(im.size)
            orientation = im.getexif().get(274, 1)
        if orientation in (5, 6, 7, 8):
            size = size[::-1]
        res = {"fmt": fmt, "size": size, "orientation": orientation}
    else:
        head = er.read_head(zm.zip_path, zm.name)
        fmt = er.sniff_format(head)
        try:
            vp = er.probe_video_in_zip(zm.zip_path, zm.name, zm.size)
            res = {"fmt": vp.container, "size": list(vp.display_size), "creation_ts": vp.creation_ts,
                   "rotation": vp.rotation}
        except Exception as exc:
            res = {"fmt": fmt, "error": str(exc)}
    cache.put(zm, res)
    return res


# ---------------------------------------------------------------- Hilfsfunktionen

def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


_tf = None
_tf_lock = threading.Lock()


def timezone_for(lat: float | None, lon: float | None, default_tz: str) -> str:
    global _tf
    if lat is None or lon is None:
        return default_tz
    with _tf_lock:
        if _tf is None:
            from timezonefinder import TimezoneFinder
            _tf = TimezoneFinder()
        name = _tf.timezone_at(lng=lon, lat=lat)
    return name or default_tz


def local_iso(ts: int, tz_name: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz_name)).isoformat()


def short_uuid(uuid: str) -> str:
    return uuid.replace("-", "")[:8].upper()


# ---------------------------------------------------------------- Aufbau

def build_catalog(settings: Settings, decisions: Decisions | None = None) -> Catalog:
    decisions = decisions or Decisions.load(settings.decisions_path)
    zips = er.list_zips(settings.input_dir)
    log.info("Lese %d Zip-Dateien aus %s", len(zips), settings.input_dir)

    media: list[er.ZipMedia] = []
    other: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(zips))) as ex:
        for (m, o), zp in zip(ex.map(er.scan_zip, zips), zips):
            media.extend(m)
            other.extend(f"{zp.name}:{x}" for x in o)
    log.info("%d Mediendateien gefunden, %d sonstige Dateien", len(media), len(other))

    entries, json_src = er.load_json_entries(zips)
    log.info("%d Einträge in %s", len(entries), json_src)

    # Gruppieren nach (Datum, UUID)
    groups: dict[tuple[str, str], dict[str, list[er.ZipMedia]]] = defaultdict(lambda: defaultdict(list))
    for zm in media:
        groups[(zm.date, zm.uuid)][zm.role].append(zm)

    # Probe (parallel, gecacht)
    cache = ProbeCache(settings.probe_cache_path)
    todo = [zm for zm in media if cache.get(zm) is None]
    if todo:
        log.info("Analysiere %d Dateien (Abmessungen, Formate, Video-Header) – einmalig, danach gecacht ...",
                 len(todo))
        done = 0
        with ThreadPoolExecutor(max_workers=settings.workers) as ex:
            for _ in ex.map(lambda z: probe(z, cache), todo):
                done += 1
                if done % 500 == 0:
                    log.info("  ... %d/%d", done, len(todo))
                    cache.save()
        cache.save()

    by_ts: dict[int, list[er.JsonEntry]] = defaultdict(list)
    for e in entries:
        by_ts[e.utc_ts].append(e)

    memories: list[Memory] = []
    for (date, uuid), roles in sorted(groups.items()):
        mains, ovs = roles.get("main", []), roles.get("overlay", [])
        gid = f"{date}_{uuid}"
        if len(mains) != 1:
            log.error("Gruppe %s hat %d main-Dateien – übersprungen (%s)", gid, len(mains),
                      [z.name for z in mains + ovs])
            continue
        mz = mains[0]
        if len(ovs) > 1:
            log.warning("Gruppe %s hat %d Overlays – verwende das erste", gid, len(ovs))
        oz = ovs[0] if ovs else None
        mp = probe(mz, cache)
        kind = "image" if mz.ext == "jpg" else "video"
        mem = Memory(
            id=gid, uuid=uuid, date=date, kind=kind, zip_path=mz.zip_path, main_name=mz.name,
            main_sig=mz.sig, main_format=mp.get("fmt", "unknown"), main_size=mp.get("size") or [0, 0],
            utc_ts=mz.utc_ts,
        )
        if mp.get("error"):
            mem.notes.append(f"Probe-Fehler main: {mp['error']}")
            log.warning("%s: Probe-Fehler: %s", gid, mp["error"])
        if kind == "video":
            mem.video_creation_ts = mp.get("creation_ts")
            mem.video_rotation = mp.get("rotation", 0)
        if oz is not None:
            op = probe(oz, cache)
            if oz.utc_ts != mz.utc_ts:
                mem.notes.append(f"Overlay-Zeitstempel weicht ab ({oz.utc_ts} vs {mz.utc_ts})")
            if op.get("fmt") == "error":
                mem.notes.append(f"Overlay unlesbar: {op.get('error')} – wird ignoriert")
                log.warning("%s: Overlay unlesbar (%s) – wird ignoriert", gid, op.get("error"))
            else:
                mem.overlay_name, mem.overlay_zip, mem.overlay_sig = oz.name, oz.zip_path, oz.sig
                if oz.zip_path != mz.zip_path:
                    mem.notes.append(f"Overlay liegt in {Path(oz.zip_path).name}")
                mem.overlay_format, mem.overlay_size = op["fmt"], op["size"]
                if op.get("has_replacement"):
                    mem.notes.append("SCOF mit Lens-Ebene: ersetzt das Foto")
        memories.append(mem)

    # ------------------------------------------------ JSON-Zuordnung
    type_of = {"image": "Image", "video": "Video"}
    files_per_ts_type = Counter((m.utc_ts, type_of[m.kind]) for m in memories)
    no_match = 0
    for m in memories:
        cands = [e for e in by_ts.get(m.utc_ts, []) if e.media_type == type_of[m.kind]]
        m.json_indices = [e.index for e in cands]
        if not cands:
            no_match += 1
            m.notes.append("Kein JSON-Eintrag mit gleichem Zeitstempel/Typ")
            log.warning("%s: kein passender JSON-Eintrag", m.id)
            continue
        locs = []
        for e in cands:
            v = None if e.lat is None else [e.lat, e.lon]
            if v not in locs:
                locs.append(v)
        if len(locs) == 1:
            if locs[0] is not None:
                m.lat, m.lon = locs[0]
                m.location_source = "json"
        else:
            m.location_candidates = locs
            m.pending_conflicts.append(CONFLICT_LOCATION)

    unmatched = []
    entries_per_ts_type = Counter((e.utc_ts, e.media_type) for e in entries)
    for e in entries:
        k = (e.utc_ts, e.media_type)
        nf = files_per_ts_type.get(k, 0)
        if nf == 0:
            unmatched.append({"index": e.index, "date": e.date_raw, "type": e.media_type,
                              "location": e.location_raw, "reason": "keine Datei im Export"})
        elif entries_per_ts_type[k] > nf:
            unmatched.append({"index": e.index, "date": e.date_raw, "type": e.media_type,
                              "location": e.location_raw,
                              "reason": f"mehrdeutig: {entries_per_ts_type[k]} Einträge, {nf} Datei(en)"})

    # ------------------------------------------------ Duplikate (identischer Inhalt)
    by_content: dict[tuple, list[Memory]] = defaultdict(list)
    for m in memories:
        by_content[(m.main_sig, m.overlay_sig)].append(m)
    n_dup = 0
    for group in by_content.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (x.utc_ts, x.id))
        keep = group[0]
        for d in group[1:]:
            d.status, d.duplicate_of = "duplicate", keep.id
            d.pending_conflicts = []
            keep.duplicates.append(d.id)
            n_dup += 1
            if keep.lat is None and d.lat is not None:
                keep.notes.append(f"Duplikat {d.id} hätte GPS {d.lat},{d.lon}")

    # ------------------------------------------------ Datum, Fit, Entscheidungen, Zeitzone, Name
    n_embedded = 0
    for m in memories:
        m.capture_ts = m.utc_ts
        if (m.kind == "video" and m.video_creation_ts and m.video_creation_ts > MIN_PLAUSIBLE_TS
                and m.utc_ts - m.video_creation_ts > settings.embedded_date_min_diff_s):
            m.capture_ts = m.video_creation_ts
            m.date_source = "embedded_video"
            if m.lat is not None or m.location_candidates:
                m.notes.append(f"GPS verworfen (stammt vom Speichern): {m.lat},{m.lon} {m.location_candidates or ''}")
            m.lat = m.lon = None
            m.location_source = "dropped_embedded"
            if CONFLICT_LOCATION in m.pending_conflicts:
                m.pending_conflicts.remove(CONFLICT_LOCATION)
            if m.is_active:
                n_embedded += 1

        if m.has_overlay and m.overlay_size and m.main_size[0]:
            m.aspect_dev = round(aspect_deviation(tuple(m.main_size), tuple(m.overlay_size)), 4)
            if m.aspect_dev > settings.ar_tolerance and m.is_active:
                m.fit = FIT_LETTERBOX
                m.pending_conflicts.append(CONFLICT_FIT)

        apply_decisions(m, decisions)
        assign_time(m, settings)

    assign_output_names(memories)

    active = [m for m in memories if m.is_active]
    stats = {
        "zips": len(zips),
        "media_files": len(media),
        "memories_total": len(memories),
        "memories_active": len(active),
        "duplicates_skipped": n_dup,
        "images": sum(m.kind == "image" for m in active),
        "videos": sum(m.kind == "video" for m in active),
        "with_overlay": sum(m.has_overlay for m in active),
        "overlay_formats": dict(Counter(m.overlay_format for m in active if m.has_overlay)),
        "main_formats": dict(Counter(m.main_format for m in active)),
        "with_gps": sum(m.lat is not None for m in active),
        "embedded_video_date": n_embedded,
        "json_entries": len(entries),
        "json_unmatched": len(unmatched),
        "files_without_json": no_match,
        "pending_conflicts": dict(Counter(c for m in active for c in m.pending_conflicts)),
        "resolved_conflicts": dict(Counter(c for m in active for c in m.resolved_conflicts)),
    }
    cat = Catalog(datetime.now().isoformat(timespec="seconds"), json_src, memories, unmatched, other, stats)
    return cat


def apply_decisions(m: Memory, decisions: Decisions) -> None:
    if not m.is_active:
        return
    d = decisions.get(m.id)
    if CONFLICT_LOCATION in m.pending_conflicts and d.get("location"):
        loc = d["location"]
        if loc.get("none"):
            m.lat = m.lon = None
            m.location_source = "decision"
        else:
            m.lat, m.lon = float(loc["lat"]), float(loc["lon"])
            m.location_source = "decision"
        m.pending_conflicts.remove(CONFLICT_LOCATION)
        m.resolved_conflicts.append(CONFLICT_LOCATION)
    if CONFLICT_FIT in m.pending_conflicts and d.get("fit") in FIT_MODES:
        m.fit = d["fit"]
        m.fit_source = "decision"
        m.pending_conflicts.remove(CONFLICT_FIT)
        m.resolved_conflicts.append(CONFLICT_FIT)


def apply_defaults(m: Memory) -> None:
    """Für offene Konflikte Standardwerte setzen (nur mit --use-defaults)."""
    if CONFLICT_LOCATION in m.pending_conflicts:
        m.lat = m.lon = None
        m.location_source = "default_none"
        m.notes.append("Standort-Konflikt ohne Entscheidung: kein GPS geschrieben")
    if CONFLICT_FIT in m.pending_conflicts:
        m.fit = FIT_LETTERBOX
        m.fit_source = "default"
    m.pending_conflicts = []


def assign_time(m: Memory, settings: Settings) -> None:
    m.tz_name = timezone_for(m.lat, m.lon, settings.default_tz)
    m.local_iso = local_iso(m.capture_ts, m.tz_name)


def output_ext(m: Memory) -> str:
    if m.kind == "image":
        return ".jpg"
    if m.has_overlay:
        return ".mp4"
    return ".mov" if m.main_format == "mov" else ".mp4"


def assign_output_names(memories: list[Memory]) -> None:
    used: set[str] = set()
    for m in sorted(memories, key=lambda x: (x.capture_ts, x.id)):
        if not m.is_active:
            m.output_name = ""
            continue
        stamp = m.local_iso[:19].replace("T", "_").replace(":", "-")
        name = f"{stamp}_{short_uuid(m.uuid)}{output_ext(m)}"
        if name.lower() in used:
            name = f"{stamp}_{m.uuid.replace('-', '').upper()}{output_ext(m)}"
        used.add(name.lower())
        m.output_name = name

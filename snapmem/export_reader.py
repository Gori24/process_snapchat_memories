"""Lesen des Snapchat-Exports direkt aus den Zip-Dateien.

Erkenntnisse aus der Analyse des Exports:
* Mediendateien liegen als ``memories/<YYYY-MM-DD>_<UUID>-main.(jpg|mp4)`` und optional
  ``...-overlay.png`` in beliebig vielen Zips (``mydata~<id>.zip``, ``mydata~<id>-2.zip`` ...).
* ``json/memories_history.json`` enthält Datum (UTC), Typ und Ort – aber KEINE Datei-Referenz.
* Jeder Zip-Eintrag hat ein "extended timestamp"-Feld (0x5455) mit dem exakten UTC-Zeitpunkt,
  der sekundengenau dem JSON-Datum entspricht. Darüber erfolgt die Zuordnung.
* ``overlay.png`` ist oft WebP, selten ein Snapchat-Container "SCOF" mit eingebetteten PNG-Ebenen.
* ``main.mp4`` ist teilweise ein QuickTime-Container (ftyp "qt  ").
"""
from __future__ import annotations

import io
import json
import logging
import re
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MEDIA_RE = re.compile(r"^memories/(\d{4}-\d{2}-\d{2})_([0-9A-Fa-f-]{36})-(main|overlay)\.(\w+)$")
JSON_NAME = "json/memories_history.json"
LOC_RE = re.compile(r"Latitude, Longitude:\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)")
MP4_EPOCH_OFFSET = 2082844800  # Sekunden zwischen 1904-01-01 und 1970-01-01


@dataclass
class ZipMedia:
    zip_path: str
    name: str
    date: str
    uuid: str
    role: str          # main | overlay
    ext: str           # Dateiendung laut Name
    size: int
    crc: int
    utc_ts: int

    @property
    def sig(self) -> str:
        return f"{self.size}:{self.crc:08x}"


@dataclass
class JsonEntry:
    index: int
    utc_ts: int
    date_raw: str
    media_type: str            # Image | Video
    location_raw: str
    lat: float | None
    lon: float | None
    extra: dict = field(default_factory=dict)


def _ut_timestamp(info: zipfile.ZipInfo) -> int | None:
    ex = info.extra
    p = 0
    while p + 4 <= len(ex):
        hid, ln = struct.unpack("<HH", ex[p:p + 4])
        if hid == 0x5455 and ln >= 5 and ex[p + 4] & 1:
            return struct.unpack("<I", ex[p + 5:p + 9])[0]
        p += 4 + ln
    return None


def _dos_timestamp_utc(info: zipfile.ZipInfo) -> int:
    # Fallback (nicht beobachtet): DOS-Zeit ist lokale Zeit des Export-Servers – ungenau.
    return int(datetime(*info.date_time).replace(tzinfo=timezone.utc).timestamp())


def list_zips(input_dir: Path) -> list[Path]:
    zips = sorted(p for p in input_dir.glob("*.zip") if p.is_file())
    if not zips:
        raise FileNotFoundError(f"Keine .zip-Dateien in {input_dir}")
    return zips


def scan_zip(zip_path: Path) -> tuple[list[ZipMedia], list[str]]:
    media: list[ZipMedia] = []
    other: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            m = MEDIA_RE.match(info.filename)
            if not m:
                other.append(info.filename)
                continue
            ts = _ut_timestamp(info)
            if ts is None:
                log.warning("Kein UTC-Zeitstempel in %s:%s – nutze DOS-Zeit", zip_path.name, info.filename)
                ts = _dos_timestamp_utc(info)
            d, uuid, role, ext = m.groups()
            media.append(ZipMedia(str(zip_path), info.filename, d, uuid.lower(), role, ext.lower(),
                                  info.file_size, info.CRC, ts))
    return media, other


def load_json_entries(zips: list[Path]) -> tuple[list[JsonEntry], str]:
    for zp in zips:
        with zipfile.ZipFile(zp) as zf:
            if JSON_NAME in zf.namelist():
                data = json.loads(zf.read(JSON_NAME).decode("utf-8"))
                return parse_json_entries(data), f"{zp.name}:{JSON_NAME}"
    raise FileNotFoundError(f"{JSON_NAME} in keiner Zip-Datei gefunden")


def parse_json_entries(data: dict) -> list[JsonEntry]:
    raw = data.get("Saved Media")
    if raw is None:
        raise ValueError("JSON enthält keinen Schlüssel 'Saved Media'")
    out: list[JsonEntry] = []
    for i, e in enumerate(raw):
        dt = datetime.strptime(e["Date"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        loc = e.get("Location", "") or ""
        lat = lon = None
        m = LOC_RE.search(loc)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if lat == 0.0 and lon == 0.0:
                lat = lon = None
        elif loc:
            log.warning("Unbekanntes Location-Format in JSON-Eintrag %d: %r", i, loc)
        extra = {k: v for k, v in e.items() if k not in ("Date", "Media Type", "Location") and v}
        out.append(JsonEntry(i, int(dt.timestamp()), e["Date"], e.get("Media Type", ""), loc, lat, lon, extra))
    return out


# ---------------------------------------------------------------- Format-Erkennung

def sniff_format(head: bytes) -> str:
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"SCOF":
        return "scof"
    if head[4:8] == b"ftyp":
        brand = head[8:12].decode("latin1")
        return "mov" if brand == "qt  " else "mp4"
    return "unknown"


def read_head(zip_path: str, name: str, n: int = 32) -> bytes:
    with zipfile.ZipFile(zip_path) as zf, zf.open(name) as fh:
        return fh.read(n)


def read_bytes(zip_path: str, name: str) -> bytes:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(name)


# ---------------------------------------------------------------- MP4/MOV-Header

@dataclass
class VideoProbe:
    container: str            # mp4 | mov
    creation_ts: int | None   # mvhd creation_time (UTC, Unix)
    width: int                # Breite laut tkhd (vor Rotation)
    height: int
    rotation: int             # 0, 90, 180, 270 (aus Matrix)

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height


def _matrix_rotation(a: int, b: int, c: int, d: int) -> int:
    # Matrix [a b; c d] in 16.16 – nur die Vorzeichen sind relevant
    if a == 0 and d == 0:
        return 90 if b > 0 else 270
    if a < 0 and d < 0:
        return 180
    return 0


def probe_video_stream(fh, total_size: int) -> VideoProbe:
    """Parst ftyp/moov sequentiell aus einem (nur vorwärts lesbaren) Stream."""
    container = "mp4"
    creation = None
    tracks: list[tuple[int, int, int]] = []
    pos = 0

    def read_exact(n: int) -> bytes:
        b = fh.read(n)
        if len(b) != n:
            raise EOFError("Unerwartetes Dateiende")
        return b

    def skip(n: int) -> None:
        while n > 0:
            chunk = fh.read(min(n, 1 << 20))
            if not chunk:
                raise EOFError("Unerwartetes Dateiende beim Überspringen")
            n -= len(chunk)

    def parse_moov(buf: bytes) -> None:
        nonlocal creation
        for typ, body in _iter_boxes(buf):
            if typ == b"mvhd":
                v = body[0]
                ct = struct.unpack(">Q", body[4:12])[0] if v == 1 else struct.unpack(">I", body[4:8])[0]
                creation = ct - MP4_EPOCH_OFFSET if ct else None
            elif typ == b"trak":
                for t2, b2 in _iter_boxes(body):
                    if t2 == b"tkhd":
                        v = b2[0]
                        off = 4 + (32 if v == 1 else 20) + 8 + 2 + 2 + 2 + 2  # bis Matrix
                        mat = struct.unpack(">9i", b2[off:off + 36])
                        w, h = struct.unpack(">II", b2[off + 36:off + 44])
                        if w and h:
                            tracks.append((w >> 16, h >> 16, _matrix_rotation(mat[0], mat[1], mat[3], mat[4])))

    found_moov = False
    while pos + 8 <= total_size:
        hdr = read_exact(8)
        size, typ = struct.unpack(">I4s", hdr)
        hl = 8
        if size == 1:
            size = struct.unpack(">Q", read_exact(8))[0]
            hl = 16
        elif size == 0:
            size = total_size - pos
        if size < hl:
            raise ValueError(f"Ungültige Box-Größe {size} bei {pos}")
        if typ == b"ftyp":
            body = read_exact(size - hl)
            container = "mov" if body[:4] == b"qt  " else "mp4"
        elif typ == b"moov":
            parse_moov(read_exact(size - hl))
            found_moov = True
            break
        else:
            skip(size - hl)
        pos += size
    if not found_moov:
        raise ValueError("Keine moov-Box gefunden")
    if not tracks:
        raise ValueError("Keine Videospur mit Abmessungen gefunden")
    w, h, rot = tracks[0]
    return VideoProbe(container, creation, w, h, rot)


def _iter_boxes(buf: bytes):
    p = 0
    n = len(buf)
    while p + 8 <= n:
        size, typ = struct.unpack(">I4s", buf[p:p + 8])
        hl = 8
        if size == 1:
            size = struct.unpack(">Q", buf[p + 8:p + 16])[0]
            hl = 16
        elif size == 0:
            size = n - p
        if size < hl:
            return
        yield typ, buf[p + hl:p + size]
        p += size


def probe_video_in_zip(zip_path: str, name: str, size: int) -> VideoProbe:
    with zipfile.ZipFile(zip_path) as zf, zf.open(name) as fh:
        return probe_video_stream(fh, size)


def probe_video_bytes(data: bytes) -> VideoProbe:
    return probe_video_stream(io.BytesIO(data), len(data))

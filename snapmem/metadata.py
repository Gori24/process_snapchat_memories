"""Metadaten (Aufnahmezeit, Zeitzone, GPS) mit ExifTool schreiben – so, wie Apple Fotos/iCloud sie liest.

Bilder (JPEG/EXIF):
  DateTimeOriginal/CreateDate/ModifyDate (lokale Zeit) + OffsetTime* (Zeitzonen-Offset),
  GPSLatitude/Longitude(+Ref), GPSDateStamp/GPSTimeStamp (UTC).
Videos (MP4/MOV):
  QuickTime CreateDate/ModifyDate + Track/Media-Daten (UTC, wie vom Standard gefordert),
  Keys:CreationDate (com.apple.quicktime.creationdate, lokale Zeit mit Offset),
  Keys:GPSCoordinates (com.apple.quicktime.location.ISO6709) und UserData:GPSCoordinates (©xyz).
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import exiftool

from snapmem.tools import find_exiftool

log = logging.getLogger(__name__)


class ExifToolPool:
    """Ein dauerhaft laufender exiftool-Prozess (-stay_open) pro Thread."""

    def __init__(self, executable: str | None = None):
        self.executable = find_exiftool(executable)
        self._local = threading.local()
        self._all: list[exiftool.ExifToolHelper] = []
        self._lock = threading.Lock()

    def get(self) -> exiftool.ExifToolHelper:
        et = getattr(self._local, "et", None)
        if et is None:
            et = exiftool.ExifToolHelper(executable=self.executable, common_args=["-charset", "filename=utf8"])
            et.run()
            self._local.et = et
            with self._lock:
                self._all.append(et)
        return et

    def close(self) -> None:
        with self._lock:
            for et in self._all:
                try:
                    et.terminate()
                except Exception:
                    pass
            self._all.clear()


def _fmt_local(dt: datetime) -> tuple[str, str]:
    off = dt.strftime("%z")
    return dt.strftime("%Y:%m:%d %H:%M:%S"), f"{off[:3]}:{off[3:]}"


def image_args(capture_ts: int, tz_name: str, lat: float | None, lon: float | None) -> list[str]:
    local = datetime.fromtimestamp(capture_ts, ZoneInfo(tz_name))
    utc = datetime.fromtimestamp(capture_ts, timezone.utc)
    ts, off = _fmt_local(local)
    args = [
        f"-EXIF:DateTimeOriginal={ts}", f"-EXIF:CreateDate={ts}", f"-EXIF:ModifyDate={ts}",
        f"-EXIF:OffsetTimeOriginal={off}", f"-EXIF:OffsetTimeDigitized={off}", f"-EXIF:OffsetTime={off}",
        f"-XMP-photoshop:DateCreated={ts}{off}",
    ]
    if lat is not None and lon is not None:
        args += [
            "-EXIF:GPSVersionID=2.3.0.0",
            f"-EXIF:GPSLatitude={abs(lat)}", f"-EXIF:GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-EXIF:GPSLongitude={abs(lon)}", f"-EXIF:GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
            f"-EXIF:GPSDateStamp={utc:%Y:%m:%d}", f"-EXIF:GPSTimeStamp={utc:%H:%M:%S}",
        ]
    return args


def video_args(capture_ts: int, tz_name: str, lat: float | None, lon: float | None) -> list[str]:
    local = datetime.fromtimestamp(capture_ts, ZoneInfo(tz_name))
    utc = datetime.fromtimestamp(capture_ts, timezone.utc)
    ts, off = _fmt_local(local)
    u = f"{utc:%Y:%m:%d %H:%M:%S}"
    args = ["-api", "QuickTimeUTC=1"]
    for tag in ("CreateDate", "ModifyDate", "TrackCreateDate", "TrackModifyDate", "MediaCreateDate",
                "MediaModifyDate"):
        args.append(f"-QuickTime:{tag}={u}+00:00")
    args.append(f"-Keys:CreationDate={ts}{off}")
    if lat is not None and lon is not None:
        coords = f"{lat:.6f} {lon:.6f}"
        args += [f"-Keys:GPSCoordinates={coords}", f"-UserData:GPSCoordinates={coords}"]
    return args


def write_metadata(pool: ExifToolPool, path: Path, kind: str, capture_ts: int, tz_name: str,
                   lat: float | None, lon: float | None) -> None:
    args = (image_args if kind == "image" else video_args)(capture_ts, tz_name, lat, lon)
    et = pool.get()
    out = et.execute("-overwrite_original", "-m", *args, str(path))
    log.debug("exiftool %s: %s", path.name, (out or "").strip().replace("\n", " | "))
    if "1 image files updated" not in (out or "") and "1 video files updated" not in (out or ""):
        err = et.last_stderr if hasattr(et, "last_stderr") else ""
        raise RuntimeError(f"exiftool hat {path.name} nicht aktualisiert: {out!r} {err!r}")


def read_metadata(pool: ExifToolPool, paths: list[Path]) -> list[dict]:
    """Liest Metadaten roh (QuickTime-Zeiten bleiben UTC, Zahlen numerisch)."""
    et = pool.get()
    return et.execute_json("-G1", "-n", *[str(p) for p in paths])


# ---------------------------------------------------------------- Dateizeiten

def set_file_times(path: Path, ts: int) -> None:
    os.utime(path, (ts, ts))
    if os.name != "nt":
        return
    # Windows: auch das Erstelldatum setzen
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        ft = int((ts + 11644473600) * 10_000_000)
        handle = k32.CreateFileW(str(path), 0x0100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
        if handle is None or handle == ctypes.c_void_p(-1).value:
            log.debug("CreateFileW fehlgeschlagen für %s (Fehler %s)", path, ctypes.get_last_error())
            return
        try:
            c = ctypes.c_ulonglong(ft)
            k32.SetFileTime(wintypes.HANDLE(handle), ctypes.byref(c), ctypes.byref(c), ctypes.byref(c))
        finally:
            k32.CloseHandle(wintypes.HANDLE(handle))
    except Exception as exc:  # pragma: no cover
        log.debug("Konnte Erstelldatum nicht setzen (%s): %s", path, exc)

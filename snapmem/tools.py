"""Auffinden und Prüfen der externen Programme (ffmpeg, exiftool)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Unter Windows keine Konsolenfenster für Subprozesse öffnen
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ToolMissing(RuntimeError):
    pass


@lru_cache(maxsize=None)
def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise ToolMissing("ffmpeg nicht gefunden (pip install imageio-ffmpeg)") from exc


def find_exiftool(explicit: str | None = None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("SNAPMEM_EXIFTOOL"):
        candidates.append(os.environ["SNAPMEM_EXIFTOOL"])
    for name in ("exiftool", "ExifTool", "exiftool.exe"):
        w = shutil.which(name)
        if w:
            candidates.append(w)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Programs" / "ExifTool" / "ExifTool.exe"))
    candidates.append(r"C:\Program Files\ExifTool\ExifTool.exe")
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c))
    raise ToolMissing(
        "exiftool nicht gefunden. Installation: `winget install OliverBetz.ExifTool` "
        "oder Pfad per --exiftool / Umgebungsvariable SNAPMEM_EXIFTOOL angeben."
    )


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    log.debug("RUN %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


@lru_cache(maxsize=None)
def encoder_works(encoder: str) -> bool:
    """Testet, ob ein ffmpeg-Encoder (z. B. hevc_nvenc) auf diesem Rechner tatsächlich läuft."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.2",
           "-c:v", encoder, "-f", "null", "-"]
    r = run(cmd, timeout=60)
    ok = r.returncode == 0
    log.debug("Encoder-Test %s: %s %s", encoder, "OK" if ok else "FEHLER", r.stderr.strip()[-300:])
    return ok


def environment_report(exiftool_hint: str | None = None) -> dict[str, str]:
    rep: dict[str, str] = {}
    try:
        ff = ffmpeg_exe()
        v = run([ff, "-version"]).stdout.splitlines()[0]
        rep["ffmpeg"] = f"{ff} ({v})"
    except Exception as exc:
        rep["ffmpeg"] = f"FEHLT: {exc}"
    try:
        et = find_exiftool(exiftool_hint)
        rep["exiftool"] = f"{et} (Version {run([et, '-ver']).stdout.strip()})"
    except Exception as exc:
        rep["exiftool"] = f"FEHLT: {exc}"
    for enc in ("hevc_nvenc", "libx265"):
        try:
            rep[f"encoder {enc}"] = "verfügbar" if encoder_works(enc) else "nicht verfügbar"
        except Exception as exc:
            rep[f"encoder {enc}"] = f"Fehler: {exc}"
    return rep

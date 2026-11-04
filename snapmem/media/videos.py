"""Videos: Overlay einbrennen (ffmpeg) und Hilfsfunktionen."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image

from snapmem.config import FIT_LETTERBOX, Settings
from snapmem.media.images import letterbox_canvas, place_layer
from snapmem.media.overlay import OverlayLayers
from snapmem.tools import encoder_works, ffmpeg_exe, run

log = logging.getLogger(__name__)

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class VideoError(RuntimeError):
    pass


def probe_duration(path: Path) -> float | None:
    r = run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    m = DURATION_RE.search(r.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def probe_streams(path: Path) -> str:
    """Kurzbeschreibung der Streams (für Logs/Tests)."""
    r = run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    return "\n".join(l.strip() for l in r.stderr.splitlines() if "Stream #" in l or "rotation" in l.lower())


def prepare_overlay_png(layers: OverlayLayers, display: tuple[int, int], fit: str, out_png: Path) -> tuple[int, int]:
    """Flacht alle Overlay-Ebenen auf eine RGBA-PNG in Zielgröße ab. Gibt die Leinwandgröße zurück."""
    if layers.replacement is not None:
        log.warning("Overlay mit Lens-Ersatzebene bei Video – Ersatzebene wird ignoriert")
    canvas = letterbox_canvas(display, layers.size) if fit == FIT_LETTERBOX else display
    flat = Image.new("RGBA", canvas, (0, 0, 0, 0))
    for layer in layers.overlays:
        flat.alpha_composite(place_layer(layer, canvas, fit))
    flat.save(out_png, "PNG")
    return canvas


def _encoder_args(settings: Settings, encoder: str) -> list[str]:
    if encoder == "hevc_nvenc":
        return ["-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(settings.nvenc_cq),
                "-b:v", "0", "-profile:v", "main", "-tag:v", "hvc1"]
    if encoder == "libx265":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", str(settings.x265_crf), "-tag:v", "hvc1",
                "-x265-params", "log-level=error"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(settings.nvenc_cq),
                "-b:v", "0"]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    raise VideoError(f"Unbekannter Encoder {encoder}")


def burn_overlay(main: Path, overlay_png: Path, out: Path, display: tuple[int, int], canvas: tuple[int, int],
                 fit: str, settings: Settings) -> str:
    """Brennt das (bereits skalierte) Overlay ins Video. Gibt den verwendeten Encoder zurück."""
    if fit == FIT_LETTERBOX and canvas != display:
        vf = (f"[0:v]pad={canvas[0]}:{canvas[1]}:(ow-iw)/2:(oh-ih)/2:color=black[b];"
              f"[b][1:v]overlay=0:0:format=auto,format=yuv420p[v]")
    else:
        # ffmpeg dreht das Video automatisch (Display-Matrix) -> Frames haben Anzeigegröße
        vf = f"[0:v]scale={display[0]}:{display[1]}[b];[b][1:v]overlay=0:0:format=auto,format=yuv420p[v]"

    encoders = [settings.video_encoder]
    for fb in ("libx265",):
        if fb not in encoders:
            encoders.append(fb)
    last_err = ""
    for enc in encoders:
        if enc.endswith("_nvenc") and not encoder_works(enc):
            log.debug("Encoder %s nicht verfügbar – überspringe", enc)
            continue
        cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-v", "error",
               "-i", str(main), "-i", str(overlay_png),
               "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
               *_encoder_args(settings, enc),
               "-fps_mode", "passthrough", "-c:a", "copy",
               "-map_metadata", "0", "-movflags", "+faststart", str(out)]
        r = run(cmd, timeout=3600)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            if r.stderr.strip():
                log.debug("ffmpeg-Meldungen (%s): %s", enc, r.stderr.strip()[-1000:])
            return enc
        last_err = r.stderr.strip()[-2000:]
        log.warning("ffmpeg mit %s fehlgeschlagen (rc=%s): %s", enc, r.returncode, last_err[-500:])
        out.unlink(missing_ok=True)
    raise VideoError(f"Video-Encoding fehlgeschlagen: {last_err}")


def extract_frame(video: Path, out_png: Path, t: float = 0.5) -> None:
    for ts in (t, 0.0):
        r = run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", str(ts), "-i", str(video),
                 "-frames:v", "1", str(out_png)])
        if r.returncode == 0 and out_png.exists():
            return
    raise VideoError(f"Konnte kein Standbild extrahieren: {r.stderr[-500:]}")

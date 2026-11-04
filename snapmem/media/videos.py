"""Videos: Overlay einbrennen (ffmpeg) und Hilfsfunktionen."""
from __future__ import annotations

import logging
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from snapmem.config import FIT_LETTERBOX, Settings
from snapmem.export_reader import _iter_boxes
from snapmem.media.images import letterbox_canvas, place_layer
from snapmem.media.overlay import OverlayLayers
from snapmem.tools import CREATE_NO_WINDOW, encoder_works, ffmpeg_exe, run

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


# ---------------------------------------------------------------- Edit-Listen (z. B. Snapchat-Zeitraffer)
#
# Manche Snapchat-Videos (beobachtet: Zeitraffer) bestehen aus wenigen Bildern über mehrere Minuten
# und einer Edit-Liste mit vielen 1/30-s-Segmenten, die der Player nacheinander abspielt. ffmpeg löst
# solche Listen beim Neukodieren nicht korrekt auf. Dieser Pfad dekodiert die Rohbilder ohne Edit-Liste
# und setzt die Bildfolge exakt nach den Segmenten zusammen.

@dataclass
class EditList:
    movie_timescale: int
    media_timescale: int
    segments: list[tuple[int, int, float]]  # (Dauer in Movie-Timescale, Medienzeit in Media-Timescale, Rate)

    @property
    def duration(self) -> float:
        return sum(s[0] for s in self.segments) / self.movie_timescale


def read_video_editlist(path: Path) -> EditList | None:
    data = path.read_bytes()
    moov = None
    p = 0
    while p + 8 <= len(data):
        size, typ = struct.unpack(">I4s", data[p:p + 8])
        hl = 8
        if size == 1:
            size, hl = struct.unpack(">Q", data[p + 8:p + 16])[0], 16
        elif size == 0:
            size = len(data) - p
        if typ == b"moov":
            moov = data[p + hl:p + size]
            break
        if size < hl:
            break
        p += size
    if moov is None:
        return None
    movie_ts = None
    for t, b in _iter_boxes(moov):
        if t == b"mvhd":
            movie_ts = struct.unpack(">I", b[20:24] if b[0] == 1 else b[12:16])[0]
        if t != b"trak":
            continue
        segs, media_ts, handler = [], None, None
        for t2, b2 in _iter_boxes(b):
            if t2 == b"mdia":
                for t3, b3 in _iter_boxes(b2):
                    if t3 == b"mdhd":
                        media_ts = struct.unpack(">I", b3[20:24] if b3[0] == 1 else b3[12:16])[0]
                    elif t3 == b"hdlr":
                        handler = b3[8:12]
            elif t2 == b"edts":
                for t3, b3 in _iter_boxes(b2):
                    if t3 == b"elst":
                        v, n, q = b3[0], struct.unpack(">I", b3[4:8])[0], 8
                        for _ in range(n):
                            if v == 1:
                                sd, mt = struct.unpack(">Qq", b3[q:q + 16])
                                q += 16
                            else:
                                sd, mt = struct.unpack(">Ii", b3[q:q + 8])
                                q += 8
                            ri, rf = struct.unpack(">hH", b3[q:q + 4])
                            q += 4
                            segs.append((sd, mt, ri + rf / 65536))
        if handler == b"vide" and movie_ts and media_ts:
            return EditList(movie_ts, media_ts, segs) if segs else None
    return None


def is_complex_editlist(el: EditList | None) -> bool:
    return el is not None and sum(1 for s in el.segments if s[1] >= 0) > 2


def _decode_raw_frames(main: Path, display: tuple[int, int]) -> tuple[list[float], bytes]:
    w, h = display
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-ignore_editlist", "1", "-i", str(main),
           "-vf", f"scale={w}:{h},showinfo", "-fps_mode", "passthrough", "-an",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    log.debug("RUN %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=3600)
    if r.returncode != 0:
        raise VideoError(f"Dekodieren ohne Edit-Liste fehlgeschlagen: {r.stderr.decode('utf-8', 'replace')[-500:]}")
    pts = [float(x) for x in re.findall(rb"pts_time:\s*(-?[\d.]+)", r.stderr)]
    frame_size = w * h * 3
    n = len(r.stdout) // frame_size
    if n == 0 or n != len(pts):
        raise VideoError(f"Rohbilder ({n}) und Zeitstempel ({len(pts)}) passen nicht zusammen")
    return pts, r.stdout


def build_frame_sequence(el: EditList, pts: list[float], fps: float) -> list[int]:
    """Für jedes Ausgabebild (fps) den Index des anzuzeigenden Rohbildes bestimmen."""
    order = sorted(range(len(pts)), key=lambda i: pts[i])
    sorted_pts = [pts[i] for i in order]
    seq: list[int] = []
    t_movie = 0.0
    n_out = max(1, round(el.duration * fps))
    seg_bounds = []
    for dur, mt, rate in el.segments:
        d = dur / el.movie_timescale
        seg_bounds.append((t_movie, t_movie + d, mt, rate))
        t_movie += d
    import bisect
    for k in range(n_out):
        t = (k + 0.5) / fps  # Bildmitte, robust gegen Rundung an Segmentgrenzen
        seg = next((s for s in seg_bounds if s[0] <= t < s[1]), seg_bounds[-1])
        start, _end, mt, rate = seg
        if mt < 0:  # leeres Segment -> erstes Bild
            seq.append(order[0])
            continue
        media_t = mt / el.media_timescale + (t - start) * (rate or 1.0)
        j = bisect.bisect_right(sorted_pts, media_t + 1e-6) - 1
        seq.append(order[max(0, j)])
    return seq


def burn_overlay_editlist(main: Path, overlay_png: Path, out: Path, display: tuple[int, int],
                          canvas: tuple[int, int], fit: str, settings: Settings, el: EditList,
                          fps: float = 30.0) -> str:
    pts, raw = _decode_raw_frames(main, display)
    frame_size = display[0] * display[1] * 3
    seq = build_frame_sequence(el, pts, fps)
    log.info("Edit-Liste aufgelöst: %d Segmente, %d Rohbilder -> %d Bilder @ %g fps (%.2fs)",
             len(el.segments), len(pts), len(seq), fps, len(seq) / fps)
    if fit == FIT_LETTERBOX and canvas != display:
        vf = (f"[0:v]pad={canvas[0]}:{canvas[1]}:(ow-iw)/2:(oh-ih)/2:color=black[b];"
              f"[b][1:v]overlay=0:0:format=auto,format=yuv420p[v]")
    else:
        vf = "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    last_err = ""
    for enc in [settings.video_encoder] + (["libx265"] if settings.video_encoder != "libx265" else []):
        if enc.endswith("_nvenc") and not encoder_works(enc):
            continue
        cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{display[0]}x{display[1]}", "-r", str(fps), "-i", "-",
               "-i", str(overlay_png), "-i", str(main),
               "-filter_complex", vf, "-map", "[v]", "-map", "2:a?", "-shortest",
               *_encoder_args(settings, enc), "-c:a", "copy", "-movflags", "+faststart", str(out)]
        log.debug("RUN %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
        try:
            for idx in seq:
                proc.stdin.write(raw[idx * frame_size:(idx + 1) * frame_size])
            proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() == 0 and out.exists():
            return enc + " (Edit-Liste aufgelöst)"
        last_err = err[-1000:]
        log.warning("Edit-Listen-Encoding mit %s fehlgeschlagen: %s", enc, last_err[-300:])
        out.unlink(missing_ok=True)
    raise VideoError(f"Edit-Listen-Encoding fehlgeschlagen: {last_err}")


def extract_frame(video: Path, out_png: Path, t: float = 0.5) -> None:
    for ts in (t, 0.0):
        r = run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", str(ts), "-i", str(video),
                 "-frames:v", "1", str(out_png)])
        if r.returncode == 0 and out_png.exists():
            return
    raise VideoError(f"Konnte kein Standbild extrahieren: {r.stderr[-500:]}")

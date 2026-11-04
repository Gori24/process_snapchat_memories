"""Erzeugt einen kleinen, synthetischen Snapchat-Export (keine echten Daten)."""
from __future__ import annotations

import io
import json
import struct
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from snapmem.tools import ffmpeg_exe

UUID_IMG = "11111111-1111-1111-1111-111111111111"
UUID_IMG_OV = "22222222-2222-2222-2222-222222222222"
UUID_SCOF = "33333333-3333-3333-3333-333333333333"
UUID_VID_OV = "44444444-4444-4444-4444-444444444444"
UUID_VID_OLD = "55555555-5555-5555-5555-555555555555"
UUID_LETTER = "66666666-6666-6666-6666-666666666666"
UUID_AMBIG = "77777777-7777-7777-7777-777777777777"


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def jpeg(size, color) -> bytes:
    im = Image.new("RGB", size, color)
    ImageDraw.Draw(im).rectangle([10, 10, 60, 60], fill=(255, 255, 255))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


def overlay(size, fmt="PNG") -> bytes:
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([0, size[1] // 2 - 20, size[0], size[1] // 2 + 20], fill=(0, 0, 0, 160))
    b = io.BytesIO()
    im.save(b, fmt)
    return b.getvalue()


def scof(text_size, lens_size) -> bytes:
    text = overlay(text_size)
    lens = io.BytesIO()
    Image.new("RGB", lens_size, (255, 200, 0)).save(lens, "PNG")
    body = b"\x00" * 12 + struct.pack("<I", len(text)) + text + b"\x00" * 4 + struct.pack("<I", len(lens.getvalue())) \
        + lens.getvalue() + b"\x00"
    return struct.pack("<I", 20) + b"SCOF" + body


def video(path: Path, size=(320, 576), seconds=1.0, creation: str | None = None, rotate: bool = False) -> bytes:
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size[0]}x{size[1]}:rate=25",
           "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    if rotate:
        rot = path.with_name("rot_" + path.name)
        subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-display_rotation", "90", "-i", str(path), "-c", "copy",
                        str(rot)], check=True)
        return rot.read_bytes()
    return path.read_bytes()


def add(zf: zipfile.ZipFile, name: str, data: bytes, utc: int) -> None:
    zi = zipfile.ZipInfo(name, date_time=datetime.fromtimestamp(utc, timezone.utc).timetuple()[:6])
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.extra = struct.pack("<HHBI", 0x5455, 5, 1, utc)  # extended timestamp (UTC), wie im echten Export
    zf.writestr(zi, data)


@pytest.fixture(scope="session")
def export_dir(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("export")
    inp = base / "input"
    inp.mkdir()
    tmp = base / "_gen"
    tmp.mkdir()

    t_img = ts("2025-07-01 10:00:00")
    t_img_ov = ts("2025-07-02 18:30:00")
    t_dup = ts("2026-01-05 09:00:00")
    t_scof = ts("2018-08-09 21:06:44")
    t_vov = ts("2025-10-25 15:20:34")
    t_vold = ts("2022-12-22 08:42:25")
    t_letter = ts("2025-09-01 12:10:40")
    t_amb = ts("2018-11-01 01:14:01")

    entries = [
        (t_img, "Image", "47.7, 10.3"),
        (t_img_ov, "Image", "48.137154, 11.576124"),
        (t_dup, "Image", "48.137154, 11.576124"),           # Re-Save-Duplikat von UUID_IMG_OV
        (t_scof, "Image", "0.0, 0.0"),
        (t_vov, "Video", "-4.268673, 39.595848"),            # Kenia -> UTC+3
        (t_vold, "Video", "47.7, 10.3"),                     # Kamerarollen-Import
        (t_letter, "Image", "37.50731, 126.959694"),
        (t_amb, "Image", "47.727028, 10.372879"),
        (t_amb, "Image", "47.73539, 10.3784485"),            # gleiche Sekunde, anderer Ort
        (ts("2022-05-05 05:05:05"), "Image", "1.0, 1.0"),    # ohne Datei
    ]
    data = {"Saved Media": [{"Date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                             "Media Type": ty, "Location": f"Latitude, Longitude: {loc}",
                             "Download Link": "", "Media Download Url": ""} for t, ty, loc in entries]}

    img_ov_main = jpeg((360, 640), (40, 90, 160))
    img_ov_ov = overlay((338, 601), "WEBP")
    with zipfile.ZipFile(inp / "mydata~123.zip", "w") as zf:
        zf.writestr("json/memories_history.json", json.dumps(data))
        zf.writestr("index.html", "<html></html>")
        add(zf, f"memories/2025-07-01_{UUID_IMG}-main.jpg", jpeg((360, 640), (200, 50, 50)), t_img)
        add(zf, f"memories/2025-07-02_{UUID_IMG_OV}-main.jpg", img_ov_main, t_img_ov)
        add(zf, f"memories/2025-07-02_{UUID_IMG_OV}-overlay.png", img_ov_ov, t_img_ov)
        add(zf, f"memories/2018-08-09_{UUID_SCOF}-main.jpg", jpeg((300, 534), (10, 10, 10)), t_scof)
    with zipfile.ZipFile(inp / "mydata~123-2.zip", "w") as zf:
        # Overlay liegt in einem anderen Zip als die Hauptdatei (kommt im echten Export vor)
        add(zf, f"memories/2018-08-09_{UUID_SCOF}-overlay.png", scof((300, 534), (288, 512)), t_scof)
        add(zf, f"memories/2026-01-05_{UUID_IMG_OV}-main.jpg", img_ov_main, t_dup)
        add(zf, f"memories/2026-01-05_{UUID_IMG_OV}-overlay.png", img_ov_ov, t_dup)
        add(zf, f"memories/2025-10-25_{UUID_VID_OV}-main.mp4",
            video(tmp / "a.mp4", (576, 320), creation="2025-10-25T15:20:34Z", rotate=True), t_vov)
        add(zf, f"memories/2025-10-25_{UUID_VID_OV}-overlay.png", overlay((338, 601)), t_vov)
        add(zf, f"memories/2022-12-22_{UUID_VID_OLD}-main.mp4",
            video(tmp / "b.mp4", creation="2022-05-14T13:38:56Z"), t_vold)
        add(zf, f"memories/2025-09-01_{UUID_LETTER}-main.jpg", jpeg((480, 640), (0, 120, 0)), t_letter)
        add(zf, f"memories/2025-09-01_{UUID_LETTER}-overlay.png", overlay((338, 601)), t_letter)
        add(zf, f"memories/2018-11-01_{UUID_AMBIG}-main.jpg", jpeg((360, 640), (90, 90, 90)), t_amb)
    return base

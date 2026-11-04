from __future__ import annotations

import io

import pytest
from PIL import Image

from snapmem import export_reader as er
from snapmem.config import FIT_LETTERBOX, FIT_STRETCH
from snapmem.media.images import composite_image, letterbox_canvas
from snapmem.media.overlay import aspect_deviation, load_overlay
from snapmem.metadata import image_args, video_args

from conftest import overlay, scof


def test_parse_json_location_and_zero():
    entries = er.parse_json_entries({"Saved Media": [
        {"Date": "2026-10-30 14:20:13 UTC", "Media Type": "Image", "Location": "Latitude, Longitude: 49.640755, 10.169687"},
        {"Date": "2016-12-27 17:12:24 UTC", "Media Type": "Video", "Location": "Latitude, Longitude: 0.0, 0.0"},
        {"Date": "2026-10-25 15:20:34 UTC", "Media Type": "Image", "Location": "Latitude, Longitude: -4.268673, 39.595848"},
    ]})
    assert (entries[0].lat, entries[0].lon) == (49.640755, 10.169687)
    assert entries[1].lat is None and entries[1].lon is None
    assert entries[2].lat == -4.268673
    assert entries[0].utc_ts == 1793370013


@pytest.mark.parametrize("head,fmt", [
    (b"\xff\xd8\xff\xe0" + b"\0" * 28, "jpeg"),
    (b"\x89PNG\r\n\x1a\n" + b"\0" * 24, "png"),
    (b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 16, "webp"),
    (b"\x14\0\0\0SCOF" + b"\0" * 24, "scof"),
    (b"\0\0\0\x14ftypqt  " + b"\0" * 20, "mov"),
    (b"\0\0\0\x18ftypmp42" + b"\0" * 20, "mp4"),
])
def test_sniff(head, fmt):
    assert er.sniff_format(head) == fmt


def test_scof_layers():
    layers = load_overlay(scof((300, 534), (288, 512)))
    assert layers.fmt == "scof"
    assert layers.replacement is not None and layers.replacement.size == (288, 512)
    assert len(layers.overlays) == 1 and layers.size == (300, 534)
    main = Image.new("RGB", (300, 534), (0, 0, 0))
    out = composite_image(main, layers, FIT_STRETCH)
    # Lens-Ebene (gelb) ersetzt das schwarze Foto
    assert out.getpixel((5, 5)) == (255, 200, 0)


def test_webp_overlay_scaled_to_main():
    layers = load_overlay(overlay((338, 601), "WEBP"))
    assert layers.fmt == "webp"
    out = composite_image(Image.new("RGB", (360, 640), (255, 255, 255)), layers, FIT_STRETCH)
    assert out.size == (360, 640)
    assert out.getpixel((180, 320)) != (255, 255, 255)  # Balken in der Mitte eingebrannt
    assert out.getpixel((180, 10)) == (255, 255, 255)


def test_letterbox():
    assert letterbox_canvas((1242, 1656), (1179, 2097)) == (1242, 2210)
    layers = load_overlay(overlay((338, 601)))
    out = composite_image(Image.new("RGB", (480, 640), (0, 120, 0)), layers, FIT_LETTERBOX)
    assert abs(out.width / out.height - 338 / 601) < 0.01
    assert out.getpixel((out.width // 2, 2)) == (0, 0, 0)  # schwarzer Rand oben
    assert aspect_deviation((1242, 1656), (1179, 2097)) > 0.3


def test_editlist_timelapse_sequence():
    from snapmem.media.videos import EditList, build_frame_sequence, is_complex_editlist
    # Zeitraffer wie im echten Export: Rohbilder alle 1.2 s, Segmente zu je 1/30 s auf steigende Medienzeiten
    pts = [i * 1.2 for i in range(10)]
    el = EditList(600, 600, [(20, 700, 1.0), (20, 1420, 1.0), (40, 2140, 1.0), (20, 5000, 1.0)])
    assert is_complex_editlist(el) and abs(el.duration - 100 / 600) < 1e-9
    seq = build_frame_sequence(el, pts, 30)
    # 700/600=1.17s -> Bild 0 (0.0s); 1420/600=2.37s -> Bild 1 (1.2s);
    # Segment 3 (2 Frames) spielt ab 3.567s: 3.583s -> Bild 2 (2.4s), 3.617s -> Bild 3 (3.6s); 8.33s -> Bild 6 (7.2s)
    assert seq == [0, 1, 2, 3, 6]
    assert not is_complex_editlist(EditList(600, 600, [(800, 0, 1.0)]))


def test_metadata_args_timezone_and_gps():
    a = image_args(1793370013, "Europe/Berlin", -4.268673, 39.595848)
    assert "-EXIF:DateTimeOriginal=2026:10:30 15:20:13" in a
    assert "-EXIF:OffsetTimeOriginal=+01:00" in a
    assert "-EXIF:GPSLatitudeRef=S" in a and "-EXIF:GPSLatitude=4.268673" in a
    s = image_args(1782000000, "Europe/Berlin", None, None)  # Sommerzeit
    assert any(x.endswith("+02:00") for x in s) and not any("GPS" in x for x in s)
    v = video_args(1793370013, "Africa/Nairobi", -4.268673, 39.595848)
    assert "-Keys:CreationDate=2026:10:30 17:20:13+03:00" in v
    assert "-QuickTime:CreateDate=2026:10:30 14:20:13+00:00" in v
    assert "-Keys:GPSCoordinates=-4.268673 39.595848" in v

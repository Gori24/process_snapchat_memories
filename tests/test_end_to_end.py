"""End-to-End mit synthetischem Export: Katalog -> Entscheidungen -> Verarbeitung -> Verify."""
from __future__ import annotations

import json

import pytest

from snapmem.catalog import CONFLICT_FIT, CONFLICT_LOCATION, build_catalog
from snapmem.config import Settings
from snapmem.pipeline import process
from snapmem.tools import ToolMissing, encoder_works, find_exiftool
from snapmem.verify import verify

from conftest import UUID_AMBIG, UUID_IMG, UUID_IMG_OV, UUID_LETTER, UUID_SCOF, UUID_VID_OLD, UUID_VID_OV

try:
    find_exiftool()
    HAVE_EXIFTOOL = True
except ToolMissing:
    HAVE_EXIFTOOL = False


@pytest.fixture()
def settings(export_dir, tmp_path):
    s = Settings.from_base(tmp_path, input_dir=export_dir / "input", workers=2, video_workers=1)
    if not encoder_works("hevc_nvenc"):
        s.video_encoder = "libx265"
    s.ensure_dirs()
    return s


def mem(cat, uuid):
    return next(m for m in cat.memories if m.uuid == uuid and m.status == "ok")


def test_catalog(settings):
    cat = build_catalog(settings)
    st = cat.stats
    assert st["memories_total"] == 8 and st["memories_active"] == 7 and st["duplicates_skipped"] == 1
    assert st["files_without_json"] == 0

    dup = next(m for m in cat.memories if m.status == "duplicate")
    assert dup.date == "2026-01-05" and dup.duplicate_of == mem(cat, UUID_IMG_OV).id

    v = mem(cat, UUID_VID_OV)
    assert v.video_rotation in (90, 270) and v.main_size == [320, 576]
    assert v.tz_name == "Africa/Nairobi" and v.local_iso.endswith("+03:00")
    assert v.output_name == "2025-10-25_18-20-34_44444444.mp4"

    old = mem(cat, UUID_VID_OLD)
    assert old.date_source == "embedded_video" and old.lat is None
    assert old.output_name.startswith("2022-05-14_15-38-56")  # Sommerzeit Berlin

    assert mem(cat, UUID_SCOF).overlay_format == "scof" and mem(cat, UUID_SCOF).lat is None
    assert CONFLICT_FIT in mem(cat, UUID_LETTER).pending_conflicts
    amb = mem(cat, UUID_AMBIG)
    assert CONFLICT_LOCATION in amb.pending_conflicts and len(amb.location_candidates) == 2
    reasons = {u["reason"].split(":")[0] for u in cat.unmatched_json}
    assert reasons == {"keine Datei im Export", "mehrdeutig"}


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool nicht installiert")
def test_process_and_verify(settings):
    cat = build_catalog(settings)
    # ohne Entscheidungen: Konflikte werden übersprungen
    assert process(settings, cat) == 0
    names = {p.name for p in settings.output_dir.iterdir()}
    assert len(names) == 5
    assert verify(settings, cat) == 0

    # Entscheidungen treffen -> Rest wird verarbeitet, erledigte werden übersprungen
    settings.decisions_path.write_text(json.dumps({
        mem(cat, UUID_AMBIG).id: {"location": {"lat": 47.73539, "lon": 10.3784485}},
        mem(cat, UUID_LETTER).id: {"fit": "letterbox"},
    }), encoding="utf-8")
    cat = build_catalog(settings)
    assert cat.stats["pending_conflicts"] == {}
    assert process(settings, cat) == 0
    assert len(list(settings.output_dir.iterdir())) == 7
    assert verify(settings, cat) == 0

    from PIL import Image
    with Image.open(settings.output_dir / mem(cat, UUID_LETTER).output_name) as im:
        assert abs(im.width / im.height - 338 / 601) < 0.01
    with Image.open(settings.output_dir / mem(cat, UUID_IMG).output_name) as im:
        assert im.size == (360, 640)

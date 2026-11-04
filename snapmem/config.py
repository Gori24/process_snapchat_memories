"""Zentrale Einstellungen und Pfade."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Fit-Modi für Overlays, deren Seitenverhältnis nicht zum Medium passt
FIT_STRETCH = "stretch"      # A: Overlay auf Mediengröße skalieren
FIT_CONTAIN = "contain"      # B: Overlay zentriert einpassen
FIT_COVER = "cover"          # C: Overlay füllend, Ränder abgeschnitten
FIT_LETTERBOX = "letterbox"  # D: Medium auf Overlay-Leinwand mit schwarzen Rändern
FIT_MODES = (FIT_STRETCH, FIT_CONTAIN, FIT_COVER, FIT_LETTERBOX)
FIT_LABELS = {
    FIT_STRETCH: "A – Overlay strecken",
    FIT_CONTAIN: "B – Overlay einpassen",
    FIT_COVER: "C – Overlay füllen",
    FIT_LETTERBOX: "D – Letterbox (wie Snapchat)",
}


@dataclass
class Settings:
    base_dir: Path
    input_dir: Path
    output_dir: Path
    work_dir: Path
    reports_dir: Path
    logs_dir: Path

    # Zeitzone, wenn kein GPS vorhanden ist
    default_tz: str = "Europe/Berlin"
    # Parallele Arbeiter (Bilder/Kopien) und parallele Video-Encodes (NVENC-Sessions sind begrenzt)
    workers: int = 6
    video_workers: int = 2
    # JPEG-Qualität für Bilder mit eingebranntem Overlay
    jpeg_quality: int = 95
    # Ab welcher relativen Abweichung des Seitenverhältnisses ein Fit-Konflikt entsteht
    ar_tolerance: float = 0.06
    # Ab welcher Differenz (Sekunden) ein älteres eingebettetes Video-Datum das JSON-Datum ersetzt
    embedded_date_min_diff_s: int = 86400
    # Video-Encoder für Overlay-Videos (Fallback: libx265)
    video_encoder: str = "hevc_nvenc"
    nvenc_cq: int = 21
    x265_crf: int = 20
    # Pfad zu exiftool (None = automatisch suchen)
    exiftool: str | None = None

    @classmethod
    def from_base(cls, base: Path, **overrides) -> "Settings":
        base = base.resolve()
        s = cls(
            base_dir=base,
            input_dir=base / "input",
            output_dir=base / "output",
            work_dir=base / "work",
            reports_dir=base / "reports",
            logs_dir=base / "logs",
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, Path(v) if k.endswith("_dir") else v)
        return s

    @property
    def catalog_path(self) -> Path:
        return self.work_dir / "catalog.json"

    @property
    def probe_cache_path(self) -> Path:
        return self.work_dir / "probe_cache.json"

    @property
    def decisions_path(self) -> Path:
        return self.work_dir / "decisions.json"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "manifest.jsonl"

    @property
    def tmp_dir(self) -> Path:
        return self.work_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.output_dir, self.work_dir, self.reports_dir, self.logs_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

"""Overlay-Dateien laden: PNG, WebP (trotz .png-Endung) und Snapchat-"SCOF"-Container.

SCOF enthält mehrere eingebettete PNGs. Beobachtet: eine transparente Ebene mit Text/Stickern
und eine vollflächige, deckende Ebene (Ergebnis einer AR-Lens), die das Foto ersetzt.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from PIL import Image

from snapmem.export_reader import sniff_format

log = logging.getLogger(__name__)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


@dataclass
class OverlayLayers:
    fmt: str                                   # png | webp | scof
    overlays: list[Image.Image] = field(default_factory=list)  # transparente Ebenen (RGBA), Reihenfolge: unten -> oben
    replacement: Image.Image | None = None     # deckende Ebene, ersetzt das Hauptbild (nur SCOF)

    @property
    def size(self) -> tuple[int, int]:
        """Referenzgröße (Leinwand) des Overlays."""
        if self.overlays:
            return self.overlays[0].size
        if self.replacement is not None:
            return self.replacement.size
        raise ValueError("Overlay ohne Ebenen")


def extract_pngs(data: bytes) -> list[bytes]:
    out = []
    i = 0
    while True:
        i = data.find(PNG_SIG, i)
        if i < 0:
            break
        j = data.find(b"IEND", i)
        if j < 0:
            raise ValueError("PNG ohne IEND im SCOF-Container")
        j += 8  # 'IEND' + CRC
        out.append(data[i:j])
        i = j
    return out


def _is_opaque(im: Image.Image) -> bool:
    if im.mode in ("RGB", "L", "P") and "transparency" not in im.info:
        return True
    a = im.convert("RGBA").getchannel("A")
    return a.getextrema()[0] == 255


def load_overlay(data: bytes) -> OverlayLayers:
    fmt = sniff_format(data[:32])
    if fmt in ("png", "webp"):
        im = Image.open(io.BytesIO(data))
        im.load()
        return OverlayLayers(fmt, overlays=[im.convert("RGBA")])
    if fmt == "scof":
        pngs = extract_pngs(data)
        if not pngs:
            raise ValueError("SCOF-Container ohne eingebettete PNGs")
        layers = OverlayLayers("scof")
        for k, b in enumerate(pngs):
            im = Image.open(io.BytesIO(b))
            im.load()
            opaque = _is_opaque(im)
            log.debug("SCOF-Ebene %d: %s %s deckend=%s", k, im.size, im.mode, opaque)
            if opaque:
                if layers.replacement is not None:
                    log.warning("SCOF mit mehreren deckenden Ebenen – verwende die erste")
                    continue
                layers.replacement = im.convert("RGB")
            else:
                layers.overlays.append(im.convert("RGBA"))
        return layers
    raise ValueError(f"Unbekanntes Overlay-Format (Kopf: {data[:16].hex()})")


def aspect_deviation(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Relative Abweichung der Seitenverhältnisse (0.0 = identisch)."""
    ra = a[0] / a[1]
    rb = b[0] / b[1]
    return abs(ra / rb - 1.0)

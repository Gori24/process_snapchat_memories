"""Bild + Overlay zusammenfügen."""
from __future__ import annotations

import io

from PIL import Image, ImageOps

from snapmem.config import FIT_CONTAIN, FIT_COVER, FIT_LETTERBOX, FIT_STRETCH
from snapmem.media.overlay import OverlayLayers

RESAMPLE = Image.Resampling.LANCZOS


def letterbox_canvas(media: tuple[int, int], overlay: tuple[int, int]) -> tuple[int, int]:
    """Leinwand im Seitenverhältnis des Overlays, in der das Medium in Originalgröße Platz hat."""
    k = max(media[0] / overlay[0], media[1] / overlay[1])
    w, h = round(overlay[0] * k), round(overlay[1] * k)
    # gerade Maße (wichtig für Video-Encoder, schadet bei Bildern nicht)
    return w + (w % 2), h + (h % 2)


def place_layer(layer: Image.Image, canvas: tuple[int, int], fit: str) -> Image.Image:
    """Skaliert eine RGBA-Overlay-Ebene gemäß Fit-Modus auf die Leinwandgröße."""
    if layer.size == canvas:
        return layer
    if fit in (FIT_STRETCH, FIT_LETTERBOX):
        return layer.resize(canvas, RESAMPLE)
    if fit == FIT_COVER:
        return ImageOps.fit(layer, canvas, RESAMPLE)
    if fit == FIT_CONTAIN:
        c = ImageOps.contain(layer, canvas, RESAMPLE)
        out = Image.new("RGBA", canvas, (0, 0, 0, 0))
        out.alpha_composite(c, ((canvas[0] - c.width) // 2, (canvas[1] - c.height) // 2))
        return out
    raise ValueError(f"Unbekannter Fit-Modus {fit}")


def composite_image(main: Image.Image, layers: OverlayLayers, fit: str) -> Image.Image:
    base = main.convert("RGB")
    if layers.replacement is not None:
        # AR-Lens-Ergebnis ersetzt das Foto (gleiches Format wie Original)
        base = layers.replacement.resize(base.size, RESAMPLE) if layers.replacement.size != base.size \
            else layers.replacement.copy()
    if not layers.overlays:
        return base
    if fit == FIT_LETTERBOX:
        canvas_size = letterbox_canvas(base.size, layers.size)
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 255))
        canvas.paste(base, ((canvas_size[0] - base.width) // 2, (canvas_size[1] - base.height) // 2))
    else:
        canvas_size = base.size
        canvas = base.convert("RGBA")
    for layer in layers.overlays:
        canvas.alpha_composite(place_layer(layer, canvas_size, fit))
    return canvas.convert("RGB")


def encode_jpeg(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    # 4:4:4 Farbunterabtastung – Text und Zeichnungen bleiben scharf
    im.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True)
    return buf.getvalue()

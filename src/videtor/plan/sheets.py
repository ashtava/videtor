"""Contact sheets: a grid of stills with a label under each, for a person (or Claude) to look at.

Used for the second look at a slot's top candidates and for reading a finished
edit shot by shot. Stills are fitted into equal cells (letterboxed, never
cropped) so a portrait take and a landscape one compare at a glance.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (24, 26, 30)
FG = (236, 238, 242)
MUTED = (150, 156, 166)
ACCENT = (69, 188, 198)


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc", "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit(im: Image.Image, w: int, h: int) -> Image.Image:
    """`im` scaled to fit inside w×h, centred on the sheet background."""
    im = im.convert("RGB")
    scale = min(w / im.width, h / im.height)
    small = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
    cell = Image.new("RGB", (w, h), BG)
    cell.paste(small, ((w - small.width) // 2, (h - small.height) // 2))
    return cell


def sheet(items: list[dict], out: Path, cols: int = 5, cell: tuple[int, int] = (180, 320), title: str | None = None) -> Path:
    """Write a grid of `items` ({image: path, label: str, sub: str, mark: bool}) to `out` as JPEG.

    `label` is the bold first line under the still, `sub` a muted second line;
    `mark` outlines the cell in the accent colour (the current pick).
    """
    w, h = cell
    label_h = 40
    rows = max(1, -(-len(items) // cols))
    top = 34 if title else 0
    canvas = Image.new("RGB", (cols * (w + 8) + 8, top + rows * (h + label_h + 8) + 8), BG)
    draw = ImageDraw.Draw(canvas)
    big, small = _font(15), _font(12)
    if title:
        draw.text((10, 8), title, fill=FG, font=big)
    for i, it in enumerate(items):
        x, y = 8 + (i % cols) * (w + 8), top + 8 + (i // cols) * (h + label_h + 8)
        if not it.get("image"):
            continue  # an empty cell
        try:
            with Image.open(it["image"]) as im:
                canvas.paste(fit(im, w, h), (x, y))
        except (OSError, ValueError):
            draw.rectangle([x, y, x + w, y + h], outline=MUTED)
            draw.text((x + 8, y + h // 2), "no still", fill=MUTED, font=small)
        if it.get("mark"):
            draw.rectangle([x - 3, y - 3, x + w + 2, y + h + 2], outline=ACCENT, width=3)
        draw.text((x + 2, y + h + 4), str(it.get("label", ""))[:28], fill=FG, font=big)
        draw.text((x + 2, y + h + 22), str(it.get("sub", ""))[:32], fill=MUTED, font=small)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=86)
    return out

"""Where each glyph of the help screen actually sits, column by column.

``read_screenshots.py`` answered "which colours are on this row". This one answers the harder
question behind the alignment complaint: what advance does each character get in the font the
player is running? The padding was computed in characters and the complaint is measured in
pixels, so the only way to know whether a pixel-based fix will help is to read the pixels.

Prints, per row, the runs of lit columns (a run is one glyph, or a few glyphs that touch) so
the gap between the verb and the dashes can be compared across rows.
"""

import pathlib
import sys

from PIL import Image

MC_COLOURS = {
    "gold": (255, 170, 0),
    "yellow": (255, 255, 85),
    "aqua": (85, 255, 255),
    "white": (255, 255, 255),
    "gray": (170, 170, 170),
    "red": (255, 85, 85),
    "green": (85, 255, 85),
}
SNAP = 24


def classify(pixel):
    for name, target in MC_COLOURS.items():
        if sum((a - b) ** 2 for a, b in zip(pixel, target)) <= SNAP ** 2 * 3:
            return name
    return None


def bands(image, threshold=150):
    width, height = image.size
    pixels = image.load()
    lit_rows = [
        sum(1 for x in range(width) if sum(pixels[x, y]) / 3 > threshold) >= 3
        for y in range(height)
    ]
    out, start = [], None
    for y, lit in enumerate(lit_rows):
        if lit and start is None:
            start = y
        elif not lit and start is not None:
            if y - start >= 5:
                out.append((start, y))
            start = None
    return out


def analyse(path):
    image = Image.open(path).convert("RGB")
    width, _ = image.size
    pixels = image.load()
    print(pathlib.Path(path).name, image.size)
    for index, band in enumerate(bands(image)):
        runs = []
        start = None
        for x in range(width):
            colour = None
            for y in range(band[0], band[1]):
                colour = classify(pixels[x, y])
                if colour:
                    break
            if colour and start is None:
                start = x
            elif not colour and start is not None:
                runs.append((start, x - 1))
                start = None
        if not runs:
            continue
        head = "  ".join("%d-%d" % run for run in runs[:14])
        print("row %2d y=%3d: %s%s" % (
            index + 1, band[0], head, "  ..." if len(runs) > 14 else ""
        ))


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        analyse(argument)

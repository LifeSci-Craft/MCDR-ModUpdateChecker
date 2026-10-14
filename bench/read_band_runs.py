"""列出指定行带的**全部**字形游程（`read_screenshot_runs.py` 只打前 14 个）。

用来量单个字形的步进：游程起点之差 = 前一个字的 advance。带上是哪一行由调用者自己认，
所以先按行号打出来，再人工对着截图数。
"""

import sys

from PIL import Image

from read_screenshot_runs import bands, classify


def analyse(path, wanted):
    image = Image.open(path).convert("RGB")
    width, _ = image.size
    pixels = image.load()
    for index, band in enumerate(bands(image)):
        if wanted and index not in wanted:
            continue
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
        if start is not None:
            runs.append((start, width - 1))
        print("band {:>3} y={:>3}-{:<3} 共 {} 段".format(index, band[0], band[1], len(runs)))
        for number, (a, b) in enumerate(runs):
            advance = runs[number + 1][0] - a if number + 1 < len(runs) else None
            print("    #{:<3} {:>5}-{:<5} 宽 {:>3}  advance {}".format(
                number, a, b, b - a + 1, "-" if advance is None else advance))
        print()


if __name__ == "__main__":
    path = sys.argv[1]
    wanted = {int(value) for value in sys.argv[2:]}
    analyse(path, wanted)

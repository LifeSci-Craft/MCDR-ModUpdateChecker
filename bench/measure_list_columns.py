"""量出玩家截图里列表每一格的起始像素列——先把「歪多少」变成数字。

`read_screenshots.py` 回答「这一行有哪些颜色」，`read_screenshot_runs.py` 回答「每个字形落在
哪些列」。这一份回答的是列表分列那个具体问题：**每一行的中间格、状态格、链接格分别从哪个 x 开始**。

做法：先按亮度切出行带，再在每一带里按颜色找块的起点。列的颜色是唯一的信号——
`[版本]` 绿、`[详细信息]` aqua、红色状态红、灰色状态灰、编号金——所以不需要认字形，
也不会被名字里的空格骗到（名字里有空格的 `Simple Voice Chat` 正是按空格分词会出错的那种）。

输出每行的四个起点，末尾打印各自的最大最小差（同一列跨行的抖动），那就是要对齐掉的东西。
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
    "blue": (85, 85, 255),
}
SNAP = 30

#: 哪些颜色算「找到了这个格子」。顺序 = 报告顺序。编号是 **yellow**（黄），标题栏才是 gold。
WATCH = ("yellow", "green", "aqua", "red", "gray")


def classify(pixel):
    for name, target in MC_COLOURS.items():
        if sum((a - b) ** 2 for a, b in zip(pixel, target)) <= SNAP ** 2 * 3:
            return name
    return None


def bands(image, threshold=150, min_height=5):
    width, height = image.size
    pixels = image.load()
    lit = [
        sum(1 for x in range(width) if sum(pixels[x, y]) / 3 > threshold) >= 3
        for y in range(height)
    ]
    out, start = [], None
    for y, is_lit in enumerate(lit):
        if is_lit and start is None:
            start = y
        elif not is_lit and start is not None:
            if y - start >= min_height:
                out.append((start, y))
            start = None
    if start is not None and height - start >= min_height:
        out.append((start, height))
    return out


def first_x(image, band, colour):
    """这一带里第一个属于 ``colour`` 的像素列。"""
    width, _ = image.size
    pixels = image.load()
    for x in range(width):
        for y in range(band[0], band[1]):
            if classify(pixels[x, y]) == colour:
                return x
    return None


def analyse(path):
    image = Image.open(path).convert("RGB")
    print(pathlib.Path(path).name, image.size)
    print()
    print("Mod | " + " | ".join("{:>6}".format(name) for name in WATCH))
    starts = {name: [] for name in WATCH}
    rows = []
    for index, band in enumerate(bands(image)):
        found = {colour: first_x(image, band, colour) for colour in WATCH}
        for colour, x in found.items():
            if x is not None:
                starts[colour].append(x)
        rows.append(found)
        print("{:>3} | {}".format(
            index + 1, " | ".join("{:>6}".format("-" if found[c] is None else found[c])
                                  for c in WATCH)))
    print()
    for colour in WATCH:
        values = starts[colour]
        if not values:
            continue
        print("{:<6} n={:<2} 范围 {}-{}  抖动 {:>3} px".format(
            colour, len(values), min(values), max(values), max(values) - min(values)))
    print()
    print("以编号（gold）为行首基准，各格的起始列：")
    print("Mod | 中格-行首 | 状态格-行首 | 链接格-行首")
    for index, found in enumerate(rows):
        base = found.get("gold")
        if base is None:
            continue
        cells = [found.get("green"), found.get("gray"), found.get("aqua")]
        print("{:>3} | {}".format(index + 1, " | ".join(
            "{:>10}".format("-" if x is None else x - base) for x in cells)))


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        analyse(argument)

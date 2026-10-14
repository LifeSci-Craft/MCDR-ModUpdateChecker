"""语言文件审计：找「写进去过、现在其实没用」的条目。

`tests/test_i18n.py` 的守卫盯的是「这个键在发布模块里出现过」——正则扫 `mod_update_checker/*.py`
的**全文**，所以有三种东西它看不见：

1. **只在注释或 docstring 里出现的键**（正则会照样命中）；
2. **模板里的占位符没人传**（`{foo}` 留在句子里，或者某次调用少传一个）；
3. **两个键的值一模一样**（往往是改名留下的孪生条目）。

这一份用 AST 只收「作为调用实参出现的字符串」，再逐键比对占位符与调用处传的关键字。

用法（仓库根目录）：``python bench/audit_language.py``
"""

import ast
import json
import pathlib
import re
import sys
from collections import defaultdict

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "mod_update_checker"

#: 这些族是运行时拼出来的（键名不在源码里，后缀来自常量表），由 test_i18n 的三条守卫盯着。
DYNAMIC_PREFIXES = ("status.", "matched_by.", "install.reason.", "download.reason.", "explain.")

_PLACEHOLDER = re.compile(r"\{([a-z_0-9]+)\}")


def string_constants(path: pathlib.Path) -> set:
    """这个模块里作为**字符串常量**出现过的全部字面量（含调用实参、字典值、常量表）。

    判据比「出现在文件里」严格：键若只被注释或 docstring 提到，它不会是一个独立的
    ``ast.Constant``（docstring 那一整段是**一段**常量，不等于键本身），于是会被抓出来。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def calls_with_key(path: pathlib.Path):
    """``(键, 传了哪些关键字, 行号)`` —— 只认**作为调用实参**的字符串常量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                out.append((argument.value, keywords, node.lineno))
    return out


def main() -> int:
    languages = sorted(PACKAGE.glob("lang/*.json"))
    catalogues = {
        path.stem: json.loads(path.read_text(encoding="utf-8")) for path in languages
    }
    chinese = catalogues["zh_cn"]

    modules = sorted(PACKAGE.glob("*.py"))
    constants: set = set()
    for path in modules:
        constants |= string_constants(path)

    passed: dict = defaultdict(list)
    for path in modules:
        for key, keywords, line in calls_with_key(path):
            passed[key].append((keywords, path.name, line))

    print("=" * 78)
    print("1. 没有作为字符串常量出现过的键（只在注释/docstring 里，或彻底没人用）")
    print("=" * 78)
    suspicious = []
    for key in sorted(chinese):
        if key.startswith(DYNAMIC_PREFIXES):
            continue
        if key not in constants:
            suspicious.append(key)
    print("\n".join("  " + key for key in suspicious) or "  （无）")

    print()
    print("=" * 78)
    print("2. 模板里的占位符，从来没有人传过")
    print("=" * 78)
    orphan_placeholders = []
    for key in sorted(chinese):
        if key.startswith(DYNAMIC_PREFIXES):
            continue
        wanted = set(_PLACEHOLDER.findall(chinese[key]))
        if not wanted:
            continue
        given = set()
        for keywords, _name, _line in passed.get(key, []):
            given |= keywords
        missing = sorted(char for char in wanted if char not in given)
        if missing:
            orphan_placeholders.append((key, missing, chinese[key]))
    for key, missing, text in orphan_placeholders:
        print("  {:<34} 没人传 {}  ← {}".format(key, missing, text))
    if not orphan_placeholders:
        print("  （无）")

    print()
    print("=" * 78)
    print("3. 值与别人完全相同的键（改名留下的孪生条目）")
    print("=" * 78)
    by_value = defaultdict(list)
    for key, text in chinese.items():
        by_value[text].append(key)
    twins = [(text, keys) for text, keys in by_value.items() if len(keys) > 1]
    for text, keys in sorted(twins, key=lambda item: item[1]):
        if text.strip() in ("",):
            continue
        print("  {!r:<30} {}".format(text, "、".join(sorted(keys))))
    if not twins:
        print("  （无）")

    print()
    print("=" * 78)
    print("4. 两种语言里长度差得最多的条目（可能是没同步的翻译）")
    print("=" * 78)
    english = catalogues["en_us"]
    rows = []
    for key in sorted(chinese):
        rows.append((abs(len(chinese[key]) - len(english[key])), key,
                     chinese[key], english[key]))
    for delta, key, zh, en in sorted(rows, reverse=True)[:8]:
        print("  Δ{:<3} {:<30} {}".format(delta, key, zh.replace("\n", "\\n")))
        print("      {:<30} {}".format("", en.replace("\n", "\\n")))

    print()
    print("键数：zh_cn {} / en_us {}".format(len(chinese), len(english)))
    return 1 if (suspicious or orphan_placeholders) else 0


if __name__ == "__main__":
    sys.exit(main())

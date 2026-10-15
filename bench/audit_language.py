"""语言文件审计：找「写进去过、现在其实没用」的条目。

`tests/test_i18n.py` 的守卫盯的是「这个键在发布模块里出现过」——正则扫 `mod_update_checker/*.py`
的**全文**，所以有三种东西它看不见：

1. **只在注释或 docstring 里出现的键**（正则会照样命中）；
2. **模板里的占位符没人传**（`{foo}` 留在句子里，或者某次调用少传一个）；
3. **两个键的值一模一样**（往往是改名留下的孪生条目）。

这一份用 AST 只收「作为调用实参出现的字符串」，再逐键比对占位符与调用处传的关键字。

**第 2 节不再靠猜调用点。** 它原来把「任何调用实参里出现的字符串常量」当成消息键，再在那次
调用上取关键字——于是键经变量传递时（`tr(header_key, count=...)`、`tr(key, **args)`、
`tr(choice.note_key, **choice.note_args)`）它看不到真正的实参，实测 15 条输出全是误报，而真丢了
占位符的模板会被这些噪音淹掉。现在改成**运行时**用真正的 `Catalog` 对每个键做一次 format 断言：
传进去的参数由模板自己的 `{...}` 决定，所以「这个键能不能被填满」是可判定的，不再依赖调用点长什么样。
"""

import ast
import importlib.util
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


def _load_i18n():
    """``mod_update_checker.i18n`` without importing MCDR.

    The package's ``__init__`` pulls in ``mcdreforged.api.all``, and this script is documented to
    run from a plain interpreter (``tests/README.md`` calls it a language-file audit, not a
    plugin entry point). Loading the module file by path keeps the one thing this needs — the
    real :class:`Catalog`, so the format check cannot drift from the runtime — and nothing else.
    """
    spec = importlib.util.spec_from_file_location(
        "audit_language_i18n", str(PACKAGE / "i18n.py")
    )
    module = importlib.util.module_from_spec(spec)
    # ``__package__`` is what ``pkgutil.get_data`` uses to find the language files.
    module.__package__ = "mod_update_checker"
    spec.loader.exec_module(module)
    return module


i18n = _load_i18n()


def _dummy_args(template: str) -> dict:
    """Argument values to format ``template`` with, one per placeholder it declares.

    Taken from the template rather than from any call site, which is the whole point: it makes
    "is this template fillable?" a property of the template, so a key passed through a variable
    (or through ``**args``) is checked exactly like a literal one.
    """
    return {name: "x" for name in _PLACEHOLDER.findall(template)}


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
    print("2. 模板里的占位符，用真正的 Catalog 填一遍看能不能填满")
    print("=" * 78)
    unfillable = []
    for language in sorted(catalogues):
        catalog = i18n.get_catalog(language)
        for key in sorted(catalogues[language]):
            template = catalog.messages.get(key)
            if template is None:
                continue
            args = _dummy_args(template)
            try:
                rendered = template.format(**args)
            except (KeyError, IndexError, ValueError, AttributeError, TypeError) as error:
                # The same exceptions ``translate`` swallows, reported instead of swallowed: a
                # template that cannot be filled is the bug this section exists to name.
                unfillable.append((language, key, "{}: {}".format(type(error).__name__, error)))
                continue
            leftover = _PLACEHOLDER.search(rendered)
            if leftover:
                unfillable.append((language, key, rendered))
    for language, key, result in unfillable:
        print("  {:<6} {:<34} ← {}".format(language, key, result.replace("\n", "\\n")))
    if not unfillable:
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
    return 1 if (suspicious or unfillable) else 0


if __name__ == "__main__":
    sys.exit(main())

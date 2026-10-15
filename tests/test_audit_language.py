"""``bench/audit_language.py`` — the maintenance tool, against its own false positives.

Its second section used to infer "who passes this placeholder" from *any* call whose arguments
contained the key as a string literal. Keys that travel through a variable — ``tr(header_key,
count=...)``, ``tr(key, **args)``, ``tr(choice.note_key, **choice.note_args)`` — were therefore
attributed to whatever unrelated call happened to mention the literal, and the tool reported
15/15 findings that were all false. Noise at that level is worse than no tool: the real finding
is in the pile somewhere and nobody reads it.

The fix is to stop inferring and ask the templates: format each one with arguments derived from
its own placeholders. That is a property of the file, so these tests pin the property rather
than any particular call site.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "bench" / "audit_language.py"
PACKAGE = REPO / "mod_update_checker"


def _run():
    result = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.returncode, result.stdout.decode("utf-8", "replace")


def test_the_tool_runs_and_reports_nothing_suspicious():
    """The repository's own language files are clean, and the tool says so."""
    code, output = _run()

    assert code == 0, output
    assert "键数：zh_cn" in output, output
    # Both the "unreferenced key" and the "unfillable template" sections.
    assert output.count("（无）") >= 2, output


def test_no_unfillable_placeholder_is_reported():
    """The 15 findings that used to be reported were all keys passed through a variable.

    Asserted by absence of the *shape* of that output rather than by a count, so a genuine
    finding still fails this test.
    """
    _code, output = _run()

    # From the section heading to the next one, minus the heading line itself.
    lines = output.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith("2. ")
    )
    end = next(
        index for index, line in enumerate(lines[start + 1:], start + 1)
        if line.startswith("3. ")
    )
    body = [line.strip() for line in lines[start + 1:end]]
    body = [line for line in body if line and not set(line) <= set("=")]
    assert body == ["（无）"], body


def test_every_template_can_be_formatted_with_its_own_placeholders():
    """The property the section rests on, checked without the tool's formatting.

    Every message in both catalogues must survive ``str.format`` with one value per placeholder
    it declares — which is exactly what the runtime does when a caller passes the arguments named
    in the template.
    """
    import re

    placeholder = re.compile(r"\{([a-z_0-9]+)\}")
    for path in sorted(PACKAGE.glob("lang/*.json")):
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        for key, template in catalogue.items():
            args = {name: "x" for name in placeholder.findall(template)}
            rendered = template.format(**args)
            assert not placeholder.search(rendered), (path.name, key, rendered)

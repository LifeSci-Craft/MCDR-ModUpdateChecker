"""Pytest bootstrap.

Puts the repository root on ``sys.path`` so that ``import mod_update_checker`` resolves to
this repo's package even when pytest is invoked with an unusual rootdir. Without this, a
``mod_update_checker`` package living elsewhere in the environment could be imported
instead, and the tests would silently validate the wrong code.

Real MCDR is a hard requirement for the suite (the plugin's config class subclasses
``mcdreforged.api.utils.Serializable``, and the end-to-end test boots a real MCDR instance).
See tests/README.md for how to create the interpreter.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Real MCDR, installed by tests/README.md's pip --target step, if that layout is in use.
TESTLIBS = ROOT / ".testlibs"
if TESTLIBS.is_dir() and str(TESTLIBS) not in sys.path:
    sys.path.insert(0, str(TESTLIBS))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

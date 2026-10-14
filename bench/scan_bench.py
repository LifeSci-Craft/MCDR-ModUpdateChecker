"""Measure what the scan stage actually costs, so any claim about speeding it up is grounded.

Four numbers, all on the same synthetic folder:

  1. the current ``scan_mods`` — one digest, one pass
  2. the same walk with a **second** digest added back, which is what v1.1.0 and earlier did:
     a SHA-512 that no caller ever read. The difference between 1 and 2 is the work v1.1.1
     removed, and it is the number quoted in the changelog;
  3. the same walk across a thread pool, to show what parallelising it *would* buy — measured
     rather than assumed, and deliberately not taken. The scan runs on a daemon thread sixty
     seconds after the server finishes starting, so it is not on any critical path, and a
     few tenths of a second is not worth the extra threads;
  4. a cold-ish read of the same files, for scale.

The jars are synthetic: a real ``fabric.mod.json`` plus a stored (incompressible) payload, so
the hashing work is representative of a real jar and the metadata read is a real read.

Run it after any change to ``digests.py`` or ``scanner.py``; the changelog's numbers come from
here and this is what makes them checkable.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mod_update_checker.scanner as scanner  # noqa: E402
from mod_update_checker.digests import READ_CHUNK  # noqa: E402
from mod_update_checker.scanner import iter_mod_jars, scan_jar, scan_mods  # noqa: E402

JARS = 120
PAYLOAD_MB = 2
POOL_SIZES = (2, 4, 8)


def build(folder: Path) -> None:
    payload = os.urandom(PAYLOAD_MB * 1024 * 1024)
    for index in range(JARS):
        name = folder / "bench-mod-{:03d}.jar".format(index)
        with zipfile.ZipFile(name, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "id": "benchmod{}".format(index),
                        "name": "Bench Mod {}".format(index),
                        "version": "1.{}.0".format(index),
                        "environment": "server",
                        "depends": {"minecraft": ">=1.21 <1.22"},
                    }
                ),
            )
            archive.writestr("payload.bin", payload)


def two_digests(handle) -> tuple:
    """What v1.1.0 computed: a SHA-1 for the lookup and a SHA-512 nobody read."""
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    size = 0
    while True:
        block = handle.read(READ_CHUNK)
        if not block:
            break
        size += len(block)
        sha1.update(block)
        sha512.update(block)
    return sha1.hexdigest(), size


def timed(label: str, function) -> float:
    started = time.perf_counter()
    result = function()
    elapsed = time.perf_counter() - started
    print("  {:<46} {:6.2f}s   {}".format(label, elapsed, result))
    return elapsed


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="muc_bench_"))
    try:
        print("building {} jars of ~{} MiB in {}".format(JARS, PAYLOAD_MB, work))
        build(work)
        total = sum(path.stat().st_size for path in work.glob("*.jar"))
        print("  {:.1f} MiB on disk\n".format(total / 1024 / 1024))

        # Warm the OS page cache first, so what is measured is hashing and not the disk.
        for path in work.glob("*.jar"):
            with open(path, "rb") as handle:
                while handle.read(READ_CHUNK):
                    pass

        print("timings (page cache warm):")
        baseline = timed(
            "current scan_mods (one digest)",
            lambda: len(scan_mods(work).mods),
        )

        original = scanner.digests_of_file
        scanner.digests_of_file = two_digests
        try:
            with_second = timed(
                "the same walk with the second digest back",
                lambda: len(scan_mods(work).mods),
            )
        finally:
            scanner.digests_of_file = original

        jobs, _ = iter_mod_jars(work)
        pooled = {
            workers: timed(
                "pooled scan, {} workers".format(workers),
                lambda w=workers: len(list(ThreadPoolExecutor(max_workers=w).map(scan_jar, jobs))),
            )
            for workers in POOL_SIZES
        }

        print("\n  the work v1.1.1 removed")
        print("    {:.2f}s -> {:.2f}s, i.e. {:.0%} of the scan".format(
            with_second, baseline, (with_second - baseline) / with_second))
        print("  what parallelising it would add on top")
        for workers, elapsed in pooled.items():
            print("    pool of {:<2}  {:.2f}x  (not taken — see the module docstring)".format(
                workers, baseline / elapsed))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

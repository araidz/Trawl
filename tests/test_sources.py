"""Trawl test suite — relocated out of the trawl package (audit: keep the
shipped zipapp free of test code). The body executes verbatim inside the
package module's namespace (exec with the module __dict__), so bare names and
globals() monkeypatches behave exactly as they did in-package.

Run:  python3 tests/test_sources.py   (from the repo root, or via run_tests.sh)
"""

import pathlib
import sys

import _hermetic  # noqa: F401  (HOME -> temp dir before trawl loads)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import trawl.sources as _mod

_body = pathlib.Path(__file__).with_name("_sources_body.py")
exec(compile(_body.read_text(), str(_body), "exec"), _mod.__dict__)

#!/usr/bin/env python3
"""One-time relocation: extract selftest bodies out of the trawl package
into tests/ so the shipped zipapp stops carrying ~2,900 lines of test code.

Each body is sliced verbatim (dedented) from `def selftest` to the trailing
`if __name__` block, written as plain source (`_<mod>_body.py`), and executed
by tests/test_<mod>.py inside the package module's __dict__ — so every bare
name and `globals()` monkeypatch behaves exactly as it did in-package.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "trawl"
OUT = ROOT / "tests"
OUT.mkdir(exist_ok=True)

_MODULES = ["aria2", "sources", "meta", "tui"]


def extract_body(path: pathlib.Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.startswith("def selftest"))
    end = next(i for i, l in enumerate(lines) if l.startswith('if __name__ == "__main__"'))
    return "".join(l[4:] if l.startswith("    ") else l for l in lines[start + 1:end])


HEADER = '''"""Trawl test suite — relocated out of the trawl package (audit: keep the
shipped zipapp free of test code). The body executes verbatim inside the
package module's namespace (exec with the module __dict__), so bare names and
globals() monkeypatches behave exactly as they did in-package.

Run:  python3 tests/{test}   (from the repo root, or via run_tests.sh)
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import trawl.{mod} as _mod

_body = pathlib.Path(__file__).with_name("_{mod}_body.py")
exec(compile(_body.read_text(), str(_body), "exec"), _mod.__dict__)
'''

for mod in _MODULES:
    src = SRC / f"{mod}.py"
    body = extract_body(src)
    if not body.strip():
        raise SystemExit(f"no selftest body in {src}")
    (OUT / f"_{mod}_body.py").write_text(body)
    (OUT / f"test_{mod}.py").write_text(HEADER.format(mod=mod, test=f"test_{mod}.py"))
    # truncate the package module at the self-check section
    text = src.read_text()
    cut = re.search(r"^# -- self-check .*\n.*?def selftest", text, re.M | re.S)
    trimmed = text[:cut.start()].rstrip() + "\n"
    src.write_text(trimmed)
    print(f"extracted {len(body.splitlines()):5d} lines from {mod}.py -> tests/")

(ROOT / "run_tests.sh").write_text('''#!/bin/sh
# Run the relocated trawl test suite (tests/_, each body executes in the
# package module's own namespace).
set -e
cd "$(dirname "$0")"
python3 tests/test_aria2.py
python3 tests/test_meta.py
python3 tests/test_sources.py
python3 tests/test_tui.py
echo "all trawl tests passed"
''')
(ROOT / "run_tests.sh").chmod(0o755)
print("wrote run_tests.sh")
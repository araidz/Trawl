#!/bin/sh
# Run the relocated trawl test suite (tests/_, each body executes in the
# package module's own namespace).
set -e
cd "$(dirname "$0")"
python3 tests/test_aria2.py
python3 tests/test_meta.py
python3 tests/test_sources.py
python3 tests/test_tui.py
python3 tests/test_canary.py
python3 tests/test_follow.py
echo "all trawl tests passed"

"""Offline checks for scripts/canary.py: classification, retries, and the report.

Run:  python3 tests/test_canary.py   (also run by run_tests.sh)
"""

import importlib.util
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parent.parent
import _hermetic  # noqa: F401  (HOME -> temp dir before trawl loads)

sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location("canary", root / "scripts" / "canary.py")
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)
from trawl.sources import Source, SourceError  # noqa: E402

# classification: refusals from a CI runner are not scraper breakage
assert canary.classify(3, "") == "ok" and canary.classify(0, "") == "empty"
for err in ("blocked by Cloudflare's browser check — try again", "HTTP 403", "HTTP 429", "timed out",
            "DNS lookup failed — site blocked or you're offline", "connection refused or reset — site down"):
    assert canary.classify(0, err) == "blocked", err
for err in ("HTTP 500", "bad json: Expecting value", "KeyError: 'results'"):
    assert canary.classify(0, err) == "error", err

# queries follow the source's group
asked = []
def src(id, group, fn):
    return Source(id, id, group, fn)
def spy(results):
    return lambda q: (asked.append(q), results)[1]
for group, q in (("Movies", "oppenheimer"), ("TV", "the office"), ("Anime", "one piece"),
                 ("Books", "dune"), ("Games", "cyberpunk"), ("Other", "ubuntu")):
    canary.check(src("s", group, spy(["r"])), pause=0)
    assert asked[-1] == q, (group, asked[-1])

# sources that can't take the group's query use their own (eztv is browse-only)
canary.check(src("eztv", "TV", spy(["r"])), pause=0)
assert asked[-1] == "", "eztv gets the latest feed"
canary.check(src("nyaa-books", "Books", spy(["r"])), pause=0)
assert asked[-1] == "book"
assert "latest feed" in canary.table([canary.check(src("eztv", "TV", spy(["r"])), pause=0)])
from trawl.sources import SOURCES as _S  # noqa: E402
assert set(canary.QUERY_OVERRIDE) <= {s.id for s in _S}, "an override names a source that no longer exists"

# check(): ok / empty (retried once) / flaky / blocked / error / unexpected exception
assert canary.check(src("a", "Movies", lambda q: ["r", "r"]), pause=0)["count"] == 2
calls = []
assert canary.check(src("b", "Movies", lambda q: calls.append(1) or []), pause=0)["status"] == "empty" and len(calls) == 2
flaky = iter([[], ["r"]])
assert canary.check(src("c", "Movies", lambda q: next(flaky)), pause=0)["status"] == "ok", "an empty then ok is not a break"
tries = []
def refuse(q):
    tries.append(1)
    raise SourceError("HTTP 403")
assert canary.check(src("d", "Movies", refuse), pause=0)["status"] == "blocked" and len(tries) == 1, "blocked isn't retried"
def parse_fail(q):
    raise SourceError("bad json: Expecting value")
rec = canary.check(src("e", "Movies", parse_fail), pause=0)
assert rec["status"] == "error" and "bad json" in rec["detail"], rec
assert canary.check(src("f", "Movies", lambda q: 1 / 0), pause=0)["status"] == "error", "any exception is a finding"

# run() keeps source order; failing() and the exit decision
canary.time.sleep = lambda s: None
rows = canary.run([src("ok1", "Movies", lambda q: ["r"]), src("dead", "TV", lambda q: []),
                   src("blk", "Anime", refuse), src("ok2", "Books", lambda q: ["r"])], workers=4)
assert [r["id"] for r in rows] == ["ok1", "dead", "blk", "ok2"]
assert [r["id"] for r in canary.failing(rows)] == ["dead"], "blocked and ok never fail the canary"

# report
md = canary.table(rows)
assert md.startswith("**1 of 4 sources look broken** (1 refused the runner — not counted)")
lines = md.splitlines()
assert lines[4].startswith("| dead | ❌ empty |") and "`the office`" in lines[4], "real failures sort first"
assert lines[5].startswith("| blk | ⚠️ blocked |") and "HTTP 403" in lines[5], "then refusals"
assert lines[6].startswith("| ok1 |") and lines[7].startswith("| ok2 |")
assert canary.table([rows[0], rows[3]]).startswith("All 2 sources answered")
assert not canary.failing([rows[0], rows[2]])

# the real source list: every group has a query, nothing is skipped
from trawl.sources import SOURCES  # noqa: E402
assert all(canary.QUERIES.get(s.group, canary.DEFAULT_QUERY) for s in SOURCES) and len(SOURCES) >= 20
print("canary ok")

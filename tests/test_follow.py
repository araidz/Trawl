"""Offline checks for trawl/follow.py: episode parsing, show names, storage, new-episode logic.

Run:  python3 tests/test_follow.py   (also run by run_tests.sh)
"""

import json
import pathlib
import sys
import tempfile
import time

import _hermetic  # noqa: F401  (HOME -> temp dir before trawl loads)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import trawl.follow as F
from trawl.sources import Result, Source, SourceError

# episode numbers: scene, 2x05, anime absolute (up to 4 digits), and the things that aren't one
for name, ep in (("Severance.S02E05.1080p.WEB.H264-GRP.mkv", (2, 5)), ("The.Office.US.S03E04.720p.HDTV", (3, 4)),
                 ("Show 2x05 720p", (2, 5)), ("show s1e2 480p", (1, 2)),
                 ("[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv", (1, 12)),
                 ("[Erai-raws] One Piece - 1100 [1080p][HEVC].mkv", (1, 1100)),
                 ("[Judas] Show - 03v2 [1080p]", (1, 3)), ("Show E12 [1080p]", (1, 12))):
    assert F.episode_of(name) == ep, (name, F.episode_of(name))
for name in ("Show.S01E01-E08.1080p.BluRay", "Severance.S02.COMPLETE.1080p", "Dune.2021.1080p.WEB-DL",
             "Dune - 2024 1080p", "Show.S01E01E02.720p", "Book.epub", ""):
    assert F.episode_of(name) is None, name
assert F.fmt_ep((2, 5)) == "S02E05" and F.fmt_ep([1, 1100]) == "S01E1100"

# the show's name: what precedes the episode marker, minus [group] tags and a trailing year
for name, title in (("Severance.S02E05.1080p.WEB", "Severance"), ("The.Office.US.S03E04.720p", "The Office US"),
                    ("[SubsPlease] Frieren - 12 (1080p) [ABCD].mkv", "Frieren"),
                    ("Doctor.Who.2005.S01E01.720p", "Doctor Who"), ("[Grp][Grp2] A Show_Name - 07 [720p]", "A Show Name"),
                    ("Dune.2021.1080p", ""), ("Show.S01.COMPLETE", "")):
    assert F.show_title(name) == title, (name, F.show_title(name))

# make_sub: only TV/anime episodes; the episode is the baseline, quality is remembered
sub = F.make_sub("Severance.S02E05.1080p.WEB.H264-GRP", "TV")
assert sub == {"id": "severance", "title": "Severance", "query": "Severance", "group": "TV", "res": 1080,
               "last": [2, 5], "auto": False, "checked": 0.0}, sub
assert F.make_sub("Severance.S02E05.1080p", "Movies") is None and F.make_sub("Dune.2021.1080p", "TV") is None
assert F.make_sub("Show S01E01", "Anime")["res"] == 0, "unknown quality means any"

# find_new: newer than the baseline, one per episode (best seeded), same quality, right show
sub = F.make_sub("Severance.S02E05.1080p.WEB", "TV")
R = lambda name, seeds=1, size=1, src="eztv": Result(name[:40].ljust(40, "0").encode().hex()[:40], name, size, seeds, 0, src, "m")
rs = [R("Severance.S02E05.1080p.WEB", 90), R("Severance.S02E06.1080p.WEB-A", 10), R("Severance.S02E06.1080p.WEB-B", 50),
      R("Severance.S02E06.720p.HDTV", 99), R("Severance.S03E01.1080p", 5), R("Severance.S02E04.1080p", 70),
      R("Other.Show.S02E07.1080p", 80), R("Severance.S02.COMPLETE.1080p", 60), R("Severance.S02E07.WEB", 40)]
new = F.find_new(sub, rs)
assert [r.name for r in new] == ["Severance.S02E06.1080p.WEB-B", "Severance.S03E01.1080p"], [r.name for r in new]
assert [r.name for r in F.find_new({**sub, "res": 0}, rs)] == [
    "Severance.S02E06.720p.HDTV", "Severance.S02E07.WEB", "Severance.S03E01.1080p"], "res 0 = any quality"
assert F.find_new(sub, []) == [] and F.find_new({**sub, "last": [9, 99]}, rs) == []
tie = F.find_new(sub, [R("Severance.S02E06.1080p.A", 5, 100), R("Severance.S02E06.1080p.B", 5, 200)])
assert tie[0].name.endswith(".B"), "equal seeders: the bigger release wins"
two = F.find_new({**sub, "query": "The Office", "last": [0, 0]}, [R("The.Office.US.S01E01.1080p"), R("The Office S01E02 1080p"),
                                                  R("Office.S01E03.1080p")])
assert len(two) == 2, "every word of the query must be in the name"

# storage: round trip, junk dropped, a corrupt file is just empty
tmp = pathlib.Path(tempfile.mkdtemp())
F.SUBS_FILE = tmp / "subscriptions.json"
assert F.load_subs() == []
good = F.make_sub("Severance.S02E05.1080p", "TV")
F.save_subs([good])
assert F.load_subs() == [good]
F.SUBS_FILE.write_text(json.dumps([good, {"id": 1}, "x", None, {**good, "last": [1]}, {**good, "group": "Books"},
                                   {**good, "auto": "yes"}, {**good, "last": ["a", 1]}]))
assert F.load_subs() == [good], "only well-formed records survive"
F.SUBS_FILE.write_text("{not json"); assert F.load_subs() == []
F.SUBS_FILE.write_text('{"a": 1}'); assert F.load_subs() == []

# check_sub: gathers across sources, counts the ones that answered, survives failures
def ok(results):
    return lambda q: results
def boom(q):
    raise SourceError("HTTP 500")
srcs = [Source("a", "A", "TV", ok([R("Severance.S02E06.1080p.WEB", 10)])),
        Source("b", "B", "TV", ok([R("Severance.S02E06.1080p.WEB", 30), R("Severance.S02E07.1080p.WEB", 5)])),
        Source("c", "C", "TV", boom)]
new, answered = F.check_sub(good, srcs)
assert answered == 2 and [r.name for r in new] == ["Severance.S02E06.1080p.WEB", "Severance.S02E07.1080p.WEB"], (answered, new)
assert new[0].seeders == 30, "duplicates merge to the best-seeded copy"
assert F.check_sub(good, [Source("c", "C", "TV", boom)]) == ([], 0), "nothing answered: the caller can tell"
t0 = time.monotonic()
slow = Source("s", "S", "TV", lambda q: (time.sleep(3), [])[1])
assert F.check_sub(good, [slow], timeout=0.3) == ([], 0) and time.monotonic() - t0 < 2.5, "a stuck source can't hang a check"
print("follow ok")

"""Following shows: remember a series, then look for episodes newer than the last one you have.

A subscription is a plain dict kept in subscriptions.json:
  id (normalized query) · title · query · group (TV|Anime) · res (0 = any) · last [season, episode]
  · auto (grab new episodes without asking) · checked (unix time of the last answered check)
"""

from __future__ import annotations

import json
import queue
import re
import time

from .aria2 import STATE_DIR
from .sources import Result, Search, dedupe, parse_release

SUBS_FILE = STATE_DIR / "subscriptions.json"
CHECK_EVERY = 4 * 3600  # seconds between automatic checks of one show

_SXE = re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})\b", re.I)
_NXN = re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.I)
_ANIME = re.compile(r"(?:\s-\s|\bEP?\.?\s?)(\d{1,4})(?:v\d)?(?=[\s\[(._]|$)", re.I)  # " - 12", "E12"
_RANGE = re.compile(r"(?<![a-z])E\d{1,3}\s?-\s?E?\d{1,3}\b", re.I)  # S01E01-E08: a batch, not one episode


def episode_of(name: str) -> tuple[int, int] | None:
    """(season, episode) from a release name; anime's absolute numbering counts as season 1.
    Season packs, multi-episode batches and movies give None."""
    if _RANGE.search(name):
        return None
    for rx in (_SXE, _NXN):
        if m := rx.search(name):
            return int(m.group(1)), int(m.group(2))
    m = _ANIME.search(name)
    # " - 2024 " is a year, not episode 2024 (One Piece is past 1100, so 4 digits are fine otherwise)
    return (1, int(m.group(1))) if m and not 1900 <= int(m.group(1)) <= 2099 else None


def fmt_ep(ep) -> str:
    return f"S{int(ep[0]):02d}E{int(ep[1]):02d}"


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def show_title(name: str) -> str:
    """The series name in a release name: what precedes the episode marker."""
    s = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", name)  # leading [Group] tags
    cuts = [m.start() for rx in (_SXE, _NXN, _ANIME) if (m := rx.search(s))]
    if not cuts or not episode_of(name):
        return ""
    s = re.sub(r"[._]+", " ", s[:min(cuts)])
    s = re.sub(r"\s*\(?(?:19|20)\d{2}\)?\s*$", "", s)  # a trailing year isn't part of the name
    return re.sub(r"\s+", " ", s).strip(" -")


def make_sub(name: str, group: str) -> dict | None:
    """A subscription starting from this release (its episode is the baseline)."""
    ep, title = episode_of(name), show_title(name)
    if not ep or not title or group not in ("TV", "Anime"):
        return None
    return {"id": norm(title), "title": title, "query": title, "group": group,
            "res": parse_release(name).res, "last": list(ep), "auto": False, "checked": 0.0}


def _valid(rec) -> bool:
    return (isinstance(rec, dict) and isinstance(rec.get("id"), str) and isinstance(rec.get("query"), str)
            and isinstance(rec.get("title"), str) and rec.get("group") in ("TV", "Anime")
            and isinstance(rec.get("res"), int) and isinstance(rec.get("auto"), bool)
            and isinstance(rec.get("checked"), (int, float))
            and isinstance(rec.get("last"), list) and len(rec["last"]) == 2
            and all(isinstance(x, int) for x in rec["last"]))


def load_subs() -> list[dict]:
    try:
        data = json.loads(SUBS_FILE.read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data if _valid(r)] if isinstance(data, list) else []


def save_subs(subs: list[dict]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SUBS_FILE.write_text(json.dumps(subs, indent=1))
    except OSError:
        pass


def find_new(sub: dict, results: list[Result]) -> list[Result]:
    """Episodes newer than sub["last"], one release each (the best seeded), oldest first."""
    terms = norm(sub["query"]).split()
    best: dict[tuple[int, int], Result] = {}
    for r in results:
        ep = episode_of(r.name)
        if not ep or ep <= tuple(sub["last"]):
            continue
        hay = norm(r.name)
        if not all(t in hay for t in terms):
            continue
        if sub["res"] and parse_release(r.name).res != sub["res"]:
            continue  # same quality as what you follow; an unknown one isn't assumed to match
        cur = best.get(ep)
        if cur is None or (r.seeders, r.size) > (cur.seeders, cur.size):
            best[ep] = r
    return [best[e] for e in sorted(best)]


def check_sub(sub: dict, sources: list, timeout: float = 45.0) -> tuple[list[Result], int]:
    """Search the sources for this show. Returns (new releases, how many sources answered);
    the caller only trusts "nothing new" when at least one source did answer."""
    search = Search(sub["query"], sources)
    got: list[Result] = []
    answered = seen = 0
    deadline = time.monotonic() + timeout
    while seen < search.total and time.monotonic() < deadline:
        try:
            u = search.updates.get(timeout=1)
        except queue.Empty:
            continue
        seen += 1
        if u.results is not None:
            answered += 1
            got += u.results
    return find_new(sub, dedupe(got)), answered

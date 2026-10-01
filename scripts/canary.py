#!/usr/bin/env python3
"""Scraper canary: search every built-in source with a query that should always have hits,
and report which ones look broken. Scrapers rot silently when a site changes its markup;
this finds out before users do. Run weekly by .github/workflows/canary.yml.

  python3 scripts/canary.py        prints a markdown table, writes canary.md,
                                   exits 1 if any source is `empty` or `error`

A source that refuses the runner (Cloudflare, 403/429, DNS, resets, timeouts) is `blocked`:
shown, but never a failure, because CI IPs are blocked far more often than real users are.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from trawl.sources import SOURCES  # noqa: E402

QUERIES = {"Games": "cyberpunk", "Movies": "oppenheimer", "TV": "the office",
           "Anime": "one piece", "Books": "dune"}
DEFAULT_QUERY = "ubuntu"  # aggregators (group "Other") index everything
# eztv has no text search (browse only: "" = its latest feed); nyaa's literature category is tiny
QUERY_OVERRIDE = {"eztv": "", "nyaa-books": "book"}
BLOCKED = ("Cloudflare", "HTTP 403", "HTTP 429", "DNS lookup failed", "refused or reset",
           "timed out", "TLS error")
ICON = {"ok": "✅", "blocked": "⚠️", "empty": "❌", "error": "❌"}


def classify(count: int, error: str) -> str:
    if error:
        return "blocked" if any(b in error for b in BLOCKED) else "error"
    return "ok" if count else "empty"


def check(source, pause: float = 2.0) -> dict:
    """One source, one known query. An empty answer gets a second try before it's believed."""
    query = QUERY_OVERRIDE.get(source.id, QUERIES.get(source.group, DEFAULT_QUERY))
    t0 = time.monotonic()
    for attempt in range(2):
        count, error = 0, ""
        try:
            count = len(source.fn(query))
        except Exception as e:  # any failure is a finding, never a crash
            error = str(e) or type(e).__name__
        status = classify(count, error)
        if status in ("ok", "blocked") or attempt:
            break
        time.sleep(pause)
    return {"id": source.id, "query": query, "status": status, "count": count,
            "secs": time.monotonic() - t0, "detail": error[:120]}


def run(sources=SOURCES, workers: int = 8) -> list[dict]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(check, sources))


def failing(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] in ("empty", "error")]


def table(rows: list[dict]) -> str:
    bad, blocked = len(failing(rows)), sum(r["status"] == "blocked" for r in rows)
    head = (f"**{bad} of {len(rows)} sources look broken**" if bad else f"All {len(rows)} sources answered")
    if blocked:
        head += f" ({blocked} refused the runner — not counted)"
    lines = [head, "", "| Source | Status | Results | Time | Query / detail |", "| --- | --- | ---: | ---: | --- |"]
    rank = {"error": 0, "empty": 0, "blocked": 1, "ok": 2}  # real breaks first, then refusals
    for r in sorted(rows, key=lambda r: (rank[r["status"]], r["id"])):
        note = f"`{r['query']}`" if r["query"] else "latest feed"
        note += f" — {r['detail']}" if r["detail"] else ""
        lines.append(f"| {r['id']} | {ICON[r['status']]} {r['status']} | {r['count']} | {r['secs']:.1f}s | {note} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    rows = run()
    md = table(rows)
    print(md)
    pathlib.Path("canary.md").write_text(md)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(md)
    return 1 if failing(rows) else 0


if __name__ == "__main__":
    sys.exit(main())

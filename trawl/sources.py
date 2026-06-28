"""Torrent source scrapers + concurrent search.

Ported from torlink's 10 sources. Each source is a function `(query) -> [Result]`.
Stdlib only: urllib (HTTP), json, re, html.unescape (entities), base64 (base32
infohash), email.utils (RSS dates). 6 JSON APIs, 2 RSS, 2 HTML (1337x).
"""

from __future__ import annotations

import base64
import html
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Callable

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
]


class SourceError(Exception):
    pass


@dataclass
class Result:
    info_hash: str
    name: str
    size: int
    seeders: int
    leechers: int
    source: str
    magnet: str
    added: int | None = None
    num_files: int | None = None


@dataclass(frozen=True)
class ParsedMagnet:
    info_hash: str
    name: str
    magnet: str


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    group: str
    fn: Callable[[str], list[Result]]


@dataclass
class SourceUpdate:
    source: str
    results: list[Result] | None  # None => failed
    error: str = ""


# -- HTTP --------------------------------------------------------------------


def fetch(url: str, retries: int = 1, timeout: float = 15.0,
          headers: dict | None = None) -> str:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=h), timeout=timeout
            ) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in RETRY_STATUS and attempt < retries:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise SourceError(last) from e
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            if attempt < retries:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise SourceError(last) from e
    raise SourceError(last or "unreachable")


def fetch_json(url: str, retries: int = 1, **kw):
    try:
        return json.loads(fetch(url, retries=retries, **kw))
    except ValueError as e:
        raise SourceError(f"bad json: {e}") from e


# -- magnet / size helpers ---------------------------------------------------


def build_magnet(info_hash: str, name: str) -> str:
    dn = urllib.parse.quote(name)
    tr = "".join(f"&tr={urllib.parse.quote(t)}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={dn}{tr}"


def normalize_info_hash(raw: str) -> str:
    if len(raw) == 32:  # base32 -> 40-hex
        try:
            return base64.b32decode(raw.upper()).hex()
        except Exception:
            return raw.lower()
    return raw.lower()


_MAGNET_RE = re.compile(r"xt=urn:btih:([a-f0-9]{40}|[a-z2-7]{32})", re.I)


def parse_magnet(s: str) -> ParsedMagnet | None:
    s = s.strip()
    if not s.lower().startswith("magnet:?"):
        return None
    m = _MAGNET_RE.search(s)
    if not m:
        return None
    info_hash = normalize_info_hash(m.group(1))
    name = info_hash
    dn = urllib.parse.parse_qs(urllib.parse.urlsplit(s).query).get("dn")
    if dn:
        name = dn[0]
    return ParsedMagnet(info_hash, name, s)


_SIZE_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
               "TIB": 1024 ** 4, "KB": 1000, "MB": 10 ** 6, "GB": 10 ** 9, "TB": 10 ** 12}
_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?I?B)", re.I)


def parse_size(s: str) -> int:
    m = _SIZE_RE.search(s or "")
    return round(float(m.group(1)) * _SIZE_UNITS.get(m.group(2).upper(), 1)) if m else 0


def _int(s) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _iso_unix(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _rfc822_unix(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError):
        return None


# -- RSS helpers (regex, mirroring torlink) ----------------------------------


def _rss_items(xml: str) -> list[str]:
    return xml.split("<item>")[1:]


def _tag(item: str, name: str) -> str:
    m = re.search(
        rf"<{re.escape(name)}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(name)}>",
        item, re.S)
    return m.group(1).strip() if m else ""


# -- sources: JSON -----------------------------------------------------------

_YTS_HOSTS = ["yts.mx", "yts.am", "yts.rs"]


def _yts(query: str) -> list[Result]:
    q = query.strip()
    params = {"limit": "50"}
    params["query_term" if q else "sort_by"] = q or "date_added"
    qs = urllib.parse.urlencode(params)
    data, last = None, None
    for host in _YTS_HOSTS:
        try:
            data = fetch_json(f"https://{host}/api/v2/list_movies.json?{qs}")
            break
        except SourceError as e:
            last = e
    if data is None:
        raise last or SourceError("YTS unreachable")
    out = []
    for movie in (data.get("data") or {}).get("movies") or []:
        base = movie.get("title_long") or movie.get("title") or "Unknown"
        for t in movie.get("torrents") or []:
            h = (t.get("hash") or "").lower()
            if not h:
                continue
            tag = " ".join(x for x in (t.get("quality"), t.get("type")) if x)
            name = f"{base} [{tag}]" if tag else base
            out.append(Result(h, name, t.get("size_bytes") or 0, t.get("seeds") or 0,
                              t.get("peers") or 0, "yts", build_magnet(h, name),
                              movie.get("date_uploaded_unix")))
    return out


_TPB = "https://apibay.org"
_TPB_MOVIE_CATS = {201, 202, 207, 209}
_TPB_TV_CATS = {205, 208}
_ZERO_HASH = "0" * 40


def _tpb(query: str, cats: set[int], browse: str, source: str) -> list[Result]:
    q = query.strip()
    url = f"{_TPB}/q.php?q={urllib.parse.quote(q)}" if q else browse
    items = fetch_json(url)
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if q and _int(it.get("category")) not in cats:
            continue
        h = (it.get("info_hash") or "").lower()
        if not h or h == _ZERO_HASH or it.get("id") == "0":
            continue
        name = it.get("name") or "Unknown"
        nf = _int(it.get("num_files"))
        out.append(Result(h, name, _int(it.get("size")), _int(it.get("seeders")),
                          _int(it.get("leechers")), source, build_magnet(h, name),
                          _int(it.get("added")) or None, nf if nf > 0 else None))
    return out


def _tpb_movies(q: str) -> list[Result]:
    return _tpb(q, _TPB_MOVIE_CATS, f"{_TPB}/precompiled/data_top100_207.json", "tpb-movies")


def _tpb_tv(q: str) -> list[Result]:
    return _tpb(q, _TPB_TV_CATS, f"{_TPB}/precompiled/data_top100_208.json", "tpb-tv")


def _eztv(query: str) -> list[Result]:
    if query.strip():  # EZTV API has no text search — browse only
        return []
    data = fetch_json("https://eztvx.to/api/get-torrents?limit=100&page=1")
    out = []
    for t in data.get("torrents") or []:
        h = (t.get("hash") or "").lower()
        if not h:
            continue
        name = t.get("title") or t.get("filename") or h
        magnet = t.get("magnet_url") or build_magnet(h, name)
        out.append(Result(h, name, _int(t.get("size_bytes")), t.get("seeds") or 0,
                          t.get("peers") or 0, "eztv", magnet, t.get("date_released_unix")))
    return out


def _solid(query: str) -> list[Result]:
    q = query.strip() or "tv show"
    data = fetch_json(f"https://solidtorrents.net/api/v1/search?q={urllib.parse.quote(q)}")
    out = []
    for it in data.get("results") or []:
        h = (it.get("infohash") or "").lower()
        if not h:
            continue
        out.append(Result(h, it.get("title") or "Unknown", it.get("size") or 0,
                          it.get("seeders") or 0, it.get("leechers") or 0, "solid",
                          build_magnet(h, it.get("title") or "Unknown"),
                          _iso_unix(it.get("updatedAt"))))
    return out


_SP_RES = ["1080", "720", "480"]


def _sp_pick(downloads: list[dict]) -> dict | None:
    for res in _SP_RES:
        for d in downloads:
            if d.get("res") == res and d.get("magnet"):
                return d
    for d in downloads:
        if d.get("magnet"):
            return d
    return None


def _subsplease(query: str) -> list[Result]:
    q = query.strip()
    params = {"tz": "UTC", "f": "search", "s": q} if q else {"tz": "UTC", "f": "latest"}
    data = fetch_json(f"https://subsplease.org/api/?{urllib.parse.urlencode(params)}")
    if not isinstance(data, dict):
        return []
    out = []
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        dl = _sp_pick(entry.get("downloads") or [])
        if not dl:
            continue
        parsed = parse_magnet(dl.get("magnet", ""))
        if not parsed:
            continue
        show = entry.get("show") or "Unknown"
        ep = f" - {entry['episode']}" if entry.get("episode") else ""
        m = re.search(r"[?&]xl=(\d+)", dl.get("magnet", ""))
        out.append(Result(parsed.info_hash, f"{show}{ep} [{dl.get('res', '?')}p]",
                          int(m.group(1)) if m else 0, 0, 0, "subsplease", parsed.magnet,
                          _iso_unix(entry.get("release_date"))))
    return out


# -- sources: RSS ------------------------------------------------------------


def _fitgirl(query: str) -> list[Result]:
    q = query.strip()
    url = (f"https://fitgirl-repacks.site/?s={urllib.parse.quote(q)}&feed=rss2"
           if q else "https://fitgirl-repacks.site/feed/")
    out = []
    for item in _rss_items(fetch(url)):
        m = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', item, re.I)
        if not m:
            continue
        magnet = html.unescape(m.group(1))
        hm = re.search(r"urn:btih:([a-zA-Z0-9]+)", magnet)
        if not hm:
            continue
        name = html.unescape(_tag(item, "title") or "Unknown Title")
        out.append(Result(hm.group(1).lower(), name, 0, 0, 0, "fitgirl", magnet,
                          _rfc822_unix(_tag(item, "pubDate"))))
    return out


def _nyaa(query: str) -> list[Result]:
    params = {"page": "rss", "q": query.strip(), "c": "0_0", "f": "0"}
    out = []
    for item in _rss_items(fetch(f"https://nyaa.si/?{urllib.parse.urlencode(params)}")):
        h = _tag(item, "nyaa:infoHash").lower()
        name = html.unescape(_tag(item, "title"))
        if not h or not name:
            continue
        out.append(Result(h, name, parse_size(_tag(item, "nyaa:size")),
                          _int(_tag(item, "nyaa:seeders")), _int(_tag(item, "nyaa:leechers")),
                          "nyaa", build_magnet(h, name), _rfc822_unix(_tag(item, "pubDate"))))
    return out


# -- sources: HTML (1337x, two-step) -----------------------------------------

_X_HOSTS = ["1337x.to", "1337x.st", "x1337x.ws", "1337xx.to"]
_X_MAX = 8
_X_STOP = {"the", "a", "an", "of", "and", "or", "to"}


def _x_rows(page: str) -> list[dict]:
    i = page.find("table-list")
    if i < 0:
        return []
    rows = []
    for tr in re.split(r"<tr[\s>]", page[i:], flags=re.I)[1:]:
        link = re.search(r'href="(/torrent/[^"]+)"[^>]*>([^<]+)</a>', tr, re.I)
        if not link:
            continue
        size = re.search(r'class="coll-4 size[^"]*">\s*([\d.]+\s*[KMGT]i?B)', tr, re.I)
        seeds = re.search(r'class="coll-2 seeds[^"]*">\s*(\d+)', tr, re.I)
        leech = re.search(r'class="coll-3 leeches[^"]*">\s*(\d+)', tr, re.I)
        rows.append({
            "name": html.unescape(link.group(2).strip()),
            "path": link.group(1),
            "seeders": _int(seeds.group(1)) if seeds else 0,
            "leechers": _int(leech.group(1)) if leech else 0,
            "size": parse_size(size.group(1) if size else ""),
        })
    return rows


def _x_magnet(base: str, path: str) -> str | None:
    try:
        page = fetch(f"{base}{path}", retries=1)
    except SourceError:
        return None
    m = re.search(r"magnet:\?xt=urn:btih:[^\"'<>\s]+", page, re.I)
    return html.unescape(m.group(0)) if m else None


def _x1337(query: str, cat: str, source: str) -> list[Result]:
    q = query.strip()
    path = (f"/category-search/{urllib.parse.quote_plus(q)}/{cat}/1/" if q
            else f"/popular-{'movies' if cat == 'Movies' else 'tv'}")
    base = page = None
    last = None
    for host in _X_HOSTS:
        try:
            cand = f"https://{host}"
            page = fetch(f"{cand}{path}", retries=2)
            base = cand
            break
        except SourceError as e:
            last = e
    if not base:
        raise last or SourceError("1337x unreachable")
    rows = _x_rows(page)
    tokens = [t for t in q.lower().split() if t]
    need = [t for t in tokens if t not in _X_STOP] or tokens
    if need:
        rows = [r for r in rows if all(t in r["name"].lower() for t in need)]
    rows.sort(key=lambda r: r["seeders"], reverse=True)
    out = []
    # ponytail: detail magnets fetched sequentially (<=8). Parallelize with daemon
    # threads if 1337x latency becomes the bottleneck.
    for r in rows[:_X_MAX]:
        magnet = _x_magnet(base, r["path"])
        if not magnet:
            continue
        hm = re.search(r"urn:btih:([a-zA-Z0-9]+)", magnet, re.I)
        if not hm:
            continue
        out.append(Result(hm.group(1).lower(), r["name"], r["size"], r["seeders"],
                          r["leechers"], source, magnet))
    return out


# -- registry ----------------------------------------------------------------

SOURCES: list[Source] = [
    Source("fitgirl", "FitGirl", "Games", _fitgirl),
    Source("yts", "YTS", "Movies", _yts),
    Source("tpb-movies", "TPB", "Movies", _tpb_movies),
    Source("x1337-movies", "1337x", "Movies", lambda q: _x1337(q, "Movies", "x1337-movies")),
    Source("eztv", "EZTV", "TV", _eztv),
    Source("solid", "Solid", "TV", _solid),
    Source("tpb-tv", "TPB", "TV", _tpb_tv),
    Source("x1337-tv", "1337x", "TV", lambda q: _x1337(q, "TV", "x1337-tv")),
    Source("nyaa", "Nyaa", "Anime", _nyaa),
    Source("subsplease", "SubsPlease", "Anime", _subsplease),
]


# -- merge -------------------------------------------------------------------


def dedupe(results: list[Result]) -> list[Result]:
    """One entry per infohash, keeping the highest seeder count."""
    by_hash: dict[str, Result] = {}
    for r in results:
        ex = by_hash.get(r.info_hash)
        if ex is None or r.seeders > ex.seeders:
            by_hash[r.info_hash] = r
    return list(by_hash.values())


def sort_results(results: list[Result]) -> list[Result]:
    return sorted(results, key=lambda r: (r.seeders, r.added or 0), reverse=True)


# -- concurrent search -------------------------------------------------------


class Search:
    """Fan out a query to every source on daemon threads; drain `updates` (a
    queue.Queue of SourceUpdate) each tick. Daemon threads => quitting never
    blocks on an in-flight fetch (the per-call timeout bounds them anyway)."""

    def __init__(self, query: str, sources: list[Source] = SOURCES):
        self.updates: queue.Queue[SourceUpdate] = queue.Queue()
        self.total = len(sources)
        for s in sources:
            threading.Thread(target=self._run, args=(s, query), daemon=True).start()

    def _run(self, s: Source, query: str) -> None:
        try:
            self.updates.put(SourceUpdate(s.id, s.fn(query)))
        except Exception as e:  # one source's failure never sinks the search
            self.updates.put(SourceUpdate(s.id, None, str(e) or type(e).__name__))


def search_all(query: str, timeout: float = 25.0) -> list[Result]:
    """Blocking concurrent search → merged, deduped, seeder-sorted results."""
    s = Search(query)
    out: list[Result] = []
    got = 0
    end = time.monotonic() + timeout
    while got < s.total:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        try:
            u = s.updates.get(timeout=remaining)
        except queue.Empty:
            break
        got += 1
        if u.results:
            out.extend(u.results)
    return sort_results(dedupe(out))


# -- self-check --------------------------------------------------------------


def selftest() -> None:
    # pure logic — deterministic, offline
    assert parse_size("1.5 GB") == 1_500_000_000
    assert parse_size("700 MiB") == 700 * 1024 ** 2
    assert parse_size("") == 0
    h40 = "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
    pm = parse_magnet(build_magnet(h40, "Some Movie 2024"))
    assert pm and pm.info_hash == h40 and pm.name == "Some Movie 2024", pm
    b32 = base64.b32encode(bytes.fromhex(h40)).decode()
    assert normalize_info_hash(b32) == h40, "base32->hex"
    assert parse_magnet("not a magnet") is None
    merged = dedupe([
        Result(h40, "lo", 1, 5, 0, "a", "m"),
        Result(h40, "hi", 1, 50, 0, "b", "m"),
    ])
    assert len(merged) == 1 and merged[0].seeders == 50, "dedupe keeps higher seeders"
    order = sort_results([
        Result("1", "a", 0, 10, 0, "x", "m"),
        Result("2", "b", 0, 99, 0, "x", "m"),
    ])
    assert order[0].seeders == 99, "sort by seeders desc"
    x_page = (
        '<table class="table-list"><tbody><tr>'
        '<td class="coll-1 name"><a href="/sort-here/">x</a>'
        '<a href="/torrent/42/The-Matrix-1999/">The Matrix 1999</a></td>'
        '<td class="coll-2 seeds">1234</td>'
        '<td class="coll-3 leeches">56</td>'
        '<td class="coll-4 size">1.5 GB<span>x</span></td></tr></tbody></table>'
    )
    xr = _x_rows(x_page)
    assert len(xr) == 1 and xr[0]["name"] == "The Matrix 1999", xr
    assert xr[0]["seeders"] == 1234 and xr[0]["leechers"] == 56, xr
    assert xr[0]["size"] == 1_500_000_000 and xr[0]["path"] == "/torrent/42/The-Matrix-1999/", xr
    print("pure logic ok")

    # live — best-effort; proves the pipeline + real parsing without requiring
    # every site to be up. Asserts every returned row is well-formed.
    s = Search("the matrix")
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    got = 0
    end = time.monotonic() + 25
    while got < s.total and time.monotonic() < end:
        try:
            u = s.updates.get(timeout=max(0.1, end - time.monotonic()))
        except queue.Empty:
            break
        got += 1
        if u.results is None:
            errors[u.source] = u.error
        else:
            counts[u.source] = len(u.results)
            for r in u.results:
                assert re.fullmatch(r"[a-f0-9]{40}", r.info_hash), f"bad hash from {u.source}: {r.info_hash!r}"
                assert r.magnet.lower().startswith("magnet:?"), f"bad magnet from {u.source}"
    print(f"sources answered: {got}/{s.total}")
    for sid, n in sorted(counts.items()):
        print(f"  {sid:14} {n} results")
    for sid, err in sorted(errors.items()):
        print(f"  {sid:14} ERROR: {err[:60]}")
    total = sum(counts.values())
    if total == 0:
        print("\n[warn] no live results — sources blocked/offline or no network.")
    else:
        print(f"\nPhase 2 selftest passed — {total} results, all well-formed.")


if __name__ == "__main__":
    selftest()

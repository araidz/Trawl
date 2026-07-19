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

from .aria2 import STATE_DIR

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

_TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
_TRACKERS_CACHE = STATE_DIR / "trackers.txt"
_TRACKERS_TTL = 7 * 86400  # refresh weekly


def _parse_trackers(text: str) -> list[str]:
    return [ln for ln in (ln.strip() for ln in text.splitlines())
            if ln.startswith(("udp://", "http://", "https://", "wss://"))]


def refresh_trackers() -> None:
    """Swap TRACKERS for a fresh ngosang/trackerslist copy, cached a week on
    disk. Never blocks a search (call it from a daemon thread); any failure
    leaves the hardcoded fallback list in place."""
    try:
        if (time.time() - _TRACKERS_CACHE.stat().st_mtime) < _TRACKERS_TTL:
            fresh = _parse_trackers(_TRACKERS_CACHE.read_text())
            if fresh:
                TRACKERS[:] = fresh
                return
    except OSError:
        pass
    try:
        text = fetch(_TRACKERS_URL, timeout=10)
        fresh = _parse_trackers(text)
        if fresh:
            TRACKERS[:] = fresh
            _TRACKERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _TRACKERS_CACHE.write_text(text)
    except (SourceError, OSError):
        pass  # keep the hardcoded fallback


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
    page: str | None = None  # the torrent's web page, to open in a browser
    group: str | None = None  # per-result category (aggregators span groups)


@dataclass(frozen=True)
class ParsedMagnet:
    info_hash: str
    name: str
    magnet: str  # the URI handed to aria2: a magnet or an http(s) link
    kind: str = "magnet"  # magnet | link | torrent (.torrent link)


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    group: str
    fn: Callable[[str], list[Result]]
    browse: bool = True  # False = search-only (no empty-query latest feed)


@dataclass
class SourceUpdate:
    source: str
    results: list[Result] | None  # None => failed
    error: str = ""


# -- HTTP --------------------------------------------------------------------


def fetch(url: str, retries: int = 1, timeout: float = 15.0,
          headers: dict | None = None, data: bytes | None = None) -> str:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=h, data=data), timeout=timeout
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


def parse_source(s: str) -> ParsedMagnet | None:
    """A grabbable input: a magnet or a direct http(s) link. Both go straight to
    aria2's addUri; the name is only a UI label. info_hash is "" for links."""
    s = s.strip()
    pm = parse_magnet(s)
    if pm:
        return pm
    if s.lower().startswith(("http://", "https://")):
        p = urllib.parse.urlparse(s)
        name = urllib.parse.unquote(p.path.rsplit("/", 1)[-1]) or p.netloc or s
        kind = "torrent" if p.path.lower().endswith(".torrent") else "link"
        return ParsedMagnet("", name, s, kind)
    return None


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
                              movie.get("date_uploaded_unix"), page=movie.get("url")))
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
                          _int(it.get("added")) or None, nf if nf > 0 else None,
                          page=f"https://thepiratebay.org/description.php?id={it.get('id')}"))
    return out


def _tpb_movies(q: str) -> list[Result]:
    return _tpb(q, _TPB_MOVIE_CATS, f"{_TPB}/precompiled/data_top100_207.json", "tpb-movies")


def _tpb_tv(q: str) -> list[Result]:
    return _tpb(q, _TPB_TV_CATS, f"{_TPB}/precompiled/data_top100_208.json", "tpb-tv")


_TPB_BOOK_CATS = {601, 602}  # 601 e-books, 602 comics


def _tpb_books(q: str) -> list[Result]:
    return _tpb(q, _TPB_BOOK_CATS, f"{_TPB}/precompiled/data_top100_601.json", "tpb-books")


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
                          t.get("peers") or 0, "eztv", magnet, t.get("date_released_unix"),
                          page=t.get("episode_url")))
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
                          _iso_unix(it.get("updatedAt")),
                          page=f"https://solidtorrents.net/view/{it['_id']}" if it.get("_id") else None))
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
                          _iso_unix(entry.get("release_date")),
                          page=f"https://subsplease.org/shows/{entry['page']}" if entry.get("page") else None))
    return out


# -- sources: RSS ------------------------------------------------------------


def _fitgirl(query: str) -> list[Result]:
    """FitGirl repacks: magnet links live in the WordPress RSS items."""
    q = query.strip()
    home = "https://fitgirl-repacks.site"
    url = f"{home}/?s={urllib.parse.quote(q)}&feed=rss2" if q else f"{home}/feed/"
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
        out.append(Result(normalize_info_hash(hm.group(1)), name, 0, 0, 0, "fitgirl", magnet,
                          _rfc822_unix(_tag(item, "pubDate")), page=_tag(item, "link") or None))
    return out


def _nyaa(query: str, cat: str = "0_0", source: str = "nyaa") -> list[Result]:
    params = {"page": "rss", "q": query.strip(), "c": cat, "f": "0"}
    out = []
    for item in _rss_items(fetch(f"https://nyaa.si/?{urllib.parse.urlencode(params)}")):
        h = _tag(item, "nyaa:infoHash").lower()
        name = html.unescape(_tag(item, "title"))
        if not h or not name:
            continue
        vid = re.search(r"/(?:view|download)/(\d+)", _tag(item, "link"))
        page = f"https://nyaa.si/view/{vid.group(1)}" if vid else None
        out.append(Result(h, name, parse_size(_tag(item, "nyaa:size")),
                          _int(_tag(item, "nyaa:seeders")), _int(_tag(item, "nyaa:leechers")),
                          source, build_magnet(h, name), _rfc822_unix(_tag(item, "pubDate")),
                          page=page))
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
    rows = rows[:_X_MAX]
    # detail pages fetched in parallel; order preserved, failures dropped
    magnets: list[str | None] = [None] * len(rows)
    def _get(i: int, path: str) -> None:
        magnets[i] = _x_magnet(base, path)
    threads = [threading.Thread(target=_get, args=(i, r["path"]), daemon=True)
               for i, r in enumerate(rows)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    out = []
    for r, magnet in zip(rows, magnets):
        if not magnet:
            continue
        hm = re.search(r"urn:btih:([a-zA-Z0-9]+)", magnet, re.I)
        if not hm:
            continue
        out.append(Result(hm.group(1).lower(), r["name"], r["size"], r["seeders"],
                          r["leechers"], source, magnet, page=base + r["path"]))
    return out


# -- sources: aggregators ----------------------------------------------------

_KNABEN_API = "https://api.knaben.eu/v1"


def _knaben_group(category: str | None) -> str | None:
    c = category or ""
    if c.startswith("XXX"):
        return "SKIP"
    if "Book" in c or "Comic" in c:
        return "Books"
    for token in ("Anime", "Movies", "TV", "Games"):
        if token in c:
            return token
    return None


def _knaben(query: str) -> list[Result]:
    q = query.strip()
    if not q:  # meta-aggregator: search only, no browse
        return []
    payload = json.dumps({"query": q, "search_type": "score", "size": 100,
                          "hide_unsafe": True, "hide_xxx": True}).encode()
    data = fetch_json(_KNABEN_API, data=payload, headers={"Content-Type": "application/json"})
    out = []
    for h in data.get("hits") or []:
        ih = (h.get("hash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", ih):
            continue
        grp = _knaben_group(h.get("category"))
        if grp == "SKIP":  # drop XXX
            continue
        name = h.get("title") or "Unknown"
        out.append(Result(ih, name, _int(h.get("bytes")), _int(h.get("seeders")),
                          _int(h.get("peers")), "knaben",
                          h.get("magnetUrl") or build_magnet(ih, name),
                          _iso_unix(h.get("date")), page=h.get("details"), group=grp))
    return out


def _animetosho(query: str) -> list[Result]:
    params = {"only_tor": "1"}
    if query.strip():
        params["q"] = query.strip()
    data = fetch_json(f"https://feed.animetosho.org/json?{urllib.parse.urlencode(params)}")
    if not isinstance(data, list):
        return []
    out = []
    for a in data:
        ih = (a.get("info_hash") or "").lower()
        if not ih:
            continue
        name = a.get("title") or "Unknown"
        nf = _int(a.get("num_files"))
        out.append(Result(ih, name, _int(a.get("total_size")), _int(a.get("seeders")),
                          _int(a.get("leechers")), "animetosho",
                          a.get("magnet_uri") or build_magnet(ih, name),
                          None, nf if nf > 0 else None, page=a.get("link"), group="Anime"))
    return out


_TGX_HOSTS = ["torrentgalaxy.to", "torrentgalaxy.mx", "tgx.rs"]


def _tgx(query: str) -> list[Result]:
    q = query.strip()
    if not q:
        return []
    path = f"/get-posts/keywords:{urllib.parse.quote(q)}:/"
    page = base = last = None
    for host in _TGX_HOSTS:
        try:
            base = f"https://{host}"
            page = fetch(f"{base}{path}", retries=1)
            break
        except SourceError as e:
            last = e
    if page is None:
        raise last or SourceError("TorrentGalaxy unreachable")
    low = page.lower()
    if "just a moment" in low or "cf-chl" in low or "checking your browser" in low:
        raise SourceError("blocked by Cloudflare")
    out = []
    for block in page.split("tgxtablerow")[1:]:
        mm = re.search(r"magnet:\?xt=urn:btih:[a-z0-9]+[^\"'<>\s]*", block, re.I)
        if not mm:
            continue
        magnet = html.unescape(mm.group(0))
        hm = re.search(r"urn:btih:([a-z0-9]+)", magnet, re.I)
        if not hm:
            continue
        dn = re.search(r"[?&]dn=([^&\"'<>]+)", magnet)
        name = urllib.parse.unquote_plus(dn.group(1)) if dn else hm.group(1)
        link = re.search(r'href="(/torrent/\d+/[^"]+)"', block)
        seeds = re.search(r"color=['\"]?#?green['\"]?[^>]*>\s*<b>\s*(\d+)", block, re.I)
        leech = re.search(r"color=['\"]?#?ff0000['\"]?[^>]*>\s*<b>\s*(\d+)", block, re.I)
        size = re.search(r"(\d+(?:\.\d+)?\s*[KMGT]i?B)", block)
        out.append(Result(normalize_info_hash(hm.group(1)), html.unescape(name),
                          parse_size(size.group(1)) if size else 0,
                          _int(seeds.group(1)) if seeds else 0,
                          _int(leech.group(1)) if leech else 0, "torrentgalaxy", magnet,
                          page=(base + link.group(1)) if link else None))
    return out


# -- sources: libgen (direct-download library, not a tracker) ----------------

_LG_HOSTS = ["libgen.li", "libgen.vg", "libgen.la"]


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _book_name(ext: str, title: str, author: str) -> str:
    """Book label with the format up front so pdf/epub/etc. is unmistakable."""
    tag = f"[{ext.upper()}] " if ext else ""
    return tag + title + (f" — {author}" if author else "")


def _libgen(query: str) -> list[Result]:
    """Library Genesis: HTML search; download is a direct http link via
    get.php?md5= (aria2 follows the CDN redirect and keeps the real filename).
    info_hash carries the md5 (dedupe key, not a btih); it's a link, not a magnet.
    ponytail: rotating domains + HTML scrape — breaks if libgen reshuffles its
    markup/hosts; upgrade path is a stable JSON API if one appears."""
    q = query.strip()
    if not q:  # a library, not a feed — search only, no browse
        return []
    page = host = last = None
    for h in _LG_HOSTS:
        try:
            host = h
            page = fetch(f"https://{h}/index.php?req={urllib.parse.quote_plus(q)}", retries=1, timeout=12)
            break
        except SourceError as e:
            last = e
    if page is None:
        raise last or SourceError("libgen unreachable")
    out = []
    for row in re.split(r"<tr[\s>]", page):
        mm = re.search(r"get\.php\?md5=([a-fA-F0-9]{32})", row)
        if not mm:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        dl = next((i for i, c in enumerate(cells) if "get.php?md5=" in c), -1)
        if dl < 2:
            continue
        md5 = mm.group(1).lower()
        gt = re.search(r'color="gray">.*?<br>\s*(.*?)\s*<br>', cells[0], re.S)
        at = re.search(r"<a [^>]*>([^<]+)</a>", cells[0])
        title = html.unescape(_strip_tags(gt.group(1) if gt else (at.group(1) if at else "")))
        if not title:
            continue
        author = html.unescape(_strip_tags(cells[1]))
        ext = _strip_tags(cells[dl - 1]).lower()
        size = parse_size(_strip_tags(cells[dl - 2]))
        name = _book_name(ext, title, author)
        out.append(Result(md5, name, size, 0, 0, "libgen",
                          f"https://{host}/get.php?md5={md5}",
                          page=f"https://{host}/ads.php?md5={md5}", group="Books"))
    return out


_ANNAS_HOSTS = ["annas-archive.org", "annas-archive.se", "annas-archive.gl"]


def _annas(query: str) -> list[Result]:
    """Anna's Archive: the aggregator (libgen forks + z-library + more) — real
    book search by title/author, with format + size. Grab attempts a direct
    libgen download; `page` is the Anna's record for browser download of what
    libgen doesn't host directly.
    ponytail: HTML scrape of a Cloudflare-fronted site; the mirror list + graceful
    per-source failure absorb host churn. Direct download only for libgen-hosted
    files — press o to open the record for everything else."""
    q = query.strip()
    if not q:  # search-only; no browse feed
        return []
    page = host = last = None
    for h in _ANNAS_HOSTS:
        try:
            host = h
            page = fetch(f"https://{h}/search?q={urllib.parse.quote_plus(q)}", retries=1, timeout=12)
            break
        except SourceError as e:
            last = e
    if page is None:
        raise last or SourceError("Anna's Archive unreachable")
    out = []
    for m in re.finditer(r'<a href="/md5/([a-f0-9]{32})"[^>]*text-lg[^>]*>(.*?)</a>', page, re.S):
        md5 = m.group(1)
        title = html.unescape(_strip_tags(m.group(2)))
        if not title:
            continue
        tail = page[m.end():m.end() + 4000]
        au = re.search(r"user-edit[^>]*></span>\s*(.*?)</a>", tail, re.S)
        author = html.unescape(_strip_tags(au.group(1))) if au else ""
        meta = re.search(r'font-semibold text-sm[^"]*"[^>]*>(.*?)</div>', tail, re.S)
        ext = size = ""
        if meta:
            fields = [f.strip() for f in html.unescape(_strip_tags(meta.group(1))).split("·")]
            if len(fields) > 1 and re.fullmatch(r"[A-Za-z0-9]{2,5}", fields[1]):
                ext = fields[1]
            size = next((f for f in fields if re.match(r"[\d.]+\s*[KMGT]B", f, re.I)), "")
        out.append(Result(md5, _book_name(ext, title, author), parse_size(size), 0, 0,
                          "annas", f"https://libgen.li/get.php?md5={md5}",
                          page=f"https://{host}/md5/{md5}", group="Books"))
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
    Source("animetosho", "AnimeTosho", "Anime", _animetosho),
    Source("tpb-books", "TPB", "Books", _tpb_books),
    Source("nyaa-books", "Nyaa", "Books", lambda q: _nyaa(q, "3_1", "nyaa-books")),
    Source("libgen", "LibGen", "Books", _libgen, browse=False),
    Source("annas", "Anna's", "Books", _annas, browse=False),
    Source("knaben", "Knaben", "Other", _knaben, browse=False),
    Source("torrentgalaxy", "TGx", "Other", _tgx, browse=False),
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
    # parse_source: magnet passes through; http(s) becomes a grabbable link with
    # a filename label; anything else (a search query) is not grabbable.
    assert parse_source(build_magnet(h40, "x")).kind == "magnet"
    lk = parse_source("https://example.com/files/My%20Book.epub")
    assert lk and lk.info_hash == "" and lk.name == "My Book.epub" and lk.kind == "link", lk
    assert parse_source("https://example.com").name == "example.com"
    assert parse_source("https://x.org/a/b.torrent").kind == "torrent"
    assert parse_source("oppenheimer 2023") is None
    merged = dedupe([
        Result(h40, "lo", 1, 5, 0, "a", "m"),
        Result(h40, "hi", 1, 50, 0, "b", "m"),
    ])
    assert len(merged) == 1 and merged[0].seeders == 50, "dedupe keeps higher seeders"
    # browse flag: only search-only sources are excluded from empty-query Latest
    assert {s.id for s in SOURCES if not s.browse} == {"libgen", "annas", "knaben", "torrentgalaxy"}, \
        [s.id for s in SOURCES if not s.browse]
    # tracker-list parse: keeps only announce urls, junk lines dropped
    tl = _parse_trackers("udp://a:1/announce\n\n# comment\nhttps://b/announce\nnot a url\n")
    assert tl == ["udp://a:1/announce", "https://b/announce"], tl
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
    # knaben category -> group mapping
    assert _knaben_group("Movies / HD") == "Movies"
    assert _knaben_group("PC / Games") == "Games"
    assert _knaben_group("Anime / Subbed") == "Anime"
    assert _knaben_group("XXX / Video") == "SKIP"
    assert _knaben_group("Books / EBooks") == "Books"
    # TGx + FitGirl parsers (synthetic pages; fetch monkeypatched — real sites unverified here)
    _g = globals()
    _of = _g["fetch"]
    _g["fetch"] = lambda *a, **k: (
        'x<div class="tgxtablerow">'
        '<a href="/torrent/55/The-Matrix/">The Matrix</a>'
        '<a href="magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c'
        '&dn=The+Matrix+1999">m</a>'
        "<font color='green'><b>1234</b></font>"
        "<font color='#ff0000'><b>56</b></font>"
        '<span class="badge">1.5 GB</span></div>')
    tg = _tgx("matrix")
    assert len(tg) == 1 and tg[0].name == "The Matrix 1999", tg
    assert tg[0].seeders == 1234 and tg[0].leechers == 56 and tg[0].size == 1_500_000_000, tg
    assert tg[0].page.endswith("/torrent/55/The-Matrix/"), tg[0].page
    _g["fetch"] = lambda *a, **k: (
        '<rss><channel><item><title>Cyberpunk 2077</title>'
        '<link>https://fitgirl-repacks.site/cyberpunk-2077/</link>'
        '<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>'
        '<description>x <a href="magnet:?xt=urn:btih:'
        'dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=cp">here</a></description>'
        '</item></channel></rss>')
    dd = _fitgirl("cyberpunk")
    assert len(dd) == 1 and dd[0].source == "fitgirl" and dd[0].name == "Cyberpunk 2077", dd
    assert dd[0].page == "https://fitgirl-repacks.site/cyberpunk-2077/", dd[0].page
    _g["fetch"] = lambda *a, **k: (
        '<table><tr>'
        '<td><a href="edition.php?id=1">x</a>'
        '<font size=1 color="gray"><br>The Hobbit <br></font></td>'
        '<td>J.R.R. Tolkien</td><td>Allen</td><td>1937</td><td>English</td><td>300</td>'
        '<td><a href="/file.php?id=1">2 MB</a></td><td>epub</td>'
        '<td><a href="/get.php?md5=aabbccddeeff00112233445566778899">Libgen</a></td>'
        '</tr></table>')
    lg = _libgen("hobbit")
    assert len(lg) == 1 and lg[0].source == "libgen" and lg[0].group == "Books", lg
    assert lg[0].info_hash == "aabbccddeeff00112233445566778899", lg[0].info_hash
    assert lg[0].name == "[EPUB] The Hobbit — J.R.R. Tolkien", lg[0].name
    assert lg[0].size == 2_000_000, lg[0].size
    assert lg[0].magnet == "https://libgen.li/get.php?md5=aabbccddeeff00112233445566778899", lg[0].magnet
    assert _libgen("") == [], "libgen browse is search-only"
    _g["fetch"] = lambda *a, **k: (
        '<a href="/md5/aabbccddeeff00112233445566778899" class="custom-a font-semibold text-lg leading-[1.2]">Sapiens: A Brief History</a>'
        '<a href="/search?q=x" class="custom-a text-sm"><span class="icon-[mdi--user-edit] text-base"></span> Yuval Noah Harari</a>'
        '<div class="text-gray-800 dark:text-slate-400 font-semibold text-sm leading-[1.2] mt-2">✅ English [en] · EPUB · 3.3MB · 2015 · 📘 Book (non-fiction)</div>')
    an = _annas("harari")
    assert len(an) == 1 and an[0].source == "annas" and an[0].group == "Books", an
    assert an[0].info_hash == "aabbccddeeff00112233445566778899", an[0].info_hash
    assert an[0].name == "[EPUB] Sapiens: A Brief History — Yuval Noah Harari", an[0].name
    assert an[0].size == 3_300_000, an[0].size
    assert an[0].magnet == "https://libgen.li/get.php?md5=aabbccddeeff00112233445566778899", an[0].magnet
    assert an[0].page == "https://annas-archive.org/md5/aabbccddeeff00112233445566778899", an[0].page
    assert _annas("") == [], "annas search-only"
    _g["fetch"] = _of
    print("pure logic ok")

    # live — best-effort; proves the pipeline + real parsing without requiring
    # every site to be up. Asserts every returned row is well-formed.
    s = Search("the matrix")
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    pages: dict[str, int] = {}
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
                # torrent rows: btih + magnet. direct-download rows (libgen): a
                # 32-hex md5 + an http link. Both must be a grabbable uri aria2 takes.
                if r.magnet.lower().startswith("magnet:?"):
                    assert re.fullmatch(r"[a-f0-9]{40}", r.info_hash), f"bad hash from {u.source}: {r.info_hash!r}"
                else:
                    assert r.magnet.lower().startswith("http"), f"bad uri from {u.source}: {r.magnet!r}"
                    assert re.fullmatch(r"[a-f0-9]{32}", r.info_hash), f"bad md5 from {u.source}: {r.info_hash!r}"
                if r.page:
                    assert r.page.startswith("http"), f"bad page from {u.source}: {r.page!r}"
                    pages[u.source] = pages.get(u.source, 0) + 1
    print(f"sources answered: {got}/{s.total}")
    for sid, n in sorted(counts.items()):
        print(f"  {sid:14} {n} results  ({pages.get(sid, 0)} with pages)")
    for sid, err in sorted(errors.items()):
        print(f"  {sid:14} ERROR: {err[:60]}")
    total = sum(counts.values())
    if total == 0:
        print("\n[warn] no live results — sources blocked/offline or no network.")
    else:
        print(f"\nPhase 2 selftest passed — {total} results, all well-formed.")


if __name__ == "__main__":
    selftest()

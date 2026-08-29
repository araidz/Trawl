"""Torrent source scrapers + concurrent search.

Ported from torlink's 10 sources. Each source is a function `(query) -> [Result]`.
Stdlib only: urllib (HTTP), json, re, html.unescape (entities), base64 (base32
infohash), email.utils (RSS dates). 6 JSON APIs, 2 RSS, 2 HTML (1337x).
"""

from __future__ import annotations

import base64
import http.client
import html
import ipaddress
import json
import math
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class ResultVariant:
    source: str
    uri: str
    page: str | None
    seeders: int
    leechers: int


@dataclass(frozen=True)
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
    variants: tuple[ResultVariant, ...] = ()


@dataclass(frozen=True)
class ParsedMagnet:
    info_hash: str
    name: str
    magnet: str  # the URI handed to aria2: a magnet or an http(s) link
    kind: str = "magnet"  # magnet | link | torrent (.torrent link)


@dataclass(frozen=True)
class TorznabFeed:
    id: str
    url: str
    api_key: str = ""


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    group: str
    fn: Callable[[str], list[Result]]
    browse: bool = True  # False = search-only (no empty-query latest feed)
    secrets: tuple[str, ...] = ()
    reports_health: bool = True  # False = swarm counts never populated (RSS/library)


@dataclass(frozen=True)
class QueryFilter:
    field: str
    comparator: str
    value: int | float | str


@dataclass(frozen=True)
class LocalQuery:
    remote: str
    filters: tuple[QueryFilter, ...] = ()
    exclusions: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    now: float = field(default_factory=time.time)


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
        except http.client.InvalidURL:
            raise SourceError("invalid URL") from None
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


# -- Torznab -----------------------------------------------------------------

_SENSITIVE_KEYS = {"api", "apikey", "key", "pass", "passkey", "password", "pwd",
                   "auth", "authorization"}


def _is_sensitive_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized in _SENSITIVE_KEYS or "token" in normalized or "secret" in normalized


def _http_candidate(raw: str, base: str = "") -> str | None:
    """Resolve one candidate without letting malformed siblings poison an item."""
    try:
        if not raw or any(ord(c) < 33 or ord(c) == 127 for c in raw):
            return None
        url = urllib.parse.urljoin(base, raw)
        p = urllib.parse.urlsplit(url)
        if p.scheme.lower() not in {"http", "https"} or not p.hostname or "@" in p.netloc:
            return None
        port = p.port
        if port is not None and not 1 <= port <= 65535:
            return None
        host = p.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.encode("idna").decode("ascii")
            if (len(ascii_host) > 253 or any(not label or len(label) > 63 or
                    not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                    for label in ascii_host.rstrip(".").split("."))):
                return None
        return url
    except (UnicodeError, ValueError, TypeError):
        return None


def validate_torznab_url(url: str) -> str:
    """Return a normalized endpoint, rejecting anything unsafe to fetch."""
    try:
        if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError
        valid = _http_candidate(url)
        if not valid:
            raise ValueError
        p = urllib.parse.urlsplit(valid)
        port = p.port
        host = p.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port:
            host += f":{port}"
        return urllib.parse.urlunsplit((p.scheme.lower(), host, p.path or "/", p.query, ""))
    except (ValueError, TypeError) as e:
        raise SourceError("invalid Torznab URL") from e


def torznab_label(url: str) -> str:
    p = urllib.parse.urlsplit(validate_torznab_url(url))
    path = p.path.rstrip("/")
    return p.netloc + (path if path and path != "/" else "")


def torznab_request_url(endpoint: str, query: str, api_key: str = "") -> str:
    p = urllib.parse.urlsplit(validate_torznab_url(endpoint))
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    seen_t = seen_q = seen_key = False
    for key, value in pairs:
        low = key.lower()
        normalized = re.sub(r"[^a-z0-9]", "", low)
        if low == "t":
            if not seen_t:
                out.append((key, "search"))
                seen_t = True
        elif low == "q":
            if not seen_q:
                out.append((key, query))
                seen_q = True
        elif api_key and normalized == "apikey":
            continue
        elif normalized == "apikey":
            if not seen_key:
                out.append(("apikey", value))
                seen_key = True
        else:
            out.append((key, value))
    if not seen_t:
        out.append(("t", "search"))
    if not seen_q:
        out.append(("q", query))
    if api_key:
        out.append(("apikey", api_key))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(out, doseq=True), ""))


def redact_url(url: str, secrets: tuple[str, ...] = ()) -> str:
    clean = "".join(c for c in url if c >= " " and c != "\x7f")
    clean = re.sub(r"(?i)(https?://)[^/?#\s]*@", r"\1***@", clean)
    clean = re.sub(
        r"([?&])([^=&#]*)(=)([^&#]*)",
        lambda m: m.group(1) + m.group(2) + m.group(3) +
        ("***" if _is_sensitive_name(urllib.parse.unquote_plus(m.group(2))) else m.group(4)),
        clean,
    )
    try:
        p = urllib.parse.urlsplit(clean)
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if p.port:
            host += f":{p.port}"
        pairs = [(k, "***" if _is_sensitive_name(k) else v)
                 for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)]
        clean = urllib.parse.urlunsplit((p.scheme, host, p.path, urllib.parse.urlencode(pairs), p.fragment))
    except ValueError:
        pass
    return redact(clean, secrets, urls=False)


def redact(text: str, secrets: tuple[str, ...] = (), *, urls: bool = True) -> str:
    clean = "".join(c for c in str(text) if c >= " " and c != "\x7f")
    if urls:
        clean = re.sub(r"https?://[^\s<>'\"]+", lambda m: redact_url(m.group(), secrets), clean,
                       flags=re.I)
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        for form in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
            clean = re.sub(re.escape(form), "***", clean, flags=re.I)
    return clean


def _optional_int(value) -> int | None:
    try:
        n = int(value)
        return n if n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _torznab_group(values: list[int]) -> str:
    if 5070 in values:
        return "Anime"
    if any(1000 <= n < 2000 or 4000 <= n < 5000 for n in values):
        return "Games"
    if any(2000 <= n < 3000 for n in values):
        return "Movies"
    if any(5000 <= n < 6000 for n in values):
        return "TV"
    if any(7000 <= n < 8000 for n in values):
        return "Books"
    return "Other"


def _http_download_like(url: str) -> bool:
    try:
        valid = _http_candidate(url)
        if not valid:
            return False
        p = urllib.parse.urlsplit(valid)
        return (
                (p.path.lower().endswith(".torrent") or
                 any(x in p.path.lower() for x in ("/download", "/get.php", "/api")) or
                 any(k.lower() in {"download", "get", "file"}
                     for k, _ in urllib.parse.parse_qsl(p.query))))
    except ValueError:
        return False


def parse_torznab(xml: str, endpoint: str, source: str = "torznab") -> list[Result]:
    label = torznab_label(endpoint)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        line, col = getattr(e, "position", (0, 0))
        raise SourceError(f"{label}: malformed XML at {line}:{col}") from None
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() == "error":
            raise SourceError(f"{label}: Torznab error {node.get('code', '')} {node.get('description', '')}".strip())
    out: list[Result] = []
    for item in (n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower() == "item"):
        children: dict[str, list[ET.Element]] = {}
        attrs: dict[str, list[str]] = {}
        for node in item:
            local = node.tag.rsplit("}", 1)[-1].lower()
            children.setdefault(local, []).append(node)
            if local == "attr":
                attrs.setdefault((node.get("name") or "").lower(), []).append(node.get("value") or "")
        title = next(((n.text or "").strip() for n in children.get("title", []) if (n.text or "").strip()), "Unknown")
        enclosures = children.get("enclosure", [])
        uri = ""
        for n in enclosures:
            raw = n.get("url", "")
            if parse_magnet(raw):
                uri = raw
                break
            absolute = _http_candidate(raw, endpoint)
            if absolute and (((n.get("type") or "").split(";", 1)[0].strip().lower() ==
                              "application/x-bittorrent") or _http_download_like(absolute)):
                uri = absolute
                break
        explicit_magnets = [item.get("magneturl", ""), *attrs.get("magneturl", [])]
        uri = uri or next((x for x in explicit_magnets if parse_magnet(x)), "")
        links = [(n.text or "").strip() for n in children.get("link", []) if (n.text or "").strip()]
        uri = uri or next((x for x in links if parse_magnet(x)), "")
        pages = [(n.text or "").strip() for tag in ("comments", "guid")
                 for n in children.get(tag, []) if (n.text or "").strip()]
        page_urls = [url for x in pages if (url := _http_candidate(x, endpoint))]
        page_keys = {urllib.parse.urldefrag(x)[0].rstrip("/") for x in page_urls}
        uri = uri or next((absolute for x in links
                           if (absolute := _http_candidate(x, endpoint)) and
                           (_http_download_like(absolute) or
                            urllib.parse.urldefrag(absolute)[0].rstrip("/") not in page_keys)), "")
        parsed = parse_magnet(uri)
        if not parsed and not _http_candidate(uri):
            continue
        info_hash = next(iter(attrs.get("infohash", []) or attrs.get("hash", [])), "")
        info_hash = normalize_info_hash(info_hash) if info_hash else (parsed.info_hash if parsed else "")
        seeders = _optional_int(next(iter(attrs.get("seeders", [])), None)) or 0
        leechers = _optional_int(next(iter(attrs.get("leechers", [])), None))
        peers = _optional_int(next(iter(attrs.get("peers", [])), None))
        if leechers is None:
            leechers = max(peers - seeders, 0) if peers is not None else 0
        tor_size = _optional_int(next(iter(attrs.get("size", [])), None))
        plain_size = _optional_int(next(((n.text or "").strip() for n in children.get("size", [])), None))
        enc_size = _optional_int(next((n.get("length") for n in enclosures if n.get("length")), None))
        size = next((n for n in (tor_size, plain_size, enc_size) if n is not None), 0)
        files = _optional_int(next(iter(attrs.get("files", []) or attrs.get("numfiles", [])), None))
        cats = [_optional_int(x) for x in attrs.get("category", [])]
        page = next(iter(page_urls), None) or next((url for x in links
                                                   if (url := _http_candidate(x, endpoint)) and url != uri), None)
        date = next(((n.text or "").strip() for n in children.get("pubdate", []) if (n.text or "").strip()), None)
        out.append(Result(info_hash, title, size, seeders, leechers, source, uri,
                          _rfc822_unix(date), files, page, _torznab_group([n for n in cats if n is not None])))
    return out


def make_torznab_source(feed: TorznabFeed) -> Source:
    endpoint = validate_torznab_url(feed.url)
    label = torznab_label(endpoint)
    query_secrets = tuple(v for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(endpoint).query)
                          if _is_sensitive_name(k) and v)
    secrets = tuple(dict.fromkeys(x for x in (feed.api_key, *query_secrets) if x))

    def search(query: str) -> list[Result]:
        if not query.strip():
            return []
        try:
            return parse_torznab(fetch(torznab_request_url(endpoint, query, feed.api_key)), endpoint, feed.id)
        except SourceError as e:
            raise SourceError(redact(str(e), secrets)[:300]) from None

    return Source(feed.id, label, "Other", search, browse=False, secrets=secrets)


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
_SIZE_RE = re.compile(r"(?<![-\d.])([\d.]+)\s*([KMGT]?I?B)", re.I)


def parse_size(s: str) -> int:
    m = _SIZE_RE.search(s or "")
    if not m:
        return 0
    try:
        value = float(m.group(1)) * _SIZE_UNITS.get(m.group(2).upper(), 1)
        return round(value) if math.isfinite(value) and 0 <= value <= sys.maxsize else 0
    except (ValueError, OverflowError):
        return 0


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
        value = parsedate_to_datetime(s)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    except (TypeError, ValueError, OverflowError):
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


def _torrentscsv(query: str) -> list[Result]:
    q = query.strip()
    if not q:  # DHT aggregator: search only, no browse feed
        return []
    data = fetch_json(f"https://torrents-csv.com/service/search?q={urllib.parse.quote(q)}&size=100")
    out = []
    for it in data.get("torrents") or []:
        h = (it.get("infohash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", h):
            continue
        name = it.get("name") or "Unknown"
        out.append(Result(h, name, _int(it.get("size_bytes")), _int(it.get("seeders")),
                          _int(it.get("leechers")), "torrents-csv", build_magnet(h, name),
                          _int(it.get("created_unix")) or None))
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
    Source("fitgirl", "FitGirl", "Games", _fitgirl, reports_health=False),
    Source("yts", "YTS", "Movies", _yts),
    Source("tpb-movies", "TPB", "Movies", _tpb_movies),
    Source("x1337-movies", "1337x", "Movies", lambda q: _x1337(q, "Movies", "x1337-movies")),
    Source("eztv", "EZTV", "TV", _eztv),
    Source("solid", "Solid", "TV", _solid),
    Source("tpb-tv", "TPB", "TV", _tpb_tv),
    Source("x1337-tv", "1337x", "TV", lambda q: _x1337(q, "TV", "x1337-tv")),
    Source("nyaa", "Nyaa", "Anime", _nyaa),
    Source("subsplease", "SubsPlease", "Anime", _subsplease, reports_health=False),
    Source("animetosho", "AnimeTosho", "Anime", _animetosho),
    Source("tpb-books", "TPB", "Books", _tpb_books),
    Source("nyaa-books", "Nyaa", "Books", lambda q: _nyaa(q, "3_1", "nyaa-books")),
    Source("libgen", "LibGen", "Books", _libgen, browse=False, reports_health=False),
    Source("annas", "Anna's", "Books", _annas, browse=False, reports_health=False),
    Source("knaben", "Knaben", "Other", _knaben, browse=False),
    Source("torrentgalaxy", "TGx", "Other", _tgx, browse=False),
    Source("torrents-csv", "TorrCSV", "Other", _torrentscsv, browse=False),
]


# -- merge -------------------------------------------------------------------


def _normalized_http(uri: str) -> str:
    try:
        p = urllib.parse.urlsplit(uri)
        if p.scheme.lower() not in {"http", "https"} or not p.hostname:
            return ""
        host = p.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if p.port:
            host += f":{p.port}"
        return urllib.parse.urlunsplit((p.scheme.lower(), host, p.path, p.query, ""))
    except ValueError:
        return ""


def result_identity(result: Result) -> str:
    parsed = parse_magnet(result.magnet)
    if parsed:
        return "btih:" + parsed.info_hash
    h = result.info_hash.strip()
    if re.fullmatch(r"[a-fA-F0-9]{32}", h) and result.magnet.lower().startswith(("http://", "https://")):
        return "md5:" + h.lower()
    if re.fullmatch(r"[a-fA-F0-9]{40}", h) or re.fullmatch(r"[A-Z2-7]{32}", h, re.I):
        return "btih:" + normalize_info_hash(h)
    uri = _normalized_http(result.magnet)
    return "uri:" + uri if uri else ""


def dedupe(results: list[Result]) -> list[Result]:
    """Merge identities; ties choose lexicographically smallest copied metadata."""
    buckets: dict[str, list[Result]] = {}
    order: list[str] = []
    for i, result in enumerate(results):
        key = result_identity(result) or f"unique:{i}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(result)
    merged: list[Result] = []
    for key in order:
        rows = buckets[key]
        primary = min(rows, key=lambda r: (-r.seeders, -r.leechers, r.source, r.magnet,
                                           (r.page is not None, r.page or ""), r.info_hash,
                                           r.name, r.size,
                                           (r.added is not None, r.added or 0),
                                           (r.num_files is not None, r.num_files or 0),
                                           (r.group is not None, r.group or "")))
        if len(rows) == 1 and not primary.variants:
            merged.append(primary)  # preserve legacy object identity for untouched rows
            continue
        variants: dict[tuple[str, str, str | None], ResultVariant] = {}
        for row in rows:
            candidates = row.variants or (ResultVariant(row.source, row.magnet, row.page,
                                                        row.seeders, row.leechers),)
            for variant in candidates:
                vk = (variant.source, variant.uri, variant.page)
                old = variants.get(vk)
                if old is None or (variant.seeders, variant.leechers) > (old.seeders, old.leechers):
                    variants[vk] = variant
        pv = ResultVariant(primary.source, primary.magnet, primary.page, primary.seeders, primary.leechers)
        ordered = [pv] + sorted((v for k, v in variants.items()
                                 if k != (pv.source, pv.uri, pv.page)),
                                key=lambda v: (-v.seeders, -v.leechers, v.source, v.uri, v.page or ""))
        merged.append(Result(primary.info_hash, primary.name, primary.size, primary.seeders,
                             primary.leechers, primary.source, primary.magnet, primary.added,
                             primary.num_files, primary.page, primary.group, tuple(ordered)))
    return merged


# -- local query language ----------------------------------------------------

_QUERY_FIELDS = {"seeders", "size", "source", "group", "age", "files"}
_QUERY_TOKEN = re.compile(r'-?"[^"\n]*"|\S+')


def parse_query(text: str, now: float | None = None) -> LocalQuery:
    now = time.time() if now is None else now
    tokens: list[str] = []
    for match in _QUERY_TOKEN.finditer(text):
        token = match.group()
        tokens.append(token)
        if '"' in token and not (token.startswith('-"') and token.endswith('"')) \
                and not (len(token) >= 2 and token[0] == token[-1] == '"'):
            tokens[-1] = text[match.start():].strip()
            break
    remote: list[str] = []
    terms: list[str] = []
    exclusions: list[str] = []
    filters: list[QueryFilter] = []
    malformed: list[str] = []
    for token in tokens:
        negative_quote = token.startswith('-"') and token.endswith('"')
        quoted = len(token) >= 2 and token[0] == token[-1] == '"'
        bad = '"' in token and not (negative_quote or quoted)
        if bad:
            malformed.append(token)
        value = token[2:-1] if negative_quote else (token[1:-1] if quoted else token)
        if not bad and (negative_quote or quoted) and not value:
            malformed.append(token)
            bad = True
        if not bad and (negative_quote or (value.startswith("-") and len(value) > 1)):
            exclusions.append(value.lower() if negative_quote else value[1:].lower())
            continue
        m = re.fullmatch(r"([A-Za-z]+):(?:(<=|>=|=|<|>))?(.+)", value)
        if m and m.group(1).lower() in _QUERY_FIELDS:
            field, op, raw = m.group(1).lower(), m.group(2), m.group(3)
            try:
                if field in {"source", "group"}:
                    if op not in {None, "="} or not raw:
                        raise ValueError
                    parsed: int | float | str = raw.lower()
                    op = "="
                elif field == "size":
                    if not re.fullmatch(r"\d+(?:\.\d+)?\s*[KMGT]?I?B", raw, re.I):
                        raise ValueError
                    number, unit = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?I?B)", raw, re.I).groups()
                    amount = float(number) * _SIZE_UNITS[unit.upper()]
                    if not math.isfinite(amount) or not 0 <= amount <= sys.maxsize:
                        raise ValueError
                    parsed = parse_size(raw)
                    op = op or ">="
                elif field == "age":
                    am = re.fullmatch(r"(\d+(?:\.\d+)?)(m|h|d|w|mo|y)", raw, re.I)
                    if not am:
                        raise ValueError
                    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800,
                            "mo": 2592000, "y": 31536000}[am.group(2).lower()]
                    parsed = float(am.group(1)) * unit
                    if not math.isfinite(parsed) or not 0 <= parsed <= sys.maxsize:
                        raise ValueError
                    op = op or ">="
                else:
                    if not re.fullmatch(r"\d+", raw):
                        raise ValueError
                    parsed = int(raw)
                    if parsed > sys.maxsize:
                        raise ValueError
                    op = op or ">="
                filters.append(QueryFilter(field, op, parsed))
                continue
            except (ValueError, OverflowError):
                malformed.append(token)
                bad = True
        elif m or (":" in value and value.split(":", 1)[0].isalpha()):
            malformed.append(token)
            bad = True
        remote.append(token if bad else value)
        if not bad:
            terms.append(value.lower())
    return LocalQuery(" ".join(remote), tuple(filters), tuple(exclusions), tuple(malformed), tuple(terms), now)


def _compare(actual: int | float, op: str, expected: int | float) -> bool:
    return {"<": actual < expected, "<=": actual <= expected, "=": actual == expected,
            ">=": actual >= expected, ">": actual > expected}[op]


def matches_query(result: Result, query: LocalQuery | str,
                  sources: dict[str, Source] | list[Source] = SOURCES) -> bool:
    query = parse_query(query) if isinstance(query, str) else query
    title = result.name.lower()
    if any(term not in title for term in query.terms) or any(term in title for term in query.exclusions):
        return False
    source_map = sources if isinstance(sources, dict) else {s.id: s for s in sources}
    for flt in query.filters:
        if flt.field == "source":
            src = source_map.get(result.source)
            if str(flt.value) not in f"{result.source} {src.label if src else ''}".lower():
                return False
        elif flt.field == "group":
            if (result.group or (source_map.get(result.source).group if source_map.get(result.source) else "")).lower() != flt.value:
                return False
        else:
            actual = {"seeders": result.seeders, "size": result.size,
                      "files": result.num_files}.get(flt.field)
            if flt.field == "age":
                actual = None if result.added is None else query.now - result.added
            if actual is None or not _compare(actual, flt.comparator, flt.value):
                return False
    return True


# -- concurrent search -------------------------------------------------------


class Search:
    """Fan out a query to every source on daemon threads; drain `updates` (a
    queue.Queue of SourceUpdate) each tick. Daemon threads => quitting never
    blocks on an in-flight fetch (the per-call timeout bounds them anyway)."""

    def __init__(self, query: str, sources: list[Source] | None = None):
        sources = SOURCES if sources is None else sources
        if len({s.id for s in sources}) != len(sources):
            raise ValueError("duplicate source id")
        self.updates: queue.Queue[SourceUpdate] = queue.Queue()
        self.total = 0
        self.query = parse_query(query)
        self.sources = {s.id: s for s in sources}
        self.in_flight: set[str] = set()
        self._lock = threading.Lock()
        for s in sources:
            self._start(s)

    def _start(self, source: Source) -> bool:
        with self._lock:
            if source.id in self.in_flight:
                return False
            self.in_flight.add(source.id)
            self.total += 1
        try:
            threading.Thread(target=self._run, args=(source,), daemon=True).start()
        except Exception as e:
            update = SourceUpdate(source.id, None,
                                  redact(str(e) or type(e).__name__, source.secrets)[:300])
            with self._lock:
                self.in_flight.discard(source.id)
                self.updates.put(update)
        return True

    def retry(self, ids) -> tuple[str, ...]:
        scheduled = []
        seen: set[str] = set()
        for source_id in ids:
            if source_id in seen:
                continue
            seen.add(source_id)
            source = self.sources.get(source_id)
            if source and self._start(source):
                scheduled.append(source_id)
        return tuple(scheduled)

    def _run(self, s: Source) -> None:
        try:
            results = [r for r in s.fn(self.query.remote)
                       if matches_query(r, self.query, self.sources)]
            update = SourceUpdate(s.id, results)
        except Exception as e:  # one source's failure never sinks the search
            update = SourceUpdate(s.id, None, redact(str(e) or type(e).__name__, s.secrets)[:300])
        with self._lock:
            self.in_flight.discard(s.id)
            self.updates.put(update)

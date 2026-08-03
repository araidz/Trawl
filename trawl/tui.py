"""Raw-ANSI terminal UI: state, render, input. No third-party deps.

Full-redraw renderer (truecolor) in torlink's look — logo header, left rail,
search/results/downloads panels, sheen progress bars, footer hints. The run
loop lives in __main__.py; this module is the view + state and is importable
without a terminal or aria2 (so render/keys are testable).
"""

from __future__ import annotations

import glob
import json
import math
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
import urllib.parse
import uuid
from collections.abc import Iterable, Sequence
from functools import cache, lru_cache

from . import theme as T
from .aria2 import STATE_DIR, Aria2Error, Download, control_infohash
from .sources import (SOURCES, LocalQuery, Result, ResultVariant, Search, TorznabFeed,
                      build_magnet, dedupe, make_torznab_source, matches_query,
                      parse_query, parse_source, redact, redact_url, result_identity, torznab_label,
                      validate_torznab_url)
from .meta import Meta, kind_for, lookup

CATS = [("all", "All"), ("games", "Games"), ("movies", "Movies"),
        ("tv", "TV"), ("anime", "Anime"), ("books", "Books")]
CAT_GROUP = {"games": "Games", "movies": "Movies", "tv": "TV", "anime": "Anime",
             "books": "Books"}

HIST_MAX = 100
HIST_FILE = STATE_DIR / "history.txt"


def load_history() -> list[str]:
    try:
        lines = [ln.strip() for ln in HIST_FILE.read_text().splitlines() if ln.strip()]
        return lines[-HIST_MAX:]
    except OSError:
        return []


def save_history(hist: list[str]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HIST_FILE.write_text("\n".join(hist[-HIST_MAX:]))
    except OSError:
        pass

DL_HIST_FILE = STATE_DIR / "downloads.jsonl"
DL_HIST_MAX = 100
CONFIG_FILE = STATE_DIR / "config.json"


def load_dl_history() -> list[dict]:
    out: list[dict] = []
    try:
        for ln in DL_HIST_FILE.read_text().splitlines()[-DL_HIST_MAX:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def append_dl_history(rec: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with DL_HIST_FILE.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg))
    except OSError:
        pass

RAIL_W = 16  # fits "Downloads (NN)"
MARGIN = 2
GAP = 2

# -- ANSI + width primitives -------------------------------------------------

RESET = "\x1b[0m"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@lru_cache(maxsize=2048)
def _fg(hexc: str) -> str:
    n = int(hexc[1:], 16)
    return f"\x1b[38;2;{(n >> 16) & 255};{(n >> 8) & 255};{n & 255}m"


def style(text: str, color: str | None = None, bold: bool = False, dim: bool = False) -> str:
    pre = ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else "") + (_fg(color) if color else "")
    return f"{pre}{text}{RESET}" if pre else text


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(s: str) -> int:
    return sum(_cw(c) for c in s)


def dtrunc(s: str, maxw: int) -> str:
    if maxw <= 0:
        return ""
    if dwidth(s) <= maxw:
        return s
    out, w = "", 0
    for ch in s:
        cw = _cw(ch)
        if w + cw > maxw - 1:
            break
        out += ch
        w += cw
    return out + "…"


def pad(s: str, w: int, align: str = "left") -> str:
    gap = w - dwidth(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def cell(text: str, w: int, align: str = "left", color: str | None = None,
         bold: bool = False, dim: bool = False) -> str:
    """A fixed-width styled cell: visible width is exactly max(0, w)."""
    if w <= 0:
        return ""
    return style(pad(dtrunc(text, w), w, align), color, bold, dim)


# -- formatters --------------------------------------------------------------


def fmt_bytes(n: float | None) -> str:
    if not n or n <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if i == 0 else f"{n:.2f} {units[i]}"


def fmt_speed(n: float | None) -> str:
    if not n or n <= 0:
        return "0 B/s"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}" if (n < 10 and i > 0) else f"{n:.0f} {units[i]}"


def fmt_eta(sec: float | None) -> str:
    if not sec or sec <= 0 or sec == float("inf"):
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    return f"{sec // 86400}d"


def fmt_rel(unix: int | None) -> str:
    if not unix:
        return "-"
    d = time.time() - unix
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 2592000:
        return f"{int(d // 86400)}d"
    return f"{int(d // 2592000)}mo"


def clean(s: str) -> str:
    s = "".join(c if c.isprintable() or c == " " else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()


def copy_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def paste_clipboard() -> str:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def reveal(path: str) -> bool:
    # reveal a file in Finder, or open a directory
    args = ["open", "-R", path] if os.path.isfile(path) else ["open", path]
    try:
        subprocess.run(args, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def remove_download_files(paths: list[str], dl_dir: str | None, name: str) -> int:
    """Delete a download's files + aria2 control files off disk; prune emptied
    subfolders (never the download dir itself). Returns files removed."""
    n = 0
    for p in paths:
        for f in (p, p + ".aria2"):
            try:
                os.remove(f)
                n += 1
            except OSError:
                pass
    if dl_dir:  # torrents keep one control file at the download-dir root
        try:
            os.remove(os.path.join(dl_dir, name + ".aria2"))
        except OSError:
            pass
    for d in sorted({os.path.dirname(p) for p in paths}, key=len, reverse=True):
        if dl_dir and os.path.abspath(d) != os.path.abspath(dl_dir):
            try:
                os.rmdir(d)  # only if now empty
            except OSError:
                pass
    return n


def open_url(url: str) -> bool:
    try:
        subprocess.run(["open", url], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def notify(title: str, message: str) -> None:
    """Fire-and-forget macOS desktop notification (never blocks the UI)."""
    script = f"display notification {json.dumps(clean(message)[:200])} with title {json.dumps(title)}"
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# -- progress bar ------------------------------------------------------------


@lru_cache(maxsize=64)
def _bar_cells(width: int, base: str) -> tuple[str, ...]:
    denom = max(1, width - 1)
    return tuple(T.progress_ramp(i / denom, T.DEEP, base, T.BRIGHT)
                 for i in range(width))


def _styled_bar(cells: tuple[str, ...] | list[str], empty: int) -> str:
    out = []
    j = 0
    while j < len(cells):  # group consecutive same-color runs to cut escapes
        k = j
        while k < len(cells) and cells[k] == cells[j]:
            k += 1
        out.append(style(T.BLOCK * (k - j), cells[j]))
        j = k
    if empty:
        out.append(style(T.TRACK * empty, T.RULE))
    return "".join(out)


@lru_cache(maxsize=128)
def _static_bar(width: int, filled: int, base: str) -> str:
    return _styled_bar(_bar_cells(width, base)[:filled], width - filled)


def render_bar(progress: float, width: int, tick: float, animate: bool,
               base: str = T.ACCENT) -> str:
    if width <= 0:
        return ""
    filled = round(max(0.0, min(1.0, progress)) * width)
    if not animate:
        return _static_bar(width, filled, base)
    cells = list(_bar_cells(width, base)[:filled])
    center = T.sheen_center(tick, T.sheen_period(width))
    lo = max(0, math.floor(center - T.SHEEN_RADIUS) + 1)
    hi = min(filled, math.ceil(center + T.SHEEN_RADIUS))
    for i in range(lo, hi):
        inten = T.sheen_intensity(i, center)
        if inten > 0:
            cells[i] = T.lerp_hex(cells[i], T.SHEEN_PEAK, inten)
    return _styled_bar(cells, width - filled)


# -- key parsing -------------------------------------------------------------

_ARROWS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}


def parse_keys(data: bytes) -> list[str]:
    keys: list[str] = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x1b:
            if data[i:i + 3] == b"\x1b[<":  # SGR mouse: \x1b[<btn;x;y(M|m)
                j = i + 3
                while j < n and data[j] not in (ord("M"), ord("m")):
                    j += 1
                parts = data[i + 3:j].split(b";")
                if parts and parts[0].isdigit():
                    btn = int(parts[0])
                    if btn == 64:  # wheel up
                        keys.append("up")
                    elif btn == 65:  # wheel down
                        keys.append("down")
                i = j + 1
            elif i + 2 < n and data[i + 1] in (ord("["), ord("O")) and bytes([data[i + 2]]) in _ARROWS:
                keys.append(_ARROWS[bytes([data[i + 2]])])
                i += 3
            else:
                keys.append("esc")
                i += 1
        elif b in (0x0d, 0x0a):
            keys.append("enter")
            i += 1
        elif b in (0x7f, 0x08):
            keys.append("backspace")
            i += 1
        elif b == 0x09:
            keys.append("tab")
            i += 1
        elif b == 0x03:
            keys.append("ctrl-c")
            i += 1
        elif b < 0x20:
            i += 1
        else:
            j = i
            while j < n and data[j] >= 0x20 and data[j] != 0x1b:
                j += 1
            keys.extend(data[i:j].decode("utf-8", "ignore"))
            i = j
    return keys


# -- terminal ----------------------------------------------------------------


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = None
        self._lines: tuple[str, ...] | None = None
        self._size: tuple[int, int] | None = None

    def enter(self) -> None:
        self._reset_frame()
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        # alt-screen + clear + SGR mouse reporting; trawl owns the whole tab
        sys.stdout.write("\x1b[?1049h\x1b[3J\x1b[2J\x1b[H\x1b[?25l\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()

    def _reset_frame(self) -> None:
        self._lines = self._size = None

    def leave(self) -> None:
        sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.saved:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((100, 30))
        return s.columns, s.lines

    def read_keys(self, timeout: float) -> list[str]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return []
        return parse_keys(data)

    def write(self, lines: list[str], size: tuple[int, int]) -> bool:
        current = tuple(lines)
        if current == self._lines and size == self._size:
            return False
        full = self._lines is None or size != self._size or len(current) != len(self._lines)
        if full:
            buf = ["\x1b[H\x1b[2J"]
            for i, ln in enumerate(current):
                buf.append(ln + "\x1b[K")
                if i < len(current) - 1:
                    buf.append("\r\n")
            buf.append("\x1b[J")
        else:
            buf = [f"\x1b[{i + 1};1H{ln}\x1b[K"
                   for i, (old, ln) in enumerate(zip(self._lines, current)) if old != ln]
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self._lines, self._size = current, size
        return True


# -- app state ---------------------------------------------------------------


class App:
    def __init__(self, eng=None):
        self.eng = eng
        self.view = "search"  # search | downloads
        self.editing = False  # splash is a landing (q quits); typing/"/" starts editing
        self.query = ""
        self.cursor = 0  # caret index into query (edit position)
        self.history = load_history()  # past queries, oldest -> newest
        self.hist_idx = len(self.history)  # cursor; == len means "live draft"
        self.draft = ""  # query in progress before browsing history
        self._results_revision = 0
        self._results: tuple[Result, ...] = ()
        self.results = []
        self.errors: dict[str, str] = {}
        self.search: Search | None = None
        self.search_done = 0
        self.search_total = len(SOURCES)
        self.sel = 0
        self.cat = "all"
        self.sort = "seeders"  # seeders | size | newest
        self.downloads: list[Download] = []
        self.dsel = 0
        self.down_speed = 0  # aria2 global download speed (bytes/s)
        self.num_active = 0
        self.help = False
        self.status = ""
        self.running = True
        self.confirm_quit = False
        self.torrent_prompt = None  # ParsedMagnet of a pending .torrent link (file vs contents)
        self.cancel_prompt = None  # Download pending cancel (delete files vs keep)
        self.picker: Download | None = None  # download whose files are being picked
        self.picker_files: list[dict] = []  # engine file dicts for the picker
        self.picker_sel = 0
        self.picker_on: set[int] = set()  # selected 1-based file indices
        self.picker_bytes = 0
        self.detail: Result | None = None  # search result shown in the details view
        self.variant_idx = 0
        cfg = load_config()
        self.config = cfg if isinstance(cfg, dict) else {}
        cfg = self.config
        self.torznab_feeds: list[dict[str, str]] = []
        seen = {s.id for s in SOURCES}
        records = self.config.get("torznab_feeds", [])
        if isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                sid, url, key = rec.get("id"), rec.get("url"), rec.get("api_key", "")
                if not all(isinstance(x, str) for x in (sid, url, key)) or not sid or sid in seen:
                    continue
                try:
                    validate_torznab_url(url)
                except Exception:
                    continue
                seen.add(sid)
                self.torznab_feeds.append({"id": sid, "url": url, "api_key": key})
        self.sources = []
        self.source_labels: dict[str, str] = {}
        self.source_groups: dict[str, str] = {}
        self.source_secrets: dict[str, tuple[str, ...]] = {}
        self._rebuild_sources()
        self.disabled_sources: set[str] = set(cfg.get("disabled_sources", []))
        self.download_dir: str | None = cfg.get("download_dir")
        self.dl_history: list[dict] = load_dl_history()  # completed downloads, oldest->newest
        self.settings = False  # settings overlay open
        self.set_sel = 0  # 0 dir, 1 provider, 2 key, 3.. sources
        self.edit_field: str | None = None  # settings text-edit: "dir" | "key"
        self.edit_buf = ""
        self.remove_feed: str | None = None
        self.local_query = LocalQuery("")
        self.start = time.monotonic()
        self.meta_provider = cfg.get("meta_provider", "tmdb")  # tmdb | omdb
        self.tmdb_key = cfg.get("tmdb_key") or os.environ.get("TMDB_API_KEY")
        self.omdb_key = cfg.get("omdb_key") or os.environ.get("OMDB_API_KEY")
        self.meta: dict[str, object] = {}  # "provider:kind:name" -> "loading" | Meta | None
        self._visible_revision = -1
        self._visible_cat: str | None = None
        self._visible_cache: tuple[Result, ...] = ()

    # -- derived
    @property
    def results(self) -> tuple[Result, ...]:
        return self._results

    @results.setter
    def results(self, value: Iterable[Result]) -> None:
        self._results = tuple(value)
        self._results_revision += 1

    def visible_results(self) -> tuple[Result, ...]:
        if self._results_revision == self._visible_revision and self.cat == self._visible_cat:
            return self._visible_cache
        if self.cat != "all":
            g = CAT_GROUP[self.cat]
            out = tuple(r for r in self.results if self.result_group(r) == g)
            self._visible_revision, self._visible_cat, self._visible_cache = self._results_revision, self.cat, out
            return out
        # All: round-robin across categories so one prolific group (e.g. anime)
        # can't monopolize the top. Buckets keep first-seen order (= current sort),
        # so the group holding the overall-top result still leads.
        buckets: dict[str, list[Result]] = {}
        for r in self.results:
            buckets.setdefault(self.result_group(r) or "Other", []).append(r)
        cols = list(buckets.values())
        ordered: list[Result] = []
        for i in range(max((len(c) for c in cols), default=0)):
            ordered += [c[i] for c in cols if i < len(c)]
        out = tuple(ordered)
        self._visible_revision, self._visible_cat, self._visible_cache = self._results_revision, self.cat, out
        return out

    def _cur(self) -> Result | None:
        """The selected search result, or None if the list is empty/out of range."""
        rs = self.visible_results()
        return rs[self.sel] if 0 <= self.sel < len(rs) else None

    def _meta_kind(self, r: Result) -> str | None:
        return kind_for(self.result_group(r))

    def _rebuild_sources(self) -> None:
        configured = []
        for rec in self.torznab_feeds:
            try:
                configured.append(make_torznab_source(TorznabFeed(**rec)))
            except Exception:
                pass
        self.sources = [*SOURCES, *configured]
        for source in self.sources:
            self.source_labels[source.id] = source.label
            self.source_groups[source.id] = source.group
            self.source_secrets[source.id] = tuple(dict.fromkeys(
                (*self.source_secrets.get(source.id, ()), *source.secrets)))

    def source_label(self, source_id: str) -> str:
        return self.source_labels.get(source_id, source_id)

    def source_secrets_for(self, source_id: str) -> tuple[str, ...]:
        return self.source_secrets.get(source_id, ())

    def _all_secrets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(secret for secrets in self.source_secrets.values()
                                   for secret in secrets if secret))

    def result_group(self, result: Result) -> str | None:
        return result.group or self.source_groups.get(result.source)

    def source_tag(self, source_id: str) -> tuple[str, str]:
        if source_id in {s.id for s in SOURCES}:
            return T.source_style(source_id)
        return dtrunc(self.source_label(source_id), 5), T.ALT

    def _variants(self, result: Result | None = None) -> tuple[ResultVariant, ...]:
        result = result or self.detail
        if not result:
            return ()
        return result.variants or (ResultVariant(result.source, result.magnet, result.page,
                                                  result.seeders, result.leechers),)

    def _variant(self) -> ResultVariant | None:
        variants = self._variants()
        return variants[self.variant_idx % len(variants)] if variants else None

    def _provider_key(self) -> str | None:
        return self.omdb_key if self.meta_provider == "omdb" else self.tmdb_key

    def _set_provider_key(self, key: str | None) -> None:
        if self.meta_provider == "omdb":
            self.omdb_key = key
        else:
            self.tmdb_key = key

    def _detail_meta(self):
        """Cached metadata for the open detail result: Meta | "loading" | None."""
        r = self.detail
        kind = self._meta_kind(r) if r else None
        return self.meta.get(f"{self.meta_provider}:{kind}:{r.name}") if kind else None

    def _ensure_meta(self, r: Result) -> None:
        """Kick a one-shot background metadata lookup for a movie/series result."""
        kind = self._meta_kind(r)
        key = self._provider_key()
        if not kind or not key:
            return
        ckey = f"{self.meta_provider}:{kind}:{r.name}"
        if ckey in self.meta:
            return
        self.meta[ckey] = "loading"
        threading.Thread(target=self._fetch_meta, args=(ckey, r.name, kind), daemon=True).start()

    def _fetch_meta(self, ckey: str, name: str, kind: str) -> None:
        try:
            self.meta[ckey] = lookup(name, kind, self.meta_provider, self._provider_key())
        except Exception:
            self.meta[ckey] = None

    def animating(self, rows: int) -> bool:
        if self.view != "downloads" or self.help or self.settings or self.picker is not None:
            return False
        covered = set(_overlay_rows(rows)) if (self.confirm_quit or self.torrent_prompt
                                               or self.cancel_prompt) else set()
        return any(self.downloads[idx].status in ("active", "metadata") and bar_row not in covered
                   for idx, bar_row in _visible_download_rows(self, rows))

    def enabled_sources(self) -> list:
        return [s for s in self.sources if s.id not in self.disabled_sources]

    @property
    def tick(self) -> float:
        return (time.monotonic() - self.start) * 1000 / T.SHEEN_TICK_MS

    # -- actions
    def submit(self) -> None:
        q = self.query.strip()
        pm = parse_source(q)
        if pm:
            self._grab_source(pm)
            self.query = ""
            self.cursor = 0
            self.editing = False
            return
        self.local_query = parse_query(q)
        srcs = self.enabled_sources()
        if not self.local_query.remote.strip():  # Latest/operator-only: browse-capable built-ins
            srcs = [s for s in srcs if s.browse]
        self.search = Search(self.local_query.remote, srcs)
        self.search_total = getattr(self.search, "total", len(srcs))
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.editing = False
        self.detail = None
        if q:
            self._add_history(q)
        self.status = f'searching "{clean(q)}"' if q else "loading latest"
        if self.local_query.malformed:
            self.status = f"searching; ignored {len(self.local_query.malformed)} malformed filter(s)"

    def _add_history(self, q: str) -> None:
        if q in self.history:
            self.history.remove(q)
        self.history.append(q)
        self.history = self.history[-HIST_MAX:]
        save_history(self.history)
        self.hist_idx = len(self.history)
        self.draft = ""

    def _hist(self, delta: int) -> None:
        if not self.history:
            return
        if self.hist_idx == len(self.history):  # leaving the live draft
            self.draft = self.query
        self.hist_idx = max(0, min(self.hist_idx + delta, len(self.history)))
        self.query = self.history[self.hist_idx] if self.hist_idx < len(self.history) else self.draft
        self.cursor = len(self.query)

    def clear(self) -> None:
        """Drop search results and return to the splash."""
        self.search = None
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.query = ""
        self.cursor = 0
        self.editing = False
        self.detail = None
        self.variant_idx = 0
        self.status = ""

    def grab(self, magnet: str, name: str) -> None:
        secrets = self._all_secrets()
        if not self.eng:
            self.status = redact(f"(no engine) {clean(name)[:48]}", secrets)
            return
        try:
            self.eng.add(magnet)
            self.status = redact(f"grabbing: {clean(name)[:48]}", secrets)
        except Aria2Error as e:
            self.status = redact(f"error: {e}", secrets)

    def _grab_source(self, pm) -> None:
        """Grab a parsed input; a .torrent link first asks file-vs-contents."""
        if pm.kind == "torrent":
            self.torrent_prompt = pm
        else:
            self.grab(pm.magnet, pm.name)

    def grab_torrent(self, url: str, name: str, contents: bool) -> None:
        """A .torrent link: follow-torrent=mem grabs its contents; =false saves
        just the .torrent file. aria2 handles both natively."""
        secrets = self._all_secrets()
        if not self.eng:
            self.status = redact(f"(no engine) {clean(name)[:48]}", secrets)
            return
        try:
            self.eng.add(url, {"follow-torrent": "mem" if contents else "false"})
            self.status = redact(f"grabbing torrent: {clean(name)[:40]}" if contents
                                 else f"downloading .torrent file: {clean(name)[:40]}", secrets)
            self.view = "downloads"
        except Aria2Error as e:
            self.status = redact(f"error: {e}", secrets)

    def _cancel(self, d: Download, delete_files: bool) -> None:
        """Remove a download from aria2; optionally delete its files off disk."""
        paths = self.eng.file_paths(d.root) if (self.eng and delete_files) else []
        if self.eng:
            self.eng.remove(d.root)
        if delete_files:
            remove_download_files(paths, self.eng.download_dir() if self.eng else None, d.name)
            self.status = f"cancelled + deleted files: {clean(d.name)[:34]}"
        else:
            self.status = f"cancelled (files kept): {clean(d.name)[:36]}"
        self.dsel = max(0, self.dsel - 1)

    def scan_resume(self) -> int:
        """Re-add incomplete BT downloads (*.aria2 control files) found in the
        download dir that aria2 isn't already running."""
        if not self.eng:
            return 0
        dir_path = self.eng.download_dir()
        if not dir_path or not os.path.isdir(dir_path):
            return 0
        have = self.eng.active_infohashes()
        n = 0
        for ctrl in glob.glob(os.path.join(dir_path, "*.aria2")):
            ih = control_infohash(ctrl)
            if not ih or ih in have:
                continue
            try:
                self.eng.add(build_magnet(ih, os.path.basename(ctrl)[:-7]), {"dir": dir_path})
                have.add(ih)
                n += 1
            except Aria2Error:
                pass
        return n

    def _save_settings(self) -> None:
        self.config.update({"disabled_sources": sorted(self.disabled_sources),
                            "download_dir": self.download_dir, "meta_provider": self.meta_provider,
                            "tmdb_key": self.tmdb_key, "omdb_key": self.omdb_key,
                            "torznab_feeds": [dict(feed) for feed in self.torznab_feeds]})
        save_config(self.config)

    def setting_items(self) -> list[tuple[str, object]]:
        return ([('dir', None), ('provider', None), ('meta-key', None)]
                + [('source', s) for s in SOURCES]
                + [('feed', feed) for feed in self.torznab_feeds]
                + [('add-feed', None)])

    def _start_feed_edit(self, feed: dict[str, str] | None = None) -> None:
        self.edit_field = "feed-url"
        self.edit_buf = feed["url"] if feed else ""
        self.remove_feed = None

    def _selected_setting(self) -> tuple[str, object]:
        items = self.setting_items()
        self.set_sel = min(self.set_sel, len(items) - 1)
        return items[self.set_sel]

    def _settings_key(self, k: str) -> None:
        rows = len(self.setting_items())
        if self.edit_field:
            if k == "enter":
                self._commit_edit()
            elif k == "esc":
                self.edit_field = None
                self.status = ""
            elif k == "backspace":
                self.edit_buf = self.edit_buf[:-1]
            elif len(k) == 1 and k >= " ":
                self.edit_buf += k
            return
        if k in ("g", "esc", "q"):
            if self.remove_feed:
                self.remove_feed = None
                self.status = ""
            else:
                self.settings = False
        elif k == "up" or (k == "k" and self._selected_setting()[0] != "feed"):
            self.set_sel = (self.set_sel - 1) % rows
        elif k in ("down", "j"):
            self.set_sel = (self.set_sel + 1) % rows
        else:
            kind, value = self._selected_setting()
            if k == "a" or (k == "enter" and kind == "add-feed"):
                self._start_feed_edit()
            elif k in ("enter", "e") and kind == "feed":
                if self.remove_feed == value["id"]:
                    self._remove_selected_feed(value)
                else:
                    self._start_feed_edit(value)
            elif k == "k" and kind == "feed":
                self.edit_field, self.edit_buf = "feed-key", value["api_key"]
                self.remove_feed = None
            elif k == "x" and kind == "feed":
                if self.remove_feed == value["id"]:
                    self._remove_selected_feed(value)
                else:
                    self.remove_feed = value["id"]
                    self.status = f"press x or Enter to remove {torznab_label(value['url'])}"
            elif k in ("enter", " ") and kind == "dir":
                self.edit_field = "dir"
                self.edit_buf = self.download_dir or (self.eng.download_dir() if self.eng else "") or ""
            elif k in ("enter", " ") and kind == "provider":
                self.meta_provider = "omdb" if self.meta_provider == "tmdb" else "tmdb"
                self.meta.clear()  # cached results are provider-specific
                self._save_settings()
            elif k in ("enter", " ") and kind == "meta-key":
                self.edit_field = "key"
                self.edit_buf = self._provider_key() or ""
            elif k in ("enter", " ") and kind in ("source", "feed"):
                sid = value.id if kind == "source" else value["id"]
                self.disabled_sources.symmetric_difference_update({sid})
                self._save_settings()

    def _remove_selected_feed(self, feed: dict[str, str]) -> None:
        self.torznab_feeds.remove(feed)
        self.disabled_sources.discard(feed["id"])
        self.remove_feed = None
        self._rebuild_sources()
        self._save_settings()
        self.set_sel = min(self.set_sel, len(self.setting_items()) - 1)
        self.status = "feed removed"

    def _commit_edit(self) -> None:
        if self.edit_field == "dir":
            self.download_dir = self.edit_buf.strip() or None
            if self.eng and self.download_dir:
                self.eng.set_dir(self.download_dir)
        elif self.edit_field == "key":
            self._set_provider_key(self.edit_buf.strip() or None)
            self.meta.clear()  # re-fetch with the new key
        elif self.edit_field in ("feed-url", "feed-key"):
            kind, value = self._selected_setting()
            if self.edit_field == "feed-key" and kind == "feed":
                value["api_key"] = self.edit_buf
            else:
                raw = self.edit_buf.strip()
                try:
                    validate_torznab_url(raw)
                except Exception:
                    self.status = "invalid Torznab URL"
                    return
                if kind == "feed":
                    value["url"] = raw
                else:
                    sid = "torznab-" + uuid.uuid4().hex[:12]
                    self.torznab_feeds.append({"id": sid, "url": raw, "api_key": ""})
                    self.set_sel = len(self.setting_items()) - 2
            self._rebuild_sources()
        self.edit_field = None
        self.status = ""
        self._save_settings()

    def _open_picker(self, d: Download) -> None:
        files = self.eng.files(d.root) if self.eng else []
        if len(files) < 2:
            self.status = ("single-file download — nothing to pick" if files
                           else "file list not ready yet — fetching metadata")
            return
        self.picker = d
        self.picker_files = files
        self.picker_sel = 0
        self.picker_on = {f["index"] for f in files if f["selected"]}
        self.picker_bytes = sum(int(f.get("length") or 0) for f in files
                                if f["index"] in self.picker_on)

    def _picker_key(self, k: str) -> None:
        n = len(self.picker_files)
        if k in ("esc", "q", "f"):
            self.picker = None
        elif k in ("up", "k"):
            self.picker_sel = (self.picker_sel - 1) % n
        elif k in ("down", "j"):
            self.picker_sel = (self.picker_sel + 1) % n
        elif k == " ":
            f = self.picker_files[self.picker_sel]
            idx, length = f["index"], int(f.get("length") or 0)
            if idx in self.picker_on:
                self.picker_on.remove(idx)
                self.picker_bytes -= length
            else:
                self.picker_on.add(idx)
                self.picker_bytes += length
        elif k == "a":
            all_on = {f["index"] for f in self.picker_files}
            if self.picker_on == all_on:
                self.picker_on, self.picker_bytes = set(), 0
            else:
                self.picker_on = all_on
                self.picker_bytes = sum(int(f.get("length") or 0) for f in self.picker_files)
        elif k == "enter":
            if not self.picker_on:
                self.status = "select at least one file"
                return
            ok = self.eng.select_files(self.picker.root, sorted(self.picker_on)) if self.eng else False
            self.status = (f"downloading {len(self.picker_on)}/{n} files" if ok
                           else "couldn't set file selection")
            self.picker = None

    def drain_search(self) -> None:
        if not self.search:
            return
        changed = False
        selected = self._cur()
        selected_id = ((result_identity(selected) or (selected.name, selected.magnet))
                       if selected else None)
        source_map = getattr(self.search, "sources", None) or {s.id: s for s in self.sources}
        incoming: list[Result] = []
        while True:
            try:
                u = self.search.updates.get_nowait()
            except queue.Empty:
                break
            self.search_done += 1
            changed = True
            if u.results is None:
                self.errors[u.source] = u.error
            else:
                self.errors.pop(u.source, None)
                incoming.extend(r for r in u.results
                                if matches_query(r, self.local_query,
                                                 source_map))
        if changed:
            self.results = self._apply_sort(dedupe([*self.results, *incoming]))
            visible = self.visible_results()
            restored = next((i for i, r in enumerate(visible)
                             if (result_identity(r) or (r.name, r.magnet)) == selected_id), None)
            self.sel = (restored if restored is not None
                        else min(self.sel, max(0, len(visible) - 1)))

    def retry_failed_sources(self) -> None:
        if not self.search or not self.errors:
            self.status = "no failed sources to retry"
            return
        scheduled = self.search.retry(tuple(self.errors))
        self.search_total = self.search.total
        self.status = (f"retrying {len(scheduled)} failed source(s)" if scheduled
                       else "failed sources already retrying")

    def _apply_sort(self, results: Sequence[Result]) -> list[Result]:
        if self.sort == "size":
            key = lambda r: (r.size, r.seeders)
        elif self.sort == "newest":
            key = lambda r: (r.added or 0, r.seeders)
        else:
            key = lambda r: (r.seeders, r.added or 0)
        return sorted(results, key=key, reverse=True)

    def _cycle_sort(self) -> None:
        order = ["seeders", "size", "newest"]
        self.sort = order[(order.index(self.sort) + 1) % len(order)]
        self.results = self._apply_sort(self.results)
        self.sel = 0
        self.status = f"sorted by {self.sort}"

    def update_downloads(self, downloads: list[Download]) -> None:
        """Replace the download list, notifying on any active->complete transition
        seen this session (not for downloads that were already complete when first
        polled, so launching with finished items stays quiet)."""
        prev = {d.root: d.status for d in self.downloads}
        for d in downloads:
            was = prev.get(d.root)
            if d.status == "complete" and was is not None and was != "complete":
                notify("trawl — download complete", d.name)
                rec = {"name": d.name, "size": d.total, "ts": int(time.time()), "path": d.path}
                self.dl_history.append(rec)
                append_dl_history(rec)
        self.downloads = downloads

    def _move(self, d: int) -> None:
        if self.view == "downloads":
            n = len(self.downloads)
            self.dsel = max(0, min(self.dsel + d, n - 1)) if n else 0
        else:
            n = len(self.visible_results())
            self.sel = max(0, min(self.sel + d, n - 1)) if n else 0

    def _cycle_cat(self, d: int) -> None:
        i = next((k for k, (key, _) in enumerate(CATS) if key == self.cat), 0)
        self.cat = CATS[(i + d) % len(CATS)][0]
        self.sel = 0

    def on_key(self, k: str) -> None:
        if self.help:
            self.help = False
            return
        if k == "ctrl-c":
            self.running = False
            return
        if self.confirm_quit:
            if k == "enter":
                self.running = False
            elif k == "esc":
                self.confirm_quit = False
            return
        if self.torrent_prompt is not None:
            pm = self.torrent_prompt
            if k in ("t", "enter"):
                self.grab_torrent(pm.magnet, pm.name, True)
                self.torrent_prompt = None
            elif k == "f":
                self.grab_torrent(pm.magnet, pm.name, False)
                self.torrent_prompt = None
            elif k in ("esc", "q"):
                self.torrent_prompt = None
            return
        if self.cancel_prompt is not None:
            d = self.cancel_prompt
            if k == "d":
                self._cancel(d, True)
                self.cancel_prompt = None
            elif k == "k":
                self._cancel(d, False)
                self.cancel_prompt = None
            elif k in ("esc", "q"):
                self.cancel_prompt = None
            return
        if self.settings:
            self._settings_key(k)
            return
        if self.picker is not None:
            self._picker_key(k)
            return
        if self.detail is not None:
            if k in ("esc", "enter", "q"):
                self.detail = None
                self.variant_idx = 0
            elif k in ("left", "right"):
                variants = self._variants()
                if len(variants) > 1:
                    self.variant_idx = (self.variant_idx + (-1 if k == "left" else 1)) % len(variants)
            elif k == "d":
                self.grab(self._variant().uri, self.detail.name)
                self.detail = None
                self.variant_idx = 0
            elif k == "o":
                page = self._variant().page
                self.status = ("opened in browser" if page and open_url(page)
                               else "no page for this source" if not page
                               else "couldn't open browser")
            elif k == "y":
                self.status = ("magnet copied to clipboard"
                               if copy_clipboard(self._variant().uri) else "copy failed")
            elif k == "p":
                m = self._detail_meta()
                poster = m.poster if isinstance(m, Meta) else ""
                self.status = ("opened poster" if poster and open_url(poster)
                               else "no poster available")
            return
        if self.view == "search" and self.editing:
            if k == "enter":
                self.submit()
            elif k == "esc":
                self.editing = False
            elif k == "backspace":
                if self.cursor > 0:
                    self.query = self.query[:self.cursor - 1] + self.query[self.cursor:]
                    self.cursor -= 1
                self.hist_idx = len(self.history)
            elif k == "left":
                self.cursor = max(0, self.cursor - 1)
            elif k == "right":
                self.cursor = min(len(self.query), self.cursor + 1)
            elif k == "tab":
                self.view, self.editing = "downloads", False
            elif k == "up":
                self._hist(-1)
            elif k == "down":
                self._hist(1)
            elif len(k) == 1 and k >= " ":
                self.query = self.query[:self.cursor] + k + self.query[self.cursor:]
                self.cursor += 1
                self.hist_idx = len(self.history)
            return
        if self.view == "search" and self.search is None and not self.editing:
            if k == "q":
                self.confirm_quit = True
            elif k == "tab":
                self.view = "downloads"
            elif k == "enter":
                self.submit()
            elif k in ("/", "i"):
                self.editing = True
            elif k == "up":
                self.editing = True
                self._hist(-1)
            elif len(k) == 1 and k >= " ":
                self.editing = True
                self.query = self.query[:self.cursor] + k + self.query[self.cursor:]
                self.cursor += 1
            return
        # nav (results) / downloads
        if k == "q":
            self.confirm_quit = True
        elif k == "?":
            self.help = True
        elif k == "g":
            self.settings = True
            self.set_sel = 0
        elif k == "tab":
            self.view = "downloads" if self.view == "search" else "search"
            self.detail = None
            self.variant_idx = 0
        elif k == "s":
            n = self.scan_resume()
            self.status = (f"resumed {n} download{'' if n == 1 else 's'}" if n
                           else "nothing to resume on disk")
            if n:
                self.view = "downloads"
        elif k == "v":
            pm = parse_source(paste_clipboard())
            if pm:
                self._grab_source(pm)
            else:
                self.status = "no magnet or link in clipboard"
        elif k in ("up", "k"):
            self._move(-1)
        elif k in ("down", "j"):
            self._move(1)
        elif self.view == "search":
            if k == "left":
                self._cycle_cat(-1)
            elif k == "right":
                self._cycle_cat(1)
            elif k in ("/", "i"):
                self.editing = True
            elif k == "c":
                self.clear()
            elif k == "S":
                self._cycle_sort()
            elif k == "r":
                self.retry_failed_sources()
            elif k == "d":
                if (r := self._cur()):
                    self.grab(r.magnet, r.name)
            elif k == "enter":
                if (r := self._cur()):
                    self.detail = r
                    self.variant_idx = 0
                    self._ensure_meta(r)
            elif k == "y":
                if (r := self._cur()):
                    self.status = ("magnet copied to clipboard"
                                   if copy_clipboard(r.magnet) else "copy failed")
            elif k == "o":
                if (r := self._cur()):
                    page = r.page
                    self.status = ("opened in browser" if page and open_url(page)
                                   else "no page for this source" if not page
                                   else "couldn't open browser")
        elif self.view == "downloads":
            if not self.downloads or not (0 <= self.dsel < len(self.downloads)):
                return
            d = self.downloads[self.dsel]
            if k == "x":
                self.cancel_prompt = d
            elif k == "p":
                if d.status == "paused":
                    if self.eng:
                        self.eng.resume(d.root)
                    self.status = f"resumed: {clean(d.name)[:40]}"
                else:
                    if self.eng:
                        self.eng.pause(d.root)
                    self.status = f"paused: {clean(d.name)[:40]}"
            elif k == "o":
                if not d.path or d.status == "metadata":
                    self.status = "location not ready yet — fetching metadata"
                elif reveal(d.path):
                    self.status = f"revealed: {clean(d.name)[:40]}"
                else:
                    self.status = "couldn't open location"
            elif k == "r":
                if d.status != "error":
                    self.status = "r retries a failed download"
                elif self.eng and self.eng.retry(d.root):
                    self.status = f"retrying: {clean(d.name)[:42]}"
                else:
                    self.status = "couldn't retry — no source uri known"
            elif k == "f":
                if d.status in ("active", "waiting", "paused"):
                    self._open_picker(d)
                else:
                    self.status = "file selection only for in-progress downloads"


# -- rendering ---------------------------------------------------------------


def _window(sel: int, total: int, h: int) -> int:
    if total <= h:
        return 0
    return max(0, min(sel - h // 2, total - h))


def _wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        if cur and dwidth(cur) + 1 + dwidth(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines or [""]


def _detail_panel(app: App, r: Result, width: int, height: int) -> list[str]:
    inner_w = width - 4
    inner = [cell(line, inner_w, color=T.ACCENT, bold=True) for line in _wrap(clean(r.name), inner_w)]
    inner.append(cell("", inner_w))

    def field(label: str, value: str, color: str | None = None) -> str:
        return "  " + cell(label, 7, dim=True) + cell(value, inner_w - 9, color=color)

    variants = app._variants(r)
    variant = variants[app.variant_idx % len(variants)]
    health = (f"{variant.seeders} seeders · {variant.leechers} leechers"
              if (variant.seeders or variant.leechers) else "unknown")
    if len(variants) > 1:
        inner.append(field("Variant", f"{app.variant_idx + 1}/{len(variants)}"))
    inner.append(field("Source", app.source_label(variant.source)))
    inner.append(field("Size", fmt_bytes(r.size)))
    inner.append(field("Health", health, T.GOOD if variant.seeders else None))
    if r.added:
        inner.append(field("Added", fmt_rel(r.added)))
    if r.num_files:
        inner.append(field("Files", str(r.num_files)))
    inner.append(field("Hash", r.info_hash))
    if variant.page:
        inner.append(field("Page", redact_url(variant.page,
                                               app.source_secrets_for(variant.source))))
    kind = app._meta_kind(r)
    if kind and app._provider_key():
        m = app._detail_meta()
        rlabel = "IMDb" if app.meta_provider == "omdb" else "TMDb"
        inner.append(cell("", inner_w))
        if m == "loading":
            inner.append(field("Info", "fetching…", T.ALT))
        elif isinstance(m, Meta):
            if m.rating:
                inner.append(field(rlabel, f"{m.rating:.1f}/10  ({m.votes:,} votes)", T.WARN))
            if m.genres:
                inner.append(field("Genre", ", ".join(m.genres)))
            if m.cast:
                inner.append(field("Cast", ", ".join(m.cast)))
            if m.poster:
                inner.append(field("Poster", "press p to open", T.ALT))
            if m.overview:
                inner.append(cell("", inner_w))
                for ln in _wrap(m.overview, inner_w - 2):
                    inner.append("  " + cell(ln, inner_w - 2, dim=True))
        else:  # fetched, no match
            inner.append(field("Info", f"no match on {app.meta_provider.upper()}", T.RULE))
    return _wrap_panel("Details", inner, width, height, True)


def _panel_top(title: str, width: int, count: str | None, bw: str) -> str:
    pre, suf = "╭─", "─╮"
    label = f" {title} "
    cnt = f" {count} " if count else ""
    fill = max(0, width - 4 - dwidth(label) - dwidth(cnt))
    return (style(pre, bw) + style(label, T.ALT, bold=True) + style("─" * fill, bw)
            + style(cnt, dim=True) + style(suf, bw))


def _panel_bottom(width: int, bw: str) -> str:
    return style("╰" + "─" * (width - 2) + "╯", bw)


def _side(line: str, width: int, bw: str) -> str:
    return style("│", bw) + " " + line + " " + style("│", bw)


def _wrap_panel(title: str, inner: list[str], width: int, height: int,
                focused: bool, count: str | None = None) -> list[str]:
    bw = T.ACCENT if focused else T.RULE
    inner_w = width - 4
    body_h = height - 2
    rows = (inner + [cell("", inner_w)] * body_h)[:body_h]
    return [_panel_top(title, width, count, bw)] + [_side(r, inner_w, bw) for r in rows] + [_panel_bottom(width, bw)]


@cache
def _logo_lines() -> tuple[str, ...]:
    out = []
    rows = len(T.LOGO_LINES)
    for row, line in enumerate(T.LOGO_LINES):
        chars = list(line)
        last = max(1, len(chars) - 1)
        ty = row / max(1, rows - 1)
        seg = ""
        for i, ch in enumerate(chars):
            if ch == " ":
                seg += " "
            elif ch in T.NET_GLYPHS:
                seg += style(ch, T.NET_COLOR, bold=True)
            else:
                seg += style(ch, T.logo_color(((i / last) + ty) / 2), bold=True)
        out.append(seg)
    return tuple(out)


def _rail(app: App, h: int) -> list[str]:
    lines = [cell("", RAIL_W)]
    for key, label in CATS:
        sel = app.view == "search" and app.cat == key
        mark = style(T.BAR, T.ACCENT, bold=True) if sel else " "
        lines.append(mark + " " + cell(label, RAIL_W - 2, color=T.ACCENT if sel else None,
                                       bold=sel, dim=not sel))
    lines.append(cell("", RAIL_W))
    dsel = app.view == "downloads"
    n = len(app.downloads)
    mark = style(T.BAR, T.ACCENT, bold=True) if dsel else " "
    label = "Downloads" + (f" ({n})" if n else "")
    lines.append(mark + " " + cell(label, RAIL_W - 2, color=T.ACCENT if dsel else None,
                                   bold=dsel, dim=not dsel))
    return (lines + [cell("", RAIL_W)] * h)[:h]


def _caret_view(q: str, cur: int, avail: int) -> tuple[str, str, str]:
    """Slice q to <= avail display cols keeping the caret (index cur) visible.
    Returns (before, at, after); `at` is the char under the caret, "" past the end."""
    caret_w = _cw(q[cur]) if cur < len(q) else 1
    left_budget = max(0, avail - caret_w)
    start, used = cur, 0
    while start > 0 and used + _cw(q[start - 1]) <= left_budget:
        start -= 1
        used += _cw(q[start])
    remaining = avail - dwidth(q[start:cur]) - caret_w
    end, used = cur + (1 if cur < len(q) else 0), 0
    while end < len(q) and used + _cw(q[end]) <= remaining:
        used += _cw(q[end])
        end += 1
    if cur < len(q):
        return q[start:cur], q[cur], q[cur + 1:end]
    return q[start:cur], "", ""


def _search_line(app: App, inner_w: int) -> str:
    editing = app.view == "search" and app.editing
    prompt = style(T.PTR + " ", T.ACCENT)
    avail = inner_w - 2  # columns after the prompt
    if not app.query:
        ph = dtrunc("Search, or paste a magnet or link…", avail)
        return prompt + style(ph, dim=True) + " " * max(0, avail - dwidth(ph))
    if not editing:
        text = dtrunc(app.query, avail)
        return prompt + style(text, T.TEXT) + " " * max(0, avail - dwidth(text))
    cur = max(0, min(app.cursor, len(app.query)))
    before, at, after = _caret_view(app.query, cur, avail)
    caret = f"\x1b[7m{at or ' '}\x1b[0m"  # reverse-video block cursor
    used = dwidth(before) + (_cw(at) if at else 1) + dwidth(after)
    return prompt + style(before, T.TEXT) + caret + style(after, T.TEXT) + " " * max(0, avail - used)


def _search_panel(app: App, width: int) -> list[str]:
    editing = app.view == "search" and app.editing
    return _wrap_panel("Search", [_search_line(app, width - 4)], width, 3, editing)


def _status_line(app: App, results: Sequence[Result], inner_w: int) -> str:
    if app.search and app.search_done < app.search_total:
        return cell(f"searching… {app.search_done}/{app.search_total} sources", inner_w, dim=True)
    errs = len(app.errors)
    if not results:
        if app.search is None:
            return cell("Type to search. Enter runs it; paste a magnet or link to grab it.", inner_w, dim=True)
        if errs >= app.search_total:
            return cell("Couldn't reach any source — they may be down.", inner_w, color=T.WARN)
        q = clean(app.query)
        return cell(f'No results for "{q}".' if q else "Nothing new right now.", inner_w, dim=True)
    note = f"  ({errs} source{'' if errs == 1 else 's'} down)" if errs else ""
    head = "popular now" if not app.query.strip() else f"{len(results)} result{'' if len(results) == 1 else 's'}"
    return cell(head + note, inner_w, dim=True)


def _results_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    results = app.visible_results()
    app.sel = min(app.sel, max(0, len(results) - 1))
    name_w = max(8, inner_w - 28)  # ptr2 + name + 9 + 9 + 5 + 3 seps
    inner: list[str] = [_status_line(app, results, inner_w)]
    if results:
        header = (cell("", 2) + " " + cell("Name", name_w, dim=True, bold=True) + " "
                  + cell("Size", 9, "right", dim=True, bold=True) + " "
                  + cell("S:L", 9, "right", dim=True, bold=True) + " "
                  + cell("Src", 5, "right", dim=True, bold=True))
        inner.append(header)
        list_h = max(1, height - 2 - len(inner))
        start = _window(app.sel, len(results), list_h)
        for idx in range(start, min(start + list_h, len(results))):
            r = results[idx]
            here = idx == app.sel
            tag, tcolor = app.source_tag(r.source)
            sl = f"{r.seeders}:{r.leechers}" if (r.seeders or r.leechers) else "-"
            if here:  # selected row: the whole line lights up in accent
                inner.append(
                    cell(T.PTR, 2, color=T.ACCENT) + " "
                    + cell(clean(r.name), name_w, color=T.ACCENT, bold=True) + " "
                    + cell(fmt_bytes(r.size), 9, "right", color=T.ACCENT, bold=True) + " "
                    + cell(sl, 9, "right", color=T.ACCENT, bold=True) + " "
                    + cell(tag, 5, "right", color=T.ACCENT, bold=True))
            else:
                inner.append(
                    cell("", 2) + " "
                    + cell(clean(r.name), name_w, dim=True) + " "
                    + cell(fmt_bytes(r.size), 9, "right", dim=True) + " "
                    + cell(sl, 9, "right", color=T.GOOD if r.seeders else None, dim=not r.seeders) + " "
                    + cell(tag, 5, "right", color=tcolor, dim=True))
    base = "Latest" if (app.search is not None and not app.query.strip()) else "Results"
    title = f"{base} · {app.sort}" if results else base
    count = f"({len(results)})" if results else None
    return _wrap_panel(title, inner, width, height, app.view == "search" and not app.editing, count)


_DOWNLOADS_PREFIX = 4  # search panel + spacer before the downloads panel
_DOWNLOAD_ITEM_H = 3
_DOWNLOAD_BAR_ROW = 2  # panel-relative: border, status, then bar
_RECENT_MIN_H = 2      # heading plus at least one history row


def _recent_downloads(app: App) -> list[dict]:
    here_names = {d.name for d in app.downloads}
    return [r for r in reversed(app.dl_history) if r.get("name") not in here_names]


def _download_viewport(app: App, height: int) -> tuple[int, int]:
    body_h = height - 2
    recent = bool(_recent_downloads(app))
    max_items = max(0, (body_h - (_RECENT_MIN_H if recent else 0)) // _DOWNLOAD_ITEM_H)
    if not app.downloads or not max_items:
        return 0, 0
    count = min(len(app.downloads), max(1, max_items))
    start = _window(app.dsel, len(app.downloads), count)
    return start, min(start + count, len(app.downloads))


def _download_rows(app: App, height: int) -> list[tuple[int, int]]:
    """Visible download indices and their bar rows relative to the panel."""
    start, end = _download_viewport(app, height)
    return [(idx, _DOWNLOAD_BAR_ROW + n * _DOWNLOAD_ITEM_H)
            for n, idx in enumerate(range(start, end))]


def _visible_download_rows(app: App, rows: int) -> list[tuple[int, int]]:
    rows = max(12, rows)
    panel_h = _main_heights(rows)[1]
    body_top = len(T.LOGO_LINES) + 1
    return [(idx, body_top + _DOWNLOADS_PREFIX + panel_row)
            for idx, panel_row in _download_rows(app, panel_h)]


def _downloads_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    body_h = height - 2
    live = app.downloads
    # Recently = past-session completions not currently in the live list (no dupes)
    recent = _recent_downloads(app)
    inner: list[str] = []
    if not live and not recent:
        inner.append(cell("No downloads yet. Find something and press d to grab it.", inner_w, dim=True))
        inner.append(cell("Press s to resume partial downloads on disk.", inner_w, dim=True))
        return _wrap_panel("Downloads", inner, width, height, app.view == "downloads")
    app.dsel = min(app.dsel, max(0, len(live) - 1))
    rows = _download_rows(app, height)
    if rows:
        for idx, _ in rows:
            d = live[idx]
            here = idx == app.dsel
            pct = int(d.progress * 100)
            if d.status == "error":
                icon, ic, base = T.ERR, T.BAD, T.BAD
                stats = dtrunc(redact(d.error or "failed", app._all_secrets()), 28)
            elif d.status == "complete":
                icon, ic, base = T.DONE, T.GOOD, T.GOOD
                stats = "done"
            elif d.status == "paused":
                icon, ic, base = T.PAUSE, T.PAUSED, T.PAUSED
                stats = f"paused  {pct}%"
            elif d.status == "metadata":
                icon, ic, base = T.DOWN, T.ACCENT, T.ACCENT
                stats = "fetching metadata…"
            else:  # active / waiting
                icon, ic, base = T.DOWN, T.ACCENT, T.ACCENT
                stats = f"{pct}%  {fmt_speed(d.speed)}  {T.PEER}{d.peers}" + (f"  {fmt_eta(d.eta)}" if d.eta else "")
            stat_w = min(dwidth(stats) + 1, inner_w - 6)
            inner.append(
                cell(icon, 2, color=ic) + cell(clean(d.name) or "…", inner_w - 2 - stat_w,
                                               color=T.ACCENT if here else None, bold=here, dim=not here)
                + cell(stats, stat_w, "right", dim=True))
            inner.append("  " + render_bar(d.progress, inner_w - 2, app.tick,
                                           d.status in ("active", "metadata"), base))
            inner.append(cell("", inner_w))
    if recent and len(inner) < body_h:
        inner.append(cell("Recently downloaded", inner_w, color=T.ALT, bold=True))
        for rec in recent:
            if len(inner) >= body_h:
                break
            right = f"{fmt_bytes(rec.get('size', 0))}  {fmt_rel(rec.get('ts'))}"
            rw = min(dwidth(right) + 1, inner_w - 6)
            inner.append(cell(T.DONE, 2, color=T.GOOD)
                         + cell(clean(rec.get("name", "?")), inner_w - 2 - rw, dim=True)
                         + cell(right, rw, "right", dim=True))
    return _wrap_panel("Downloads", inner, width, height, app.view == "downloads",
                       f"({len(live)})" if live else None)


def _settings_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    items = app.setting_items()
    body_h = max(1, height - 2)
    start = _window(app.set_sel, len(items), body_h)
    inner = []
    for idx in range(start, min(start + body_h, len(items))):
        kind, value = items[idx]
        selected = idx == app.set_sel
        prefix = cell(T.PTR if selected else "", 2, color=T.ACCENT)
        if kind == "dir":
            shown = app.edit_buf + "▌" if app.edit_field == "dir" else app.download_dir or "(from aria2.conf)"
            text, on = f"Download dir: {shown}", bool(app.download_dir or app.edit_field == "dir")
        elif kind == "provider":
            text, on = f"Metadata provider: {app.meta_provider.upper()}", True
        elif kind == "meta-key":
            key = app.edit_buf if app.edit_field == "key" else app._provider_key() or ""
            shown = "•" * min(len(key), 12) + ("▌" if app.edit_field == "key" else "")
            text, on = f"{app.meta_provider.upper()} key: {shown or '(not set)'}", bool(key)
        elif kind == "source":
            on = value.id not in app.disabled_sources
            text = f"Sources · [{'x' if on else ' '}] {value.label}  ·  {value.group}"
        elif kind == "feed":
            on = value["id"] not in app.disabled_sources
            if selected and app.edit_field == "feed-url":
                shown = redact_url(app.edit_buf, app._all_secrets()) + "▌"
            elif selected and app.edit_field == "feed-key":
                shown = "key: " + "•" * min(len(app.edit_buf), 12) + "▌"
            else:
                shown = torznab_label(value["url"])
            armed = "  [remove?]" if app.remove_feed == value["id"] else ""
            text = f"[{'x' if on else ' '}] {shown}{armed}"
        else:
            shown = (redact_url(app.edit_buf, app._all_secrets()) + "▌"
                     if selected and app.edit_field == "feed-url" else "")
            text, on = (shown or "+ Add Torznab feed"), True
        inner.append(prefix + cell(text, inner_w - 2, color=T.ACCENT if selected else None,
                                   bold=selected, dim=not selected and not on))
    return _wrap_panel("Settings", inner, width, height, True)


def _picker_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    files = app.picker_files
    inner = [cell(f"{len(app.picker_on)}/{len(files)} files · {fmt_bytes(app.picker_bytes)}",
                  inner_w, dim=True)]
    list_h = max(1, height - 2 - len(inner))
    start = _window(app.picker_sel, len(files), list_h)
    for idx in range(start, min(start + list_h, len(files))):
        f = files[idx]
        here = idx == app.picker_sel
        on = f["index"] in app.picker_on
        name = os.path.basename(f["path"]) or f["path"] or "?"
        inner.append(
            cell(T.PTR if here else "", 2, color=T.ACCENT)
            + cell("[x]" if on else "[ ]", 4, color=T.GOOD if on else T.RULE)
            + cell(clean(name), inner_w - 16, color=T.ACCENT if here else None,
                   bold=here, dim=not here and not on)
            + cell(fmt_bytes(f.get("length")), 10, "right", dim=True))
    return _wrap_panel("Files", inner, width, height, True,
                       f"({clean(app.picker.name)[:24]})" if app.picker else None)


def _help_panel(width: int, height: int) -> list[str]:
    inner_w = width - 4
    groups = [
        ("Search", [("type", "search (paste a magnet or link to grab)"), ("enter", "details"),
                     ("d", "download"), ("o", "open page in browser"), ("y", "copy magnet"),
                     ("/  i", "edit query"), ("↑ ↓", "recall past searches"),
                     ("r", "retry failed sources"),
                     ("filters", "seeders: size: age: files: source: group:"),
                     ("examples", 'matrix -cam size:>1GiB group:movies'),
                     ("S", "cycle sort (seeders/size/newest)"), ("c", "clear results"),
                     ("← →", "filter category"), ("v", "grab magnet/link from clipboard")]),
        ("Details", [("← →", "cycle duplicate source variants"),
                     ("d / y / o", "use selected variant")]),
        ("Settings", [("a", "add Torznab feed"), ("enter / e", "edit endpoint"),
                      ("k", "edit separate key"), ("space", "toggle source"),
                      ("x x", "arm / confirm feed removal")]),
        ("Navigate", [("↑ ↓  j k", "move selection / scroll wheel"),
                      ("tab", "switch search / downloads")]),
        ("Downloads", [("p", "pause / resume"), ("x", "cancel (ask: delete or keep files)"),
                       ("r", "retry a failed download"), ("f", "choose files (season packs)"),
                       ("o", "reveal in Finder"), ("s", "resume partial downloads on disk")]),
        ("General", [("g", "settings (sources, download dir)"), ("?", "this help"),
                     ("q", "quit (confirm)"), ("ctrl-c", "quit now")]),
    ]
    inner = [cell("Keys", inner_w, color=T.ACCENT, bold=True), cell("", inner_w)]
    for title, items in groups:
        inner.append(cell(title, inner_w, color=T.ALT, bold=True))
        for keys, desc in items:
            inner.append("  " + cell(keys, 12, color=T.BRIGHT) + " " + cell(desc, inner_w - 15, dim=True))
        inner.append(cell("", inner_w))
    return _wrap_panel("Help", inner, width, height, True)


def _footer(app: App, width: int) -> str:
    if app.cancel_prompt is not None:
        hints = [("d", "delete files"), ("k", "keep files"), ("esc", "abort")]
    elif app.torrent_prompt is not None:
        hints = [("t", "contents"), ("f", ".torrent file"), ("esc", "cancel")]
    elif app.help:
        hints = [("any key", "close")]
    elif app.settings:
        hints = ([("type", "value"), ("enter", "save"), ("esc", "cancel")] if app.edit_field
                 else [("↑↓", "move"), ("a", "add"), ("e", "endpoint"), ("k", "key"),
                       ("space", "toggle"), ("x", "remove"), ("g/esc", "close")])
    elif app.picker is not None:
        hints = [("↑↓", "move"), ("space", "toggle"), ("a", "all/none"),
                 ("enter", "apply"), ("esc", "cancel")]
    elif app.detail is not None:
        hints = [("←→", "variant"), ("d", "download"), ("o", "page"), ("y", "copy"), ("p", "poster"), ("esc/q", "back")]
    elif app.view == "search" and app.editing:
        hints = [("enter", "search"), ("↑↓", "history"), ("esc", "nav"), ("tab", "downloads"), ("^c", "quit")]
    elif app.view == "search":
        hints = [("↑↓", "move"), ("enter", "details"), ("d", "grab"), ("o", "page"), ("y", "copy"),
                 ("r", "retry"), ("S", "sort"), ("←→", "category"), ("v", "paste"), ("g", "settings"), ("q", "quit")]
    else:
        hints = [("↑↓", "move"), ("p", "pause/resume"), ("x", "cancel"), ("r", "retry"),
                 ("f", "files"), ("o", "reveal"), ("s", "resume"), ("g", "settings"),
                 ("tab", "search"), ("q", "quit")]
    out, used = "", 0
    if app.status:
        st = dtrunc(redact(clean(app.status), app._all_secrets()), max(10, width // 2))
        out = style(st, T.ALT) + "   "
        used = dwidth(st) + 3
    sep = "  " + T.DOT + "  "
    sep_w = dwidth(sep)
    last = hints[-1]  # the quit hint must always survive truncation
    last_w = sep_w + dwidth(last[0]) + 1 + dwidth(last[1])
    first = True
    for k, v in hints[:-1]:
        add = dwidth(k) + 1 + dwidth(v) + (0 if first else sep_w)
        if used + add + last_w > width:  # keep room for the quit hint
            break
        if not first:
            out += style(sep, dim=True)
        out += style(k, T.ACCENT) + style(" " + v, dim=True)
        used += add
        first = False
    if not first:
        out += style(sep, dim=True)
    out += style(last[0], T.ACCENT) + style(" " + last[1], dim=True)
    return out


TAGLINE = "A curated, terminal-native torrent & book finder."
CATS_LINE = "games  ·  movies  ·  tv  ·  anime  ·  books"


def _center(line: str, plain_w: int, cols: int) -> str:
    return " " * max(0, (cols - plain_w) // 2) + line


def _splash(app: App, cols: int, rows: int) -> list[str]:
    """torlink's calm welcome: centered gradient logo, tagline, search box, hints."""
    logo_w = max(dwidth(s) for s in T.LOGO_LINES)
    left = max(0, (cols - logo_w) // 2)
    block = [" " * left + L for L in _logo_lines()]
    block += ["", _center(style(TAGLINE, T.TEXT), dwidth(TAGLINE), cols),
              _center(style(CATS_LINE, dim=True), dwidth(CATS_LINE), cols), ""]
    box_w = min(64, cols - 8)
    editing = app.view == "search" and app.editing
    box = _wrap_panel("Search", [_search_line(app, box_w - 4)], box_w, 3, editing)
    bleft = max(0, (cols - box_w) // 2)
    block += [" " * bleft + b for b in box]
    if editing:
        hints = [("↵", "search"), ("esc", "back"), ("tab", "downloads")]
    else:
        hints = [("type", "to search"), ("↵", "browse"), ("q", "quit")]
    parts, plain = [], 0
    for i, (k, v) in enumerate(hints):
        if i:
            parts.append(style("   ", dim=True))
            plain += 3
        parts.append(style(k, T.ALT) + style(" " + v, dim=True))
        plain += dwidth(k) + 1 + dwidth(v)
    block += ["", _center("".join(parts), plain, cols)]
    if app.status:
        st = dtrunc(redact(clean(app.status), app._all_secrets()), cols - 4)
        block += ["", _center(style(st, T.ALT), dwidth(st), cols)]
    top = max(0, (rows - len(block)) // 2)
    return ([""] * top + block + [""] * rows)[:rows]

def _modal_box(cols: int, label: str, hints: list[tuple[str, str]], color: str) -> list[str]:
    plain = "  " + label + "   " + "  ·  ".join(f"{k} {v}" for k, v in hints) + "  "
    box_w = dwidth(plain) + 2
    pre = " " * max(0, (cols - box_w) // 2)
    inner = "  " + style(label, color, bold=True) + "   "
    for i, (k, v) in enumerate(hints):
        if i:
            inner += style("  ·  ", dim=True)
        inner += style(k, T.ACCENT) + style(" " + v, dim=True)
    inner += "  "
    return [
        pre + style("╭" + "─" * (box_w - 2) + "╮", color),
        pre + style("│", color) + inner + style("│", color),
        pre + style("╰" + "─" * (box_w - 2) + "╯", color),
    ]


def _confirm(cols: int) -> list[str]:
    return _modal_box(cols, "Quit trawl?", [("↵", "yes"), ("esc", "no")], T.WARN)


def _torrent_box(cols: int) -> list[str]:
    return _modal_box(cols, ".torrent link — download",
                      [("t", "contents"), ("f", "the .torrent"), ("esc", "cancel")], T.ACCENT)


def _cancel_box(cols: int) -> list[str]:
    return _modal_box(cols, "Cancel download",
                      [("d", "delete files"), ("k", "keep files"), ("esc", "abort")], T.WARN)


def _overlay(lines: list[str], app: App, cols: int, rows: int) -> list[str]:
    box = (_confirm(cols) if app.confirm_quit
           else _torrent_box(cols) if app.torrent_prompt
           else _cancel_box(cols) if app.cancel_prompt
           else None)
    if not box:
        return lines
    mid = _overlay_rows(rows).start
    for j, b in enumerate(box):
        if mid + j < len(lines):
            lines[mid + j] = b
    return lines


def _overlay_rows(rows: int) -> range:
    rows = max(12, rows)
    mid = max(0, rows // 2 - 1)
    return range(mid, min(rows, mid + 3))


def _main_heights(rows: int) -> tuple[int, int]:
    rows = max(12, rows)
    body_h = rows - len(T.LOGO_LINES) - 3
    return body_h, body_h - 4


def _redact_frame(lines: list[str], app: App) -> list[str]:
    candidates = {form for secret in app._all_secrets() for form in
                  (secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret))
                  if form}
    if not candidates:
        return lines
    secrets = re.compile("|".join(map(re.escape, sorted(candidates, key=len, reverse=True))), re.I)

    def redact_plain(text: str) -> str:
        return secrets.sub(lambda m: "*" * (dwidth(m.group()) or 3), text)

    def redact_line(line: str) -> str:
        out, end = [], 0
        for ansi in _ANSI.finditer(line):
            out.extend((redact_plain(line[end:ansi.start()]), ansi.group()))
            end = ansi.end()
        out.append(redact_plain(line[end:]))
        return "".join(out)

    return [redact_line(line) for line in lines]


def render(app: App, cols: int, rows: int) -> list[str]:
    cols = max(40, cols)
    rows = max(12, rows)
    if app.view == "search" and app.search is None and not app.help and not app.settings:
        return _redact_frame(_overlay(_splash(app, cols, rows), app, cols, rows), app)
    lines: list[str] = []
    for L in _logo_lines():
        lines.append(" " * MARGIN + L)
    rule_w = max(0, cols - 2 * MARGIN)
    if app.down_speed > 0 or app.num_active > 0:
        stat = f" {T.DOWN} {fmt_speed(app.down_speed)}  {app.num_active} active "
        dashes = max(0, rule_w - dwidth(stat) - 2)
        lines.append(" " * MARGIN + style("─" * dashes + "─", T.RULE)
                     + style(stat, T.ALT) + style("─", T.RULE))
    else:
        lines.append(" " * MARGIN + style("─" * rule_w, T.RULE))

    body_h, panel_h = _main_heights(rows)
    content_w = cols - MARGIN - RAIL_W - GAP - 1

    if app.help:
        content = _help_panel(content_w, body_h)
        rail = [cell("", RAIL_W)] * body_h
    elif app.settings:
        content = _settings_panel(app, content_w, body_h)
        rail = [cell("", RAIL_W)] * body_h
    elif app.picker is not None:
        content = _picker_panel(app, content_w, body_h)
        rail = [cell("", RAIL_W)] * body_h
    else:
        rail = _rail(app, body_h)
        content = _search_panel(app, content_w) + [""]
        if app.view == "search" and app.detail is not None:
            content += _detail_panel(app, app.detail, content_w, panel_h)
        elif app.view == "search":
            content += _results_panel(app, content_w, panel_h)
        else:
            content += _downloads_panel(app, content_w, panel_h)
    content = (content + [""] * body_h)[:body_h]

    for i in range(body_h):
        lines.append(" " * MARGIN + rail[i] + " " * GAP + content[i])

    lines.append("")
    lines.append(" " * MARGIN + _footer(app, cols - MARGIN))
    lines = (lines + [""] * rows)[:rows]
    return _redact_frame(_overlay(lines, app, cols, rows), app)


# -- self-check --------------------------------------------------------------


def selftest() -> None:
    # width primitives
    assert dwidth("abc") == 3 and dwidth("日本") == 4, "east-asian width"
    assert dwidth(strip_ansi(cell("hi", 10))) == 10, "cell pads to width"
    assert dtrunc("hello world", 5) == "hell…", dtrunc("hello world", 5)
    def reference_bar(progress, width, tick, animate, base=T.ACCENT):
        if width <= 0:
            return ""
        filled = round(max(0.0, min(1.0, progress)) * width)
        denom = max(1, width - 1)
        cells = [T.progress_ramp(i / denom, T.DEEP, base, T.BRIGHT)
                 for i in range(filled)]
        if animate:
            center = T.sheen_center(tick, T.sheen_period(width))
            lo = max(0, math.floor(center - T.SHEEN_RADIUS) + 1)
            hi = min(filled, math.ceil(center + T.SHEEN_RADIUS))
            for i in range(lo, hi):
                intensity = T.sheen_intensity(i, center)
                if intensity > 0:
                    cells[i] = T.lerp_hex(cells[i], T.SHEEN_PEAK, intensity)
        return _styled_bar(cells, width - filled)

    for width in (0, 1, 2, 10, 17):
        for progress in (-0.2, 0, .33, .5, 1, 1.2):
            for tick in (0, 3.25, 999):
                for base in (T.ACCENT, T.BAD):
                    for animate in (False, True):
                        got = render_bar(progress, width, tick, animate, base)
                        assert got == reference_bar(progress, width, tick, animate, base), \
                            (width, progress, tick, base, animate, got.encode())
    static = render_bar(0.5, 10, 0, False)
    assert strip_ansi(static).count("█") == 5, "bar half full"
    before = _static_bar.cache_info().hits
    assert render_bar(0.5, 10, 999, False) == static
    assert _static_bar.cache_info().hits == before + 1, "static bar cache"
    _bar_cells.cache_clear(); _static_bar.cache_clear()
    for width in range(1, 201):
        render_bar(.37, width, width, True)
        render_bar(.37, width, 0, False)
    assert _bar_cells.cache_info().maxsize == 64 and _bar_cells.cache_info().currsize <= 64
    assert _static_bar.cache_info().maxsize == 128 and _static_bar.cache_info().currsize <= 128

    # terminal frame suppression/diffing without a TTY
    class _Out:
        def __init__(self): self.data, self.flushes = "", 0
        def write(self, s): self.data += s
        def flush(self): self.flushes += 1

    old_stdout, fake = sys.stdout, _Out()
    sys.stdout = fake  # type: ignore[assignment]
    try:
        term = Terminal()
        assert term.write(["one", "two-long"], (80, 24)) is True and fake.flushes == 1
        fake.data = ""
        assert term.write(["one", "two-long"], (80, 24)) is False
        assert fake.data == "" and fake.flushes == 1, "unchanged frame must not flush"
        assert term.write(["one", "two"], (80, 24)) is True
        assert fake.flushes == 2 and "\x1b[2;1Htwo\x1b[K" in fake.data, fake.data
        fake.data = ""
        assert term.write(["one"], (80, 24)) is True
        assert fake.flushes == 3 and "\x1b[2J" in fake.data, "line shrink fully clears"
        fake.data = ""
        assert term.write([], (80, 24)) is True
        assert fake.flushes == 4 and "\x1b[2J" in fake.data, "zero-line frame clears"
        fake.data = ""
        assert term.write([], (100, 24)) is True
        assert fake.flushes == 5 and "\x1b[2J" in fake.data, "size change fully clears"
        term._reset_frame()
        assert term._lines is None and term._size is None, "enter frame invalidation"
    finally:
        sys.stdout = old_stdout

    # key parsing
    assert parse_keys(b"\x1b[A") == ["up"]
    assert parse_keys(b"ab\r\x7f\t\x03") == ["a", "b", "enter", "backspace", "tab", "ctrl-c"]
    assert parse_keys("café".encode()) == ["c", "a", "f", "é"]
    assert parse_keys(b"\x1b[<64;10;5M") == ["up"], "wheel up"
    assert parse_keys(b"\x1b[<65;10;5M") == ["down"], "wheel down"
    assert parse_keys(b"\x1b[<0;1;1M") == [], "click ignored, sequence consumed"
    assert parse_keys(b"a\x1b[<64;1;1Mb") == ["a", "up", "b"], "mouse mid-stream"
    print("primitives ok")

    # interaction: edit -> type -> submit -> nav -> tab -> downloads
    app = App(eng=None)
    assert app.view == "search" and not app.editing and app.search is None, "splash landing"
    app.on_key("q")
    assert app.confirm_quit, "q quits on the splash landing"
    app.on_key("esc")
    assert not app.confirm_quit
    for ch in "matrix":
        app.on_key(ch)
    assert app.query == "matrix" and app.editing, "typing starts editing"
    app.on_key("backspace")
    assert app.query == "matri"
    # arrow-key editing: caret moves, inserts/deletes land mid-string
    assert app.cursor == 5, app.cursor  # caret at end of "matri"
    app.on_key("left"); app.on_key("left")
    assert app.cursor == 3, app.cursor
    app.on_key("x")  # insert at caret -> "matxri"
    assert app.query == "matxri" and app.cursor == 4, (app.query, app.cursor)
    app.on_key("backspace")  # delete char before caret -> "matri"
    assert app.query == "matri" and app.cursor == 3, (app.query, app.cursor)
    for _ in range(5):
        app.on_key("left")
    assert app.cursor == 0, app.cursor  # clamps at start
    app.on_key("right")
    assert app.cursor == 1, app.cursor
    app.search = Search.__new__(Search)  # simulate a completed search -> browse nav
    app.search_done = app.search_total
    app.results = [
        Result("a" * 40, "The Matrix 1999 [1080p]", 1_500_000_000, 900, 30, "yts", "magnet:?xt=m"),
        Result("b" * 40, "The Matrix Reloaded", 2_000_000_000, 0, 0, "fitgirl", "magnet:?xt=m"),
    ]
    app.editing = False
    app.on_key("down")
    assert app.sel == 1, app.sel
    grabbed = {}
    app.grab = lambda m, name: grabbed.update(name=name)  # type: ignore
    app.on_key("d")
    assert grabbed.get("name") == "The Matrix Reloaded", grabbed
    app.on_key("right")
    assert app.cat == "games", app.cat
    app.on_key("c")  # clear -> back to splash landing
    assert app.search is None and app.results == () and not app.editing, "c clears to splash"
    app.on_key("tab")
    assert app.view == "downloads"
    # quit confirmation: q arms it, esc cancels, q+enter quits; ^c is immediate
    appq = App(eng=None)
    appq.view = "downloads"
    appq.on_key("q")
    assert appq.confirm_quit and appq.running, "q should arm confirm, not quit"
    appq.on_key("esc")
    assert not appq.confirm_quit and appq.running, "esc cancels quit"
    appq.on_key("q")
    appq.on_key("enter")
    assert not appq.running, "q then enter quits"
    appq.running = True
    appq.on_key("ctrl-c")
    assert not appq.running, "ctrl-c quits immediately"
    # downloads: p toggles pause/resume, x cancels — all routed to the engine
    calls = []

    class _FakeEng:
        def pause(self, r): calls.append(("pause", r))
        def resume(self, r): calls.append(("resume", r))
        def remove(self, r): calls.append(("remove", r))
        def file_paths(self, r): calls.append(("files", r)); return []
        def download_dir(self): return None

    appd = App(eng=_FakeEng())
    appd.view = "downloads"
    appd.downloads = [Download("g", "F", "active", 100, 10, 5, 1, None, root="r1")]
    appd.on_key("p")
    assert calls == [("pause", "r1")], calls
    appd.downloads[0].status = "paused"
    appd.on_key("p")
    assert calls[-1] == ("resume", "r1"), calls
    appd.on_key("x")  # x opens the cancel prompt; nothing removed yet
    assert appd.cancel_prompt is appd.downloads[0] and "remove" not in [c[0] for c in calls], calls
    appd.on_key("d")  # delete files + entry
    assert ("files", "r1") in calls and calls[-1] == ("remove", "r1") and appd.cancel_prompt is None, calls
    # remove_download_files: deletes file + .aria2, prunes the emptied subfolder,
    # keeps the download dir itself.
    import tempfile
    _td = tempfile.mkdtemp()
    _sub = os.path.join(_td, "Show"); os.makedirs(_sub)
    _f = os.path.join(_sub, "ep.mkv"); open(_f, "w").close(); open(_f + ".aria2", "w").close()
    assert remove_download_files([_f], _td, "Show") == 2, "deleted file + control file"
    assert not os.path.exists(_sub) and os.path.isdir(_td), "subfolder pruned, dl dir kept"
    # copy magnet (y), open page (o, search), reveal (o, downloads) route to helpers
    g = globals()
    orig = {k: g[k] for k in ("copy_clipboard", "reveal", "open_url")}
    hit = []
    g["copy_clipboard"] = lambda t: hit.append(("copy", t)) or True
    g["reveal"] = lambda p: hit.append(("reveal", p)) or True
    g["open_url"] = lambda u: hit.append(("open", u)) or True
    appy = App(eng=None)
    appy.search = Search.__new__(Search)
    appy.editing = False
    appy.results = [Result("a" * 40, "X", 1, 1, 0, "yts", "magnet:?xt=test",
                          page="https://yts.mx/movies/x")]
    appy.on_key("y")
    assert hit == [("copy", "magnet:?xt=test")], hit
    appy.on_key("o")  # search view: open the torrent page
    assert hit[-1] == ("open", "https://yts.mx/movies/x"), hit
    appy.view = "downloads"
    appy.downloads = [Download("g", "F", "complete", 1, 1, 0, 0, None, root="r", path="/tmp/F")]
    appy.on_key("o")  # downloads view: reveal in Finder
    assert hit[-1] == ("reveal", "/tmp/F"), hit
    for k, v in orig.items():
        g[k] = v
    # details view: enter opens, esc closes, d grabs from it
    appx = App(eng=None)
    appx.search = Search.__new__(Search)
    appx.editing = False
    appx.tmdb_key = None  # keep offline; the meta lookup path is tested separately
    appx.results = [Result("c" * 40, "Some Movie", 1, 5, 1, "yts", "magnet:?xt=z", page="http://p")]
    appx.on_key("enter")
    assert appx.detail is appx.results[0], "enter opens details"
    appx.on_key("esc")
    assert appx.detail is None, "esc closes details"
    appx.on_key("enter")
    grabbed2 = {}
    appx.grab = lambda m, n: grabbed2.update(m=m)  # type: ignore
    appx.on_key("d")
    assert grabbed2.get("m") == "magnet:?xt=z" and appx.detail is None, "d grabs from details"

    # s scans for resumables via the engine; metadata reveal gives a clear message
    class _ScanEng:
        def download_dir(self): return "/no-such-dir-xyz"
        def active_infohashes(self): return set()

    apps = App(eng=_ScanEng())
    apps.view = "downloads"
    apps.on_key("s")
    assert apps.status == "nothing to resume on disk", apps.status
    apps.downloads = [Download("g", "m", "metadata", 0, 0, 0, 0, None, root="r", path="")]
    apps.dsel = 0
    apps.on_key("o")
    assert "metadata" in apps.status, apps.status
    # r retries only errored downloads, routed to the engine
    rcalls = []

    class _RetryEng:
        def retry(self, r): rcalls.append(r); return "newgid"
        def files(self, r): return []

    appr = App(eng=_RetryEng())
    appr.view = "downloads"
    appr.downloads = [Download("g", "F", "active", 100, 10, 5, 1, None, root="r1")]
    appr.on_key("r")
    assert rcalls == [] and "retries a failed" in appr.status, appr.status
    appr.downloads[0].status = "error"
    appr.on_key("r")
    assert rcalls == ["r1"] and appr.status.startswith("retrying"), (rcalls, appr.status)
    # f opens the file picker (2+ files); space toggles, a flips all, enter applies
    sel_calls = []

    class _PickEng:
        def __init__(self):
            self.current = [{"index": 1, "path": "/d/S01E01.mkv", "length": 700, "selected": True},
                            {"index": 2, "path": "/d/S01E02.mkv", "selected": True}]
        def files(self, r):
            return self.current
        def select_files(self, r, idx): sel_calls.append((r, idx)); return True

    pick_eng = _PickEng()
    appf = App(eng=pick_eng)
    appf.view = "downloads"
    appf.downloads = [Download("g", "Pack", "active", 100, 10, 5, 1, None, root="rp")]
    appf.on_key("f")
    assert appf.picker is not None and appf.picker_on == {1, 2}, appf.picker_on
    assert appf.picker_bytes == 700, appf.picker_bytes
    appf.on_key(" ")  # toggle file 1 off
    assert appf.picker_on == {2} and appf.picker_bytes == 0, (appf.picker_on, appf.picker_bytes)
    appf.on_key("a")  # all
    assert appf.picker_on == {1, 2} and appf.picker_bytes == 700
    appf.on_key("a")  # none
    assert appf.picker_bytes == 0
    appf.on_key("enter")  # empty selection refused
    assert appf.picker is not None and "at least one" in appf.status
    appf.on_key(" ")
    appf.on_key("enter")
    assert sel_calls == [("rp", [1])] and appf.picker is None, sel_calls
    pick_eng.current = [{"index": 1, "path": "/d/S02E01.mkv", "length": 900, "selected": False},
                        {"index": 2, "path": "/d/S02E02.mkv", "length": 1100, "selected": True}]
    appf.on_key("f")
    assert [f["path"] for f in appf.picker_files] == ["/d/S02E01.mkv", "/d/S02E02.mkv"]
    assert appf.picker_on == {2} and appf.picker_bytes == 1100, "reopen refreshes selection bytes"
    appf.on_key("esc")
    # single-file download: picker refuses to open
    class _OneEng:
        def files(self, r): return [{"index": 1, "path": "/d/f.iso", "length": 1, "selected": True}]
    app1 = App(eng=_OneEng())
    app1.view = "downloads"
    app1.downloads = [Download("g", "F", "active", 1, 0, 0, 0, None, root="r")]
    app1.on_key("f")
    assert app1.picker is None and "nothing to pick" in app1.status, app1.status
    # picker renders width-safe
    appf.on_key("f")
    appf2_frames = render(appf, 100, 30)
    jp = "\n".join(strip_ansi(x) for x in appf2_frames)
    assert "Files" in jp and "S02E01.mkv" in jp, jp
    for ln in appf2_frames:
        assert dwidth(strip_ansi(ln)) <= 100, "picker overflow"
    appf.on_key("esc")
    assert appf.picker is None
    # completion notification: once on active->complete, never for pre-complete/staying
    gn = globals()
    orig_notify, orig_append, notes = gn["notify"], gn["append_dl_history"], []
    gn["notify"] = lambda t, m: notes.append((t, m))
    gn["append_dl_history"] = lambda r: None  # don't touch the real file
    appn = App(eng=None)
    appn.dl_history = []
    appn.update_downloads([Download("g", "Movie", "active", 100, 50, 1, 1, None, root="r1"),
                           Download("g2", "Old", "complete", 1, 1, 0, 0, None, root="r2")])
    assert notes == [], "no notify on first sight (incl already-complete)"
    appn.update_downloads([Download("g", "Movie", "complete", 100, 100, 0, 0, None, root="r1"),
                           Download("g2", "Old", "complete", 1, 1, 0, 0, None, root="r2")])
    assert notes == [("trawl — download complete", "Movie")], notes
    assert appn.dl_history and appn.dl_history[-1]["name"] == "Movie", appn.dl_history
    appn.update_downloads([Download("g", "Movie", "complete", 100, 100, 0, 0, None, root="r1")])
    assert len(notes) == 1, "no re-notify while staying complete"
    assert len(appn.dl_history) == 1, "history recorded once"
    gn["notify"], gn["append_dl_history"] = orig_notify, orig_append
    # search history: ↑/↓ recall past queries; add dedups + moves to end + saves
    gh = globals()
    orig_load, orig_save, saved = gh["load_history"], gh["save_history"], []
    gh["load_history"] = lambda: ["alpha", "beta"]
    gh["save_history"] = lambda h: (saved.clear(), saved.extend(h))
    apph = App(eng=None)
    assert apph.history == ["alpha", "beta"] and apph.hist_idx == 2
    apph.editing = True
    apph.query = "ga"
    apph.on_key("up")
    assert apph.query == "beta", apph.query
    apph.on_key("up")
    assert apph.query == "alpha"
    apph.on_key("down")
    apph.on_key("down")
    assert apph.query == "ga", apph.query  # back to the live draft
    apph._add_history("alpha")
    assert apph.history == ["beta", "alpha"] and saved == ["beta", "alpha"], (apph.history, saved)
    gh["load_history"], gh["save_history"] = orig_load, orig_save
    # sort toggle (S) cycles seeders -> size -> newest and reorders
    appso = App(eng=None)
    appso.search = Search.__new__(Search)
    appso.editing = False
    appso.results = [Result("1" + "x" * 39, "small-many", 1, 99, 0, "yts", "m"),
                     Result("2" + "x" * 39, "huge-few", 9_000_000_000, 1, 0, "yts", "m")]
    assert appso.sort == "seeders"
    appso.on_key("S")
    assert appso.sort == "size" and appso.results[0].name == "huge-few", appso.sort
    appso.on_key("S")
    assert appso.sort == "newest"
    appso.on_key("S")
    assert appso.sort == "seeders" and appso.results[0].name == "small-many"
    # Latest (empty query) fans out only to browse-capable sources
    gsr = globals()
    o_search = gsr["Search"]
    cap = {}
    class _CapSearch:
        def __init__(self, q, srcs=None):
            cap["srcs"] = srcs
    gsr["Search"] = _CapSearch
    appl = App(eng=None)
    appl.submit()  # empty query -> Latest
    assert cap["srcs"] and all(s.browse for s in cap["srcs"]), "Latest skips non-browse sources"
    assert len(cap["srcs"]) == sum(1 for s in SOURCES if s.browse) == appl.search_total, cap
    gsr["Search"] = o_search
    # "All" interleaves categories so a prolific group can't monopolize the top
    appi = App(eng=None)
    appi.results = [
        Result("a1" + "x" * 38, "anime1", 1, 500, 0, "nyaa", "m"),
        Result("a2" + "x" * 38, "anime2", 1, 400, 0, "nyaa", "m"),
        Result("a3" + "x" * 38, "anime3", 1, 300, 0, "nyaa", "m"),
        Result("t1" + "x" * 38, "tv1", 1, 50, 0, "tpb-tv", "m"),
    ]
    assert [r.name for r in appi.visible_results()] == ["anime1", "tv1", "anime2", "anime3"], \
        [r.name for r in appi.visible_results()]
    cached = appi.visible_results()
    assert appi.visible_results() is cached, "visible results identity cache"
    appi.results = appi.results[:]
    assert appi.visible_results() is not cached, "results replacement invalidates"
    cached = appi.visible_results()
    appi.cat = "anime"
    assert appi.visible_results() is not cached, "category invalidates"
    try:
        appi.visible_results().append(appi.results[0])  # type: ignore[attr-defined]
        assert False, "cached output must be immutable"
    except AttributeError:
        pass
    try:
        appi.results.append(appi.results[0])  # type: ignore[attr-defined]
        assert False, "stored results must be immutable"
    except AttributeError:
        pass
    # Queue updates replace the source once per batch; clear and sort do likewise.
    class _QueuedSearch:
        def __init__(self): self.updates = queue.Queue()
    appc = App(eng=None)
    appc.search = _QueuedSearch()  # type: ignore[assignment]
    first = Result("q1" + "x" * 38, "first", 1, 1, 0, "yts", "m")
    second = Result("q2" + "x" * 38, "second", 2, 9, 0, "yts", "m")
    appc.results = [first]
    before_results, before_revision = appc.results, appc._results_revision
    update = type("Update", (), {"source": "yts", "results": [second], "error": ""})()
    appc.search.updates.put(update)
    appc.drain_search()
    assert appc.results is not before_results and appc._results_revision == before_revision + 1
    assert {r.name for r in appc.visible_results()} == {"first", "second"}, "append-like update"
    cached = appc.visible_results()
    appc._cycle_sort()
    assert appc.visible_results() is not cached and appc.results[0] is second, "sort replacement"
    cached = appc.visible_results()
    appc.results = []
    assert appc.visible_results() == () and appc.visible_results() is not cached, "clear replacement"
    # clipboard grab (v): a magnet on the clipboard gets grabbed
    gp = globals()
    orig_paste = gp["paste_clipboard"]
    gp["paste_clipboard"] = lambda: "magnet:?xt=urn:btih:" + "a" * 40
    appv = App(eng=None)
    appv.search = Search.__new__(Search)
    appv.editing = False
    got = {}
    appv.grab = lambda m, n: got.update(m=m)  # type: ignore
    appv.on_key("v")
    assert got.get("m", "").startswith("magnet:?"), got
    gp["paste_clipboard"] = lambda: "https://example.com/f.iso"  # a direct link grabs too
    appv.on_key("v")
    assert got.get("m") == "https://example.com/f.iso", got
    gp["paste_clipboard"] = lambda: "not a magnet or link"
    appv.on_key("v")
    assert appv.status == "no magnet or link in clipboard", appv.status
    gp["paste_clipboard"] = orig_paste
    # .torrent link: submit opens the file-vs-contents prompt (no immediate grab);
    # t = contents (follow-torrent), f = the .torrent file; a plain link grabs directly.
    appt = App(eng=None)
    grabbed: dict = {}
    appt.grab = lambda m, n: grabbed.update(direct=m)  # type: ignore
    appt.grab_torrent = lambda u, n, contents: grabbed.update(url=u, tor=contents)  # type: ignore
    appt.query, appt.editing = "https://s.org/book.torrent", True
    appt.submit()
    assert appt.torrent_prompt is not None and "url" not in grabbed, grabbed
    appt.on_key("f")
    assert grabbed == {"url": "https://s.org/book.torrent", "tor": False}, grabbed
    assert appt.torrent_prompt is None
    appt.query, appt.editing = "https://s.org/b2.torrent", True
    appt.submit(); appt.on_key("t")
    assert grabbed["tor"] is True and grabbed["url"].endswith("b2.torrent"), grabbed
    appt.query, appt.editing = "https://s.org/file.iso", True
    appt.submit()
    assert grabbed.get("direct") == "https://s.org/file.iso" and appt.torrent_prompt is None
    # settings overlay: rows are 0 dir, 1 provider, 2 key, 3.. sources — all persisted
    gc = globals()
    o_load_cfg, o_save_cfg, saved_cfg = gc["load_config"], gc["save_config"], {}
    gc["load_config"] = lambda: {}
    gc["save_config"] = lambda c: saved_cfg.update(c)
    appg = App(eng=None)
    appg.view = "downloads"  # g on the splash landing types; open from a nav view
    assert appg.disabled_sources == set() and not appg.settings
    appg.on_key("g")
    assert appg.settings, "g opens settings"
    # provider toggle (row 1) flips tmdb<->omdb and persists
    assert appg.meta_provider == "tmdb"
    appg.set_sel = 1
    appg.on_key(" ")
    assert appg.meta_provider == "omdb" and saved_cfg["meta_provider"] == "omdb", saved_cfg
    appg.on_key(" ")
    assert appg.meta_provider == "tmdb", "provider toggles back"
    # key entry (row 2) writes the active provider's key
    appg.set_sel = 2
    appg.tmdb_key = None
    appg.on_key("enter")
    assert appg.edit_field == "key"
    for ch in "abc123":
        appg.on_key(ch)
    appg.on_key("enter")
    assert appg.tmdb_key == "abc123" and saved_cfg["tmdb_key"] == "abc123", saved_cfg
    # source toggle (row 3+)
    appg.set_sel = 3
    sid = SOURCES[0].id
    appg.on_key(" ")
    assert sid in appg.disabled_sources and sid in saved_cfg["disabled_sources"], saved_cfg
    assert SOURCES[0] not in appg.enabled_sources()
    appg.on_key(" ")
    assert sid not in appg.disabled_sources, "toggle back on"
    # download dir (row 0)
    appg.set_sel = 0
    appg.on_key("enter")
    assert appg.edit_field == "dir"
    for ch in "/tmp/dl":
        appg.on_key(ch)
    appg.on_key("enter")
    assert appg.download_dir == "/tmp/dl" and saved_cfg["download_dir"] == "/tmp/dl", saved_cfg
    appg.on_key("g")
    assert not appg.settings, "g closes settings"
    gc["load_config"], gc["save_config"] = o_load_cfg, o_save_cfg

    # v0.3 integration: config migration, dynamic feeds/settings, local filtering,
    # retries, variants, and secret-safe full frames. Everything here is offline.
    gv = globals()
    originals = {k: gv[k] for k in ("load_config", "load_history", "load_dl_history",
                                     "save_config", "save_history", "open_url",
                                     "copy_clipboard")}
    saved_v3: dict = {}
    sentinel = "SECRETSENTINEL"
    feed = {"id": "torznab-test", "url": f"https://indexer.invalid/api?foo={sentinel}&apikey=url-key",
            "api_key": sentinel}
    bad_records = [None, {}, {"id": SOURCES[0].id, "url": "https://duplicate.invalid/api"},
                   {"id": "duplicate", "url": "https://one.invalid/api"},
                   {"id": "duplicate", "url": "https://two.invalid/api"},
                   {"id": "invalid", "url": "file:///tmp/feed"}, feed]
    gv["load_history"] = lambda: []
    gv["load_dl_history"] = lambda: []
    gv["save_history"] = lambda h: None
    gv["save_config"] = lambda c: (saved_v3.clear(), saved_v3.update(c))
    builtins_before = tuple(SOURCES)
    try:
        gv["load_config"] = lambda: {"unknown": {"keep": True}, "torznab_feeds": bad_records}
        av = App()
        assert [f["id"] for f in av.torznab_feeds] == ["duplicate", feed["id"]], \
            "invalid feed records were not ignored"
        av._save_settings()
        assert saved_v3["unknown"] == {"keep": True} and saved_v3["torznab_feeds"] == av.torznab_feeds, \
            "config persistence did not preserve exact feed and unknown values"
        assert tuple(SOURCES) == builtins_before, "dynamic feeds mutated global sources"
        retained_label = av.source_label(feed["id"])
        assert retained_label != feed["id"] and av.result_group(
            Result("", "x", 0, 0, 0, feed["id"], "m")) == "Other", "dynamic source maps"
        assert av.source_secrets_for(feed["id"]) == (sentinel, "url-key"), "source secret extraction"
        av.disabled_sources.add(feed["id"])
        assert feed["id"] not in {s.id for s in av.enabled_sources()}, "enabled dynamic sources"
        av.results = [Result("f" * 40, "kept", 1, 1, 0, feed["id"], "magnet:?xt=kept")]
        dynamic = av.torznab_feeds[-1]
        av._remove_selected_feed(dynamic)
        assert av.results[0].name == "kept" and av.source_label(feed["id"]) == retained_label, \
            "feed removal changed results or discarded retained labels"
        assert av.source_secrets_for(feed["id"]) == (sentinel, "url-key"), "removed secret map not retained"

        gv["load_config"] = lambda: {}
        old = App()
        assert old.torznab_feeds == [], "old config without feeds"

        # One shared setting_items list controls navigation and rendering, including scrolling.
        aset = App()
        aset.settings = True
        aset.set_sel = len(aset.setting_items()) - 1
        assert "+ Add Torznab feed" in "\n".join(strip_ansi(x) for x in _settings_panel(aset, 70, 6)), \
            "settings selection/render list drift"
        aset.on_key("enter")
        assert aset.edit_field == "feed-url", "add-feed enter did not edit URL"
        aset.edit_buf = f"https://new.invalid/api?apikey={sentinel}"
        assert sentinel not in "\n".join(_settings_panel(aset, 70, 6)), "add-feed URL edit leaked secret"
        aset.edit_buf = "bad"
        aset.on_key("enter")
        assert aset.edit_field == "feed-url" and not aset.torznab_feeds, "invalid URL mutated feeds"
        aset.on_key("esc")
        aset.on_key("a")
        aset.edit_buf = "https://new.invalid/api"
        aset.on_key("enter")
        assert len(aset.torznab_feeds) == 1, "feed add"
        old_endpoint_secret, old_key_secret = "OLD-ENDPOINT-SECRET", "OLD-SEPARATE-KEY"
        aset.on_key("e"); aset.edit_buf = f"https://new.invalid/api?apikey={old_endpoint_secret}"; aset.on_key("enter")
        aset.on_key("k"); aset.edit_buf = old_key_secret; aset.on_key("enter")
        aset.on_key("k"); aset.edit_buf = "separate"; aset.on_key("enter")
        assert aset.torznab_feeds[0]["api_key"] == "separate", "feed key edit"
        aset.on_key("e"); aset.edit_buf = "https://edited.invalid/api"; aset.on_key("enter")
        assert aset.torznab_feeds[0]["url"] == "https://edited.invalid/api", "feed URL edit"
        edited_feed = aset.torznab_feeds[0]
        assert {old_endpoint_secret, old_key_secret} <= set(aset.source_secrets_for(edited_feed["id"])), \
            "feed edit discarded historical secrets"
        aset.on_key(" ")
        assert edited_feed["id"] in aset.disabled_sources, "Space did not toggle configured feed"
        aset.on_key(" ")
        assert edited_feed["id"] not in aset.disabled_sources, "Space did not restore configured feed"
        aset.set_sel = 3
        sid = SOURCES[0].id
        aset.on_key("enter")
        assert sid in aset.disabled_sources, "Enter did not toggle built-in source"
        aset.on_key(" ")
        assert sid not in aset.disabled_sources, "Space did not toggle built-in source"
        aset.set_sel = next(i for i, item in enumerate(aset.setting_items()) if item[0] == "feed")
        aset.on_key("x"); aset.on_key("esc")
        assert aset.torznab_feeds and aset.remove_feed is None, "escape did not cancel removal"
        aset.on_key("x"); aset.on_key("x")
        assert not aset.torznab_feeds and aset.set_sel < len(aset.setting_items()), "remove/clamp failed"

        # Submit sends remote text only while retaining the raw history entry.
        captures = []
        real_search = gv["Search"]
        class _SubmitSearch:
            def __init__(self, query, sources):
                captures.append((query, sources)); self.total = len(sources)
        gv["Search"] = _SubmitSearch
        gv["load_config"] = lambda: {"torznab_feeds": [feed]}
        aq = App(); aq.query = '"exact phrase" -cam seeders:>2 mystery:value'; aq.submit()
        assert captures[-1][0] == "exact phrase mystery:value" and aq.history[-1] == aq.query, \
            "remote query/history split"
        aq.query = "-cam seeders:>2"; aq.submit()
        assert all(s.browse for s in captures[-1][1]) and feed["id"] not in {s.id for s in captures[-1][1]}, \
            "operator-only query included search-only feed"
        gv["Search"] = real_search

        # Filter before dedupe with the active search's source map; preserve selection across sorting.
        active_source = type("ActiveSource", (), {"id": "removed", "label": "Old", "group": "Movies"})()
        class _ActiveSearch:
            def __init__(self):
                self.updates, self.sources = queue.Queue(), {"removed": active_source}
        af = App(); af.search = _ActiveSearch()  # type: ignore[assignment]
        af.local_query = parse_query("good group:movies")
        selected = Result("1" * 40, "good selected", 1, 1, 0, "removed", "magnet:?xt=urn:btih:" + "1" * 40)
        af.results = [selected]; af.sel = 0
        shared_hash = "2" * 40
        bad = Result(shared_hash, "bad", 1, 99, 0, "removed", "magnet:?xt=urn:btih:" + shared_hash)
        good = Result(shared_hash, "good new", 1, 50, 0, "removed", "magnet:?xt=urn:btih:" + shared_hash)
        af.search.updates.put(type("U", (), {"source": "removed", "results": [bad, good], "error": ""})())
        af.drain_search()
        assert [r.name for r in af.results] == ["good new", "good selected"], "active-map filter before dedupe"
        assert af._cur().name == "good selected", "selection identity was not restored after sorting"

        # Retry accounting keeps errors visible while in flight, then clears only a success.
        class _RetrySearch:
            def __init__(self):
                self.updates, self.sources, self.in_flight = queue.Queue(), {}, set()
                self.total = 2
            def retry(self, ids):
                scheduled = tuple(i for i in ids if i not in self.in_flight)
                self.in_flight.update(scheduled); self.total += len(scheduled)
                return scheduled
        ar = App(); ar.search = _RetrySearch(); ar.search_total = 2  # type: ignore[assignment]
        ar.search.updates.put(type("U", (), {"source": "bad", "results": None, "error": "down"})())
        ar.search.updates.put(type("U", (), {"source": "good", "results": [], "error": ""})())
        ar.drain_search()
        assert ar.search_done == ar.search_total == 2 and ar.errors == {"bad": "down"}, \
            "initial completion accounting"
        ar.retry_failed_sources()
        assert ar.search_done == 2 and ar.search_total == 3 and ar.errors == {"bad": "down"}, \
            "in-flight retry accounting"
        ar.retry_failed_sources()
        assert ar.status == "failed sources already retrying" and ar.search_total == 3, \
            "repeat retry scheduled in-flight source"
        ar.search.in_flight.clear()
        ar.search.updates.put(type("U", (), {"source": "bad", "results": None, "error": "down again"})())
        ar.drain_search()
        assert ar.search_done == ar.search_total == 3 and ar.errors == {"bad": "down again"}, \
            "failed retry completion accounting"
        ar.retry_failed_sources()
        assert ar.search_done == 3 and ar.search_total == 4 and ar.errors == {"bad": "down again"}, \
            "second in-flight retry accounting"
        ar.retry_failed_sources()
        assert ar.status == "failed sources already retrying" and ar.search_total == 4, \
            "second repeat retry scheduled in-flight source"
        ar.search.in_flight.clear()
        ar.search.updates.put(type("U", (), {"source": "bad", "results": [], "error": ""})())
        ar.drain_search()
        assert ar.search_done == ar.search_total == 4 and not ar.errors, "successful retry completion accounting"

        # Variant state and actions are isolated to details; canonical list remains unchanged.
        variants = (ResultVariant("one", "magnet:?xt=one", "https://one.invalid", 1, 0),
                    ResultVariant("two", "magnet:?xt=two", "https://two.invalid", 2, 0))
        vr = Result("4" * 40, "variant", 1, 2, 0, "one", "magnet:?xt=canonical", variants=variants)
        va = App(); va.search = Search.__new__(Search); va.results = [vr]; va.editing = False
        used = []
        gv["open_url"] = lambda u: used.append(("open", u)) or True
        gv["copy_clipboard"] = lambda u: used.append(("copy", u)) or True
        va.variant_idx = 1
        va.on_key("enter")
        assert va.variant_idx == 0, "opening details did not reset variant selection"
        va.on_key("right"); va.on_key("y"); va.on_key("o")
        assert used == [("copy", "magnet:?xt=two"), ("open", "https://two.invalid")], "variant y/o"
        va.grab = lambda u, n: used.append(("grab", u))  # type: ignore[method-assign]
        va.on_key("d")
        assert used[-1] == ("grab", "magnet:?xt=two") and va.results[0].magnet == "magnet:?xt=canonical", \
            "variant download changed canonical result"
        va.detail = Result("6" * 40, "single", 1, 1, 0, "one", "magnet:?xt=single")
        assert "Variant" not in "\n".join(strip_ansi(x) for x in _detail_panel(va, va.detail, 80, 15)), \
            "single result displayed a variant counter"

        # Historical endpoint and separate-key secrets still redact old in-flight results.
        aset.settings = False
        old_page = (f"https://new.invalid/item?apikey={old_endpoint_secret}"
                    f"&token={old_key_secret}")
        aset.detail = Result("7" * 40, "old result", 1, 1, 0, edited_feed["id"], "m",
                             variants=(ResultVariant(edited_feed["id"], "m", old_page, 1, 0),))
        aset.status = f"old request failed: {old_endpoint_secret} {old_key_secret}"
        historical_frame = "\n".join(render(aset, 100, 30))
        assert old_endpoint_secret not in historical_frame and old_key_secret not in historical_frame, \
            "historical feed secret appeared in rendered output"

        # Every sensitive UI mode and an aria2 URI error must hide configured secrets.
        gv["load_config"] = lambda: {"torznab_feeds": [feed], "tmdb_key": sentinel}
        secret_app = App()
        frames = []
        secret_app.settings = True
        feed_row = next(i for i, item in enumerate(secret_app.setting_items()) if item[0] == "feed")
        secret_app.set_sel, secret_app.edit_field, secret_app.edit_buf = feed_row, "feed-url", feed["url"]
        frames += render(secret_app, 100, 30)
        secret_app.edit_field, secret_app.edit_buf = "feed-key", sentinel
        frames += render(secret_app, 100, 30)
        secret_app.set_sel, secret_app.edit_field, secret_app.edit_buf = 2, "key", sentinel
        frames += render(secret_app, 100, 30)
        page = f"https://indexer.invalid/item?api_key={sentinel}&safe={sentinel}"
        secret_app.settings = False
        secret_app.detail = Result("5" * 40, "safe", 1, 1, 0, feed["id"], "m", page=page)
        frames += render(secret_app, 100, 30)
        class _ErrorEng:
            def add(self, uri, options=None): raise Aria2Error(f"failed {page}")
        secret_app.eng = _ErrorEng(); secret_app.grab(page, "safe")
        frames += render(secret_app, 100, 30)
        assert sentinel not in secret_app.status and sentinel not in "\n".join(frames), \
            "configured secret appeared in a frame or status"

        # Final-frame masking ignores ANSI spans and preserves display width.
        encoded_secret = "Long Secret+/Sentinel"
        mask_app = App()
        mask_app.source_secrets["mask-test"] = ("m", "0", "38", "a", encoded_secret)
        forms = (encoded_secret, urllib.parse.quote(encoded_secret, safe=""),
                 urllib.parse.quote_plus(encoded_secret))
        plain = "keep | " + " | ".join(forms)
        styled = style(plain, T.ACCENT)
        masked = _redact_frame(["keep", styled], mask_app)
        assert masked[0] == "keep", "unmatched output changed during final-frame masking"
        assert _ANSI.findall(masked[1]) == _ANSI.findall(styled), "ANSI sequence changed during masking"
        assert dwidth(strip_ansi(masked[1])) == dwidth(strip_ansi(styled)), \
            "final-frame masking changed display width"
        assert all(form.lower() not in strip_ansi(masked[1]).lower() for form in forms), \
            "encoded secret form appeared after final-frame masking"
        mask_frame = render(mask_app, 60, 20)
        assert all(dwidth(strip_ansi(line)) <= 60 for line in mask_frame), \
            "masked render exceeded requested width"
    finally:
        for k, v in originals.items():
            gv[k] = v
        if "real_search" in locals():
            gv["Search"] = real_search
    print("interaction ok")

    # render: search nav, downloads, help — sized lines, no overflow, no crash
    app2 = App(eng=None)
    app2.editing = False
    app2.search = Search.__new__(Search)  # marker so status shows results path
    app2.search_done = app2.search_total
    app2.results = [Result(str(i) + "x" * 39, f"Result {i} 日本語", 10**9, 100 - i, i, "yts",
                          "magnet:?xt=m", int(time.time())) for i in range(40)]
    app2.downloads = [
        Download("g1", "Active.Movie.mkv", "active", 100, 42, 2_500_000, 12, 90.0, root="r1"),
        Download("g2", "Done.Movie.mkv", "complete", 100, 100, 0, 0, None, root="r2"),
        Download("g3", "meta", "metadata", 0, 0, 0, 0, None, root="r3"),
        Download("g4", "Paused.Movie.mkv", "paused", 100, 60, 0, 4, None, root="r4"),
        Download("g5", "Bad.Movie.mkv", "error", 100, 5, 0, 0, None, error="no peers", root="r5"),
    ]
    app2.down_speed, app2.num_active = 5_000_000, 2  # exercise the header readout
    # animation runs only for an animated row in the exact visible download slice
    app2.view = "downloads"
    assert app2.animating(30)
    app2.help = True
    assert not app2.animating(30), "help covers downloads"
    app2.help = False
    app2.view = "search"
    assert not app2.animating(30), "hidden downloads"
    app2.view = "downloads"
    app2.downloads = [Download(str(i), str(i), "complete", 1, 1, 0, 0, None, root=str(i))
                      for i in range(12)]
    app2.downloads[-1].status = "active"
    app2.dsel = 0
    assert not app2.animating(24), "offscreen active download"
    app2.dsel = len(app2.downloads) - 1
    assert app2.animating(24), "selected active download is visible"
    app2.confirm_quit = True
    assert app2.animating(24), "partial prompt leaves the active bar visible"
    app2.confirm_quit = False
    app2.dsel = 0
    app2.downloads[-1].status = "complete"
    app2.downloads[1].status = "active"  # second bar is on the centered overlay row
    for prompt in ("confirm_quit", "torrent_prompt", "cancel_prompt"):
        setattr(app2, prompt, True)
        assert not app2.animating(24), f"only active bar hidden by {prompt}"
        setattr(app2, prompt, False if prompt == "confirm_quit" else None)
    app2.downloads[0].status = "active"  # first bar remains above the overlay
    app2.confirm_quit = True
    assert app2.animating(24), "active row outside overlay still animates"
    app2.confirm_quit = False
    for covering in ("help", "settings", "picker"):
        setattr(app2, covering, True)
        assert not app2.animating(24), f"{covering} fully covers downloads"
        setattr(app2, covering, False if covering != "picker" else None)
    assert not app2.animating(1), "minimum terminal has no visible bar"
    app2.downloads = [
        Download("g1", "Active.Movie.mkv", "active", 100, 42, 2_500_000, 12, 90.0, root="r1"),
        Download("g2", "Done.Movie.mkv", "complete", 100, 100, 0, 0, None, root="r2"),
        Download("g3", "meta", "metadata", 0, 0, 0, 0, None, root="r3"),
        Download("g4", "Paused.Movie.mkv", "paused", 100, 60, 0, 4, None, root="r4"),
        Download("g5", "Bad.Movie.mkv", "error", 100, 5, 0, 0, None, error="no peers", root="r5"),
    ]
    app2.dsel = 0
    # Shared viewport rows are the rows the panel actually renders, including
    # history reservation and the clamped minimum terminal size.
    app2.dl_history = []
    positions = _visible_download_rows(app2, 24)
    assert len(positions) == 4, positions
    frame24 = render(app2, 100, 24)
    for idx, bar_row in positions:
        assert clean(app2.downloads[idx].name) in strip_ansi(frame24[bar_row - 1])
        assert "█" in strip_ansi(frame24[bar_row]) or "░" in strip_ansi(frame24[bar_row])
    app2.dl_history = [{"name": "OldSession.iso", "size": 1, "ts": 1}]
    reserved = _visible_download_rows(app2, 24)
    assert len(reserved) == 3 and len(reserved) < len(positions), reserved
    assert _visible_download_rows(app2, 1) == [] and _download_viewport(app2, 1) == (0, 0)
    app2.dl_history = []
    for cols, rows in [(100, 30), (80, 24), (140, 50)]:
        for view, help_ in [("search", False), ("downloads", False), ("search", True)]:
            app2.view, app2.help = view, help_
            frame = render(app2, cols, rows)
            assert len(frame) == rows, f"{len(frame)} != {rows}"
            for ln in frame:
                w = dwidth(strip_ansi(ln))
                assert w <= cols, f"line width {w} > {cols} ({view}): {strip_ansi(ln)!r}"
    # spot-check content present
    app2.help, app2.view, app2.query = False, "search", "matrix"
    f = "\n".join(strip_ansi(x) for x in render(app2, 100, 30))
    assert "results" in f and "quit" in f and "Result 0" in f, f
    app2.view = "downloads"
    f = "\n".join(strip_ansi(x) for x in render(app2, 100, 30))
    assert "Active.Movie.mkv" in f and "fetching metadata" in f, "downloads view"
    assert "█" in f or "░" in f, "no progress bar"
    # recently-downloaded section + settings overlay render, width-safe
    app2.dl_history = [{"name": "OldSession.iso", "size": 10**9, "ts": int(time.time()), "path": "/x"}]
    rf = render(app2, 100, 30)  # view is "downloads" here
    for ln in rf:
        assert dwidth(strip_ansi(ln)) <= 100, "downloads overflow"
    assert any("Recently downloaded" in strip_ansi(x) for x in rf), "recent section"
    app2.dl_history = []
    app2.settings = True
    gf = render(app2, 100, 30)
    for ln in gf:
        assert dwidth(strip_ansi(ln)) <= 100, "settings overflow"
    joined = "\n".join(strip_ansi(x) for x in gf)
    assert "Settings" in joined and "Sources" in joined and "FitGirl" in joined, "settings view"
    app2.settings = False
    # details view renders, width-safe
    app2.view, app2.detail = "search", app2.results[0]
    df = render(app2, 100, 30)
    for ln in df:
        assert dwidth(strip_ansi(ln)) <= 100, "details overflow"
    assert any("Details" in strip_ansi(x) for x in df) and any("Health" in strip_ansi(x) for x in df), "details view"
    app2.detail = None
    # details shows info, provider-labeled, with a poster hint; cache is per-provider
    app2.meta_provider, app2.tmdb_key = "tmdb", "test-key"
    app2.meta[f"tmdb:movie:{app2.results[0].name}"] = Meta(
        "The Matrix", "1999", 8.2, 25000, ["Action", "Sci-Fi"], ["Keanu Reeves"],
        "Neo learns the truth.", "http://img/p.jpg")
    app2.view, app2.detail = "search", app2.results[0]
    jm = "\n".join(strip_ansi(x) for x in render(app2, 100, 30))
    assert "TMDb" in jm and "8.2/10" in jm and "Action" in jm and "Keanu Reeves" in jm, jm
    assert "Neo learns" in jm and "press p to open" in jm, jm
    # OMDb provider labels the rating IMDb and reads its own cache slot
    app2.meta_provider, app2.omdb_key = "omdb", "omdb-key"
    app2.meta[f"omdb:movie:{app2.results[0].name}"] = Meta(
        "The Matrix", "1999", 8.7, 1999001, ["Action"], ["Keanu Reeves"], "A sim.", "")
    of = render(app2, 100, 30)
    jo = "\n".join(strip_ansi(x) for x in of)
    assert "IMDb" in jo and "8.7/10" in jo, jo
    for ln in of:
        assert dwidth(strip_ansi(ln)) <= 100, "meta detail overflow"
    app2.detail, app2.tmdb_key, app2.omdb_key, app2.meta_provider = None, None, None, "tmdb"
    # quit-confirm modal stamps over the center, width-safe
    app2.confirm_quit = True
    cf = render(app2, 100, 30)
    assert any("Quit trawl?" in strip_ansi(x) for x in cf), "confirm modal missing"
    for ln in cf:
        assert dwidth(strip_ansi(ln)) <= 100, "confirm overflow"
    app2.confirm_quit = False
    # .torrent modal stamps over the center, width-safe
    from .sources import ParsedMagnet
    app2.torrent_prompt = ParsedMagnet("", "book.torrent", "https://s.org/book.torrent", "torrent")
    tf = render(app2, 100, 30)
    assert any(".torrent link" in strip_ansi(x) for x in tf), "torrent modal missing"
    for ln in tf:
        assert dwidth(strip_ansi(ln)) <= 100, "torrent modal overflow"
    app2.torrent_prompt = None
    # cancel modal stamps over the center, width-safe
    app2.view = "downloads"
    app2.cancel_prompt = app2.downloads[0]
    xf = render(app2, 100, 30)
    assert any("Cancel download" in strip_ansi(x) for x in xf), "cancel modal missing"
    for ln in xf:
        assert dwidth(strip_ansi(ln)) <= 100, "cancel modal overflow"
    app2.cancel_prompt = None
    # the net motif glyphs render in the logo
    assert any(g in "".join(_logo_lines()) for g in T.NET_GLYPHS), "net glyphs missing"
    # splash: fresh app (no search yet) shows the centered welcome
    app3 = App(eng=None)
    for cols, rows in [(100, 30), (80, 24), (140, 50)]:
        frame = render(app3, cols, rows)
        assert len(frame) == rows
        for ln in frame:
            assert dwidth(strip_ansi(ln)) <= cols, f"splash overflow: {strip_ansi(ln)!r}"
    sf = "\n".join(strip_ansi(x) for x in render(app3, 100, 30))
    assert "terminal-native" in sf and "games" in sf and "Search" in sf, "splash content"
    app3.search = Search.__new__(Search)  # once searched, splash gives way to browse
    assert "terminal-native" not in "\n".join(strip_ansi(x) for x in render(app3, 100, 30))
    print("render ok")
    print("\nPhase 3 selftest passed.")


if __name__ == "__main__":
    selftest()

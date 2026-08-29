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
                      parse_magnet, parse_query, parse_source, redact, redact_url,
                      result_identity, torznab_label, validate_torznab_url)
from .meta import Meta, kind_for, lookup

CATS = [("all", "All"), ("games", "Games"), ("movies", "Movies"),
        ("tv", "TV"), ("anime", "Anime"), ("books", "Books")]
CAT_GROUP = {"games": "Games", "movies": "Movies", "tv": "TV", "anime": "Anime",
             "books": "Books"}
CAT_GLYPH = {"all": "✦", "games": "◆", "movies": "★", "tv": "▶", "anime": "❀", "books": "▤"}

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
        cfg = json.loads(CONFIG_FILE.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg))
    except OSError:
        pass

RAIL_W = 18  # glyph + label + count
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


def _safe_basename(name: str) -> str:
    s = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", clean(name)).strip().strip(".")
    return (s[:120] or "torrent")


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
_TILDE = {b"[5~": "pageup", b"[6~": "pagedown", b"[1~": "home", b"[4~": "end",
          b"[7~": "home", b"[8~": "end"}
_CTRL = {0x01: "ctrl-a", 0x05: "ctrl-e", 0x15: "ctrl-u", 0x17: "ctrl-w"}


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
            elif data[i:i + 6] == b"\x1b[200~":  # bracketed paste: wrap to close
                j = data.find(b"\x1b[201~", i + 6)
                if j < 0:
                    j = n
                keys.extend(c for c in data[i + 6:j].decode("utf-8", "ignore") if c >= " ")
                i = n if j == n else j + 6
            elif i + 2 < n and data[i + 1] in (ord("["), ord("O")) and bytes([data[i + 2]]) in _ARROWS:
                keys.append(_ARROWS[bytes([data[i + 2]])])
                i += 3
            elif data[i + 1:i + 4] in _TILDE:
                keys.append(_TILDE[data[i + 1:i + 4]])
                i += 4
            elif data[i + 1:i + 3] in (b"[H", b"OH", b"[F", b"OF"):
                keys.append("home" if data[i + 1:i + 3] in (b"[H", b"OH") else "end")
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
        elif b in _CTRL:
            keys.append(_CTRL[b])
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
        # alt-screen + clear + SGR mouse + bracketed paste; trawl owns the whole tab
        sys.stdout.write("\x1b[?1049h\x1b[3J\x1b[2J\x1b[H\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[?2004h")
        sys.stdout.flush()

    def _reset_frame(self) -> None:
        self._lines = self._size = None

    def leave(self) -> None:
        sys.stdout.write("\x1b]0;\x07\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.saved:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def set_title(self, text: str) -> None:
        sys.stdout.write(f"\x1b]0;trawl — {text}\x07")
        sys.stdout.flush()

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
        self.help_scroll = 0
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
        cfg = self.config = load_config()
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
        self.source_by_id: dict[str, Source] = {}
        self.source_secrets: dict[str, tuple[str, ...]] = {}
        self._rebuild_sources()
        self.disabled_sources: set[str] = set(cfg.get("disabled_sources", []))
        self.download_dir: str | None = cfg.get("download_dir")
        self.dl_history: list[dict] = load_dl_history()  # completed downloads, oldest->newest
        self.settings = False  # settings overlay open
        self.set_sel = 0  # settings selection (never a 'section' row)
        self.edit_field: str | None = None  # settings text-edit: "dir" | "key"
        self.edit_buf = ""
        self.remove_feed: str | None = None
        self.show_errors = False  # per-source failure viewer over the results
        self.hide_dead = bool(cfg.get("hide_dead", False))
        self.folder_prompt: tuple[str, str] | None = None  # (uri, name) for D download
        self.folder_buf = ""
        self.last_dir: str | None = None  # last Shift+D destination, reused as prompt default
        self._exports: dict[str, tuple[str, str, str, float]] = {}  # gid -> (ih, name, dir, started)
        self.local_query = LocalQuery("")
        self.start = time.monotonic()
        self.meta_provider = cfg.get("meta_provider", "tmdb")  # tmdb | omdb
        self.theme = T.set_theme(cfg.get("theme", "violet"))
        self.tmdb_key = cfg.get("tmdb_key") or os.environ.get("TMDB_API_KEY")
        self.omdb_key = cfg.get("omdb_key") or os.environ.get("OMDB_API_KEY")
        self.meta: dict[str, object] = {}  # "provider:kind:name" -> "loading" | Meta | None

    # -- derived
    @property
    def results(self) -> tuple[Result, ...]:
        return self._results

    @results.setter
    def results(self, value: Iterable[Result]) -> None:
        self._results = tuple(value)

    def visible_results(self) -> tuple[Result, ...]:
        base = tuple(r for r in self.results
                     if not (self.hide_dead and r.seeders == 0
                             and self.source_reports_health(r.source)))
        if self.cat != "all":
            g = CAT_GROUP[self.cat]
            return tuple(r for r in base if self.result_group(r) == g)
        # All: round-robin across categories so one prolific group (e.g. anime)
        # can't monopolize the top. Buckets keep first-seen order (= current sort),
        # so the group holding the overall-top result still leads.
        buckets: dict[str, list[Result]] = {}
        for r in base:
            buckets.setdefault(self.result_group(r) or "Other", []).append(r)
        cols = list(buckets.values())
        ordered: list[Result] = []
        for i in range(max((len(c) for c in cols), default=0)):
            ordered += [c[i] for c in cols if i < len(c)]
        return tuple(ordered)

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
        self.source_by_id = {s.id: s for s in self.sources}
        for source in self.sources:
            self.source_secrets[source.id] = tuple(dict.fromkeys(
                (*self.source_secrets.get(source.id, ()), *source.secrets)))

    def source_label(self, source_id: str) -> str:
        src = self.source_by_id.get(source_id)
        return src.label if src else source_id

    def source_reports_health(self, source_id: str) -> bool:
        src = self.source_by_id.get(source_id)
        return src.reports_health if src else True

    def source_secrets_for(self, source_id: str) -> tuple[str, ...]:
        return self.source_secrets.get(source_id, ())

    def _all_secrets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(secret for secrets in self.source_secrets.values()
                                   for secret in secrets if secret))

    def result_group(self, result: Result) -> str | None:
        src = self.source_by_id.get(result.source)
        return result.group or (src.group if src else None)

    def source_tag(self, source_id: str) -> tuple[str, str]:
        if source_id in {s.id for s in SOURCES}:
            return T.source_style(source_id)
        return dtrunc(self.source_label(source_id), 5), T.ALT

    @property
    def page_title(self) -> str:
        """Short label for the terminal title bar (callers rate-limit it)."""
        if self.view == "downloads":
            return "downloads"
        if self.show_errors:
            return f"errors · {len(self.errors)}"
        if self.detail is not None:
            return "details"
        if self.query.strip():
            return clean(self.query)[:40]
        return "latest" if self.search is not None else "search"

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
        return any(self.downloads[idx].status in ("active", "metadata")
                   for idx, _ in _visible_download_rows(self, rows))

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
        self.show_errors = False
        self.status = ""

    def grab(self, magnet: str, name: str, dir_: str | None = None) -> None:
        secrets = self._all_secrets()
        if not self.eng:
            self.status = redact(f"(no engine) {clean(name)[:48]}", secrets)
            return
        try:
            self.eng.add(magnet, {"dir": dir_} if dir_ else None)
            self.status = redact(f"grabbing: {clean(name)[:48]}", secrets)
        except Aria2Error as e:
            self.status = redact(f"error: {e}", secrets)

    def _start_folder_prompt(self, uri: str, name: str) -> None:
        self.folder_prompt = (uri, name)
        self.folder_buf = self.last_dir or self.download_dir or ""

    def _commit_folder_prompt(self) -> None:
        uri, name = self.folder_prompt or ("", "")
        self.folder_prompt = None
        path = os.path.expanduser(self.folder_buf.strip())
        if not path:
            self.status = "folder download cancelled (no path)"
            return
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            self.status = f"couldn't create folder: {e.strerror or e}"
            return
        self.last_dir = path
        self.grab(uri, name, dir_=path)
        if self.view == "search":
            self.view = "downloads"

    def export_torrent(self, uri: str, name: str) -> None:
        """Fetch metadata only and save <name>.torrent into the download dir."""
        secrets = self._all_secrets()
        if not uri.startswith("magnet:"):
            self.status = redact(f"not a magnet: {clean(name)[:36]}", secrets)
            return
        if not self.eng:
            self.status = redact(f"(no engine) {clean(name)[:48]}", secrets)
            return
        try:
            pm = parse_magnet(uri)
            ih = pm.info_hash if pm else ""
            if not ih:
                m = re.search(r"btih:([0-9a-fA-F]{40}|[0-9A-Za-z]{32})", uri)
                ih = m.group(1) if m else "?"
            dir_path = self.eng.download_dir() or os.path.expanduser("~")
            gid = self.eng.save_metadata(uri, dir_path)
            if not gid:
                self.status = "couldn't start .torrent fetch"
                return
            self._exports[gid] = (ih, name, dir_path, time.monotonic())
            self.status = redact(f"fetching .torrent for {clean(name)[:36]}…", secrets)
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
                            "theme": self.theme, "hide_dead": self.hide_dead,
                            "tmdb_key": self.tmdb_key, "omdb_key": self.omdb_key,
                            "torznab_feeds": [dict(feed) for feed in self.torznab_feeds]})
        save_config(self.config)

    def _set_theme(self, name: str) -> None:
        self.theme = T.set_theme(name)
        _logo_lines.cache_clear()  # the gradient logo is baked at first render

    def setting_items(self) -> list[tuple[str, object]]:
        return ([('section', 'General'), ('dir', None), ('provider', None), ('meta-key', None),
                 ('theme', None),
                 ('section', 'Sources')]
                + [('source', s) for s in SOURCES]
                + [('section', 'Torznab feeds')]
                + [('feed', feed) for feed in self.torznab_feeds]
                + [('add-feed', None)])

    def _start_feed_edit(self, feed: dict[str, str] | None = None) -> None:
        self.edit_field = "feed-url"
        self.edit_buf = feed["url"] if feed else ""
        self.remove_feed = None

    def _snap_setting(self, sel: int) -> int:
        """Clamp sel to a selectable row, stepping past 'section' headers."""
        items = self.setting_items()
        n = len(items)
        sel = min(max(0, sel), n - 1)
        if items[sel][0] != "section":
            return sel
        for cand in range(sel + 1, n):
            if items[cand][0] != "section":
                return cand
        for cand in range(sel - 1, -1, -1):
            if items[cand][0] != "section":
                return cand
        return 0

    def _move_setting(self, delta: int) -> None:
        items = self.setting_items()
        n = len(items)
        sel = self.set_sel
        for _ in range(n):
            sel = (sel + delta) % n
            if items[sel][0] != "section":
                self.set_sel = sel
                return

    def _selected_setting(self) -> tuple[str, object]:
        items = self.setting_items()
        self.set_sel = self._snap_setting(self.set_sel)
        return items[self.set_sel]

    def _settings_key(self, k: str) -> None:
        if self.edit_field:
            if k == "enter":
                self._commit_edit()
            elif k == "esc":
                self.edit_field = None
                self.status = ""
            elif k == "backspace":
                self.edit_buf = self.edit_buf[:-1]
            elif k == "ctrl-u":
                self.edit_buf = ""
            elif len(k) == 1 and k >= " ":
                self.edit_buf += k
            return
        if k in ("g", "esc", "q"):
            if self.remove_feed:
                self.remove_feed = None
                self.status = ""
            else:
                self.settings = False
        elif k in ("up", "k", "pageup"):
            for _ in range(8 if k == "pageup" else 1):
                self._move_setting(-1)
        elif k in ("down", "j", "pagedown"):
            for _ in range(8 if k == "pagedown" else 1):
                self._move_setting(1)
        elif k in ("home", "end"):
            items = self.setting_items()
            self.set_sel = 0 if k == "home" else len(items) - 1
            self._snap_setting(self.set_sel)
        else:
            kind, value = self._selected_setting()
            if k == "a" or (k == "enter" and kind == "add-feed"):
                self._start_feed_edit()
            elif k in ("enter", "e") and kind == "feed":
                if self.remove_feed == value["id"]:
                    self._remove_selected_feed(value)
                else:
                    self._start_feed_edit(value)
            elif k == "K" and kind == "feed":
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
            elif k in ("enter", " ") and kind == "theme":
                self._set_theme("light" if self.theme == "violet" else "violet")
                self._save_settings()
            elif k in ("enter", " ") and kind in ("source", "feed"):
                sid = value.id if kind == "source" else value["id"]
                self.disabled_sources.symmetric_difference_update({sid})
                self._save_settings()

    def _remove_selected_feed(self, feed: dict[str, str]) -> None:
        removed = next((s for s in self.sources if s.id == feed["id"]), None)
        self.torznab_feeds.remove(feed)
        self.disabled_sources.discard(feed["id"])
        self.remove_feed = None
        self._rebuild_sources()
        if removed:  # retire, don't drop: in-flight results keep their labels/groups
            self.source_by_id[removed.id] = removed
        self._save_settings()
        self.set_sel = self._snap_setting(self.set_sel)
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
        elif k in ("up", "k", "pageup"):
            self.picker_sel = (self.picker_sel - (8 if k == "pageup" else 1)) % n
        elif k in ("down", "j", "pagedown"):
            self.picker_sel = (self.picker_sel + (8 if k == "pagedown" else 1)) % n
        elif k in ("home", "end"):
            self.picker_sel = 0 if k == "home" else n - 1
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

    def drain_search(self) -> bool:
        """Drain finished source updates into the result list. True if the
        search state changed (caller may then skip a redundant render)."""
        if not self.search:
            return False
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
        return changed

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
        polled, so launching with finished items stays quiet). .torrent exports
        are resolved here too: once the metadata fetch completes, the <hash>.torrent
        aria2 wrote is renamed to the item's name and the fetch row is dropped."""
        prev = {d.root: d.status for d in self.downloads}
        for d in downloads:
            if d.root in self._exports:
                continue  # a metadata fetch, not a real download
            was = prev.get(d.root)
            if d.status == "complete" and was is not None and was != "complete":
                notify("trawl — download complete", d.name)
                rec = {"name": d.name, "size": d.total, "ts": int(time.time()), "path": d.path}
                self.dl_history.append(rec)
                append_dl_history(rec)
        exports = dict(self._exports)
        kept = []
        now = time.monotonic()
        for d in downloads:
            if d.root in exports:
                ih, name, dir_path, started = exports[d.root]
                st = self.eng.status(d.root) if self.eng else ""
                if st == "complete":
                    self._finish_export(d.root, ih, name, dir_path)
                elif st == "error" or now - started > 60:
                    self._exports.pop(d.root, None)
                    try:
                        self.eng.remove(d.root)
                    except Aria2Error:
                        pass
                    self.status = ("couldn't fetch .torrent metadata"
                                   if st == "error" else
                                   "couldn't fetch .torrent metadata (timed out)")
                continue
            kept.append(d)
        self.downloads = kept

    def _finish_export(self, gid: str, ih: str, name: str, dir_path: str) -> None:
        self._exports.pop(gid, None)
        src = os.path.join(dir_path, f"{ih}.torrent")
        target = os.path.join(dir_path, _safe_basename(name) + ".torrent")
        saved = src
        try:
            deadline = time.monotonic() + 3  # file write can lag the status flip
            while not os.path.exists(src) and time.monotonic() < deadline:
                time.sleep(0.1)
            if src != target and os.path.exists(src):
                os.replace(src, target)
                saved = target
        except OSError:
            saved = src
        try:
            self.eng.remove(gid)
        except Aria2Error:
            pass
        self.status = redact(f"saved {os.path.basename(saved)}", self._all_secrets())

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
            if k in ("up", "down", "j", "k", "pageup", "pagedown"):
                if k in ("down", "j"):
                    self.help_scroll += 1
                elif k in ("up", "k"):
                    self.help_scroll -= 1
                elif k == "pagedown":
                    self.help_scroll += 8
                else:
                    self.help_scroll -= 8
                self.help_scroll = max(0, self.help_scroll)
            else:
                self.help = False
                self.help_scroll = 0
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
        if self.folder_prompt is not None:
            if k == "enter":
                self._commit_folder_prompt()
            elif k == "esc":
                self.folder_prompt = None
            elif k == "backspace":
                self.folder_buf = self.folder_buf[:-1]
            elif k == "ctrl-u":
                self.folder_buf = ""
            elif len(k) == 1 and k >= " ":
                self.folder_buf += k
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
            elif k == "D":
                self._start_folder_prompt(self._variant().uri, self.detail.name)
            elif k == "e":
                self.export_torrent(self._variant().uri, self.detail.name)
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
            elif k in ("left", "home", "ctrl-a"):
                self.cursor = 0 if k in ("home", "ctrl-a") else max(0, self.cursor - 1)
            elif k in ("right", "end", "ctrl-e"):
                self.cursor = len(self.query) if k in ("end", "ctrl-e") else min(len(self.query), self.cursor + 1)
            elif k == "ctrl-u":  # kill to start
                self.query = self.query[self.cursor:]
                self.cursor = 0
            elif k == "ctrl-w":  # kill word behind the caret
                head = self.query[:self.cursor].rstrip(" ")
                cut = head.rfind(" ")
                keep = cut + 1 if cut >= 0 else 0
                self.query = self.query[:keep] + self.query[self.cursor:]
                self.cursor = keep
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
            self.set_sel = self._snap_setting(0)
        elif k == "tab":
            self.view = "downloads" if self.view == "search" else "search"
            self.detail = None
            self.variant_idx = 0
            self.show_errors = False
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
        elif k == "pageup":
            self._move(-8)
        elif k == "pagedown":
            self._move(8)
        elif k in ("home", "end"):
            last = (len(self.downloads) - 1 if self.view == "downloads"
                    else len(self.visible_results()) - 1)
            if self.view == "downloads":
                self.dsel = 0 if k == "home" else max(0, last)
            else:
                self.sel = 0 if k == "home" else max(0, last)
        elif self.view == "search":
            if k == "left":
                self._cycle_cat(-1)
            elif k == "right":
                self._cycle_cat(1)
            elif k in ("/", "i"):
                self.editing = True
            elif k == "E":
                if self.errors:
                    self.show_errors = not self.show_errors
                else:
                    self.status = "all sources answered — nothing to show"
            elif k == "esc":
                if self.show_errors:
                    self.show_errors = False
            elif k == "c":
                self.clear()
            elif k == "S":
                self._cycle_sort()
            elif k == "z":
                self.hide_dead = not self.hide_dead
                self._save_settings()
                self.status = ("hiding dead torrents" if self.hide_dead
                               else "showing all torrents")
            elif k == "r":
                self.retry_failed_sources()
            elif k == "d":
                if (r := self._cur()):
                    self.grab(r.magnet, r.name)
            elif k == "D":
                if (r := self._cur()):
                    self._start_folder_prompt(r.magnet, r.name)
            elif k == "e":
                if (r := self._cur()):
                    self.export_torrent(r.magnet, r.name)
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
    if len(variants) > 1:
        inner.append(cell("", inner_w))
        inner.append(cell(f"Variants  ({len(variants)})  ← →", inner_w, color=T.ALT, bold=True))
        vlw = max(0, inner_w - 2 - 1 - 9 - 5)
        for i, v in enumerate(variants):
            here = i == app.variant_idx % len(variants)
            vtag, _ = app.source_tag(v.source)
            inner.append(cell(T.PTR if here else " ", 2, color=T.ACCENT) + " "
                         + cell(app.source_label(v.source), vlw,
                                color=T.ACCENT if here else T.TEXT, bold=here)
                         + cell(seed_leech(v, app.source_reports_health(v.source)),
                                9, "right", color=seed_color(v.seeders), bold=here)
                         + cell(vtag, 5, "right", color=T.ACCENT if here else T.ALT, bold=here))
        inner.append(cell("", inner_w))
    inner.append(field("Source", app.source_label(variant.source)))
    inner.append(field("Size", fmt_bytes(r.size)))
    if variant.seeders or variant.leechers:
        ratio = min(1.0, (variant.seeders or 0) / 1000)
        filled = round(ratio * 10)
        bar = style(T.BLOCK * filled, seed_color(variant.seeders) or T.RULE) \
            + style(T.TRACK * (10 - filled), T.RULE)
        text = (f"{variant.seeders} s · {variant.leechers} l" if (variant.seeders or variant.leechers)
                else "unknown")
        inner.append("  " + cell("Health", 7, dim=True) + bar + " "
                     + cell(text, inner_w - 20, dim=True))
    else:
        inner.append(field("Health", "unknown"))
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


def _cat_counts(app: App) -> dict[str, int]:
    counts = {key: 0 for key, _ in CATS}
    counts["all"] = len(app.results)
    for r in app.results:
        g = app.result_group(r)
        for key, group in CAT_GROUP.items():
            if g == group:
                counts[key] += 1
                break
    return counts


def _rail(app: App, h: int) -> list[str]:
    counts = _cat_counts(app)
    lines = [cell("", RAIL_W)]
    for key, label in CATS:
        sel = app.view == "search" and app.cat == key
        mark = style(T.BAR, T.ACCENT, bold=True) if sel else " "
        n = counts.get(key, 0)
        lines.append(mark + " " + style(CAT_GLYPH[key], T.ALT) + " "
                     + cell(label, 9, color=T.ACCENT if sel else T.TEXT, bold=sel)
                     + cell(str(n) if n else "", 5, "right", color=T.ACCENT if sel else None,
                            bold=sel, dim=not sel))
    lines.append(cell("", RAIL_W))
    dsel = app.view == "downloads"
    n = len(app.downloads)
    mark = style(T.BAR, T.ACCENT, bold=True) if dsel else " "
    lines.append(mark + " " + style(T.DOWN, T.ALT) + " "
                 + cell("Downloads", 9, color=T.ACCENT if dsel else T.TEXT, bold=dsel)
                 + cell(f"({n})" if n else "", 5, "right", color=T.ACCENT if dsel else None,
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


def _errors_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    inner: list[str] = []
    if not app.errors:
        inner.append(cell("No failed sources — every source answered.", inner_w, dim=True))
    for sid, msg in app.errors.items():
        inner.append(cell(f" {app.source_label(sid)} —", inner_w, color=T.BAD, bold=True))
        for ln in _wrap(clean(msg) or "unknown error", inner_w - 2):
            inner.append("  " + cell(ln, inner_w - 2, dim=True))
        inner.append(cell("", inner_w))
    count = f"({len(app.errors)})" if app.errors else None
    return _wrap_panel("Failed sources", inner, width, height, True, count)


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


def seed_color(n: int) -> str | None:
    """Health tier for a seeder count: hot (GOOD), warm (ALT), cold (WARN), none."""
    if n <= 0:
        return None
    if n < 100:
        return T.WARN
    if n < 1000:
        return T.ALT
    return T.GOOD


def seed_leech(r: Result, reports_health: bool = True) -> str:
    s, l = r.seeders, r.leechers
    if not reports_health and not s and not l:
        return "—"  # source reports no swarm counts; unknown, not dead
    if s and l:
        return f"{s}:{l}"
    if s:
        return str(s)
    if l:
        return f"0:{l}"
    return "-"


def _results_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    results = app.visible_results()
    app.sel = min(app.sel, max(0, len(results) - 1))
    name_w = max(8, inner_w - 28)  # ptr2 + name + 9 + 9 + 5 + 3 seps
    inner: list[str] = [_status_line(app, results, inner_w)]
    if results:
        header = (cell("", 2) + " " + cell("Name", name_w, dim=True, bold=True) + " "
                  + cell("Size", 9, "right", dim=True, bold=True) + " "
                  + cell("Seed", 9, "right", dim=True, bold=True) + " "
                  + cell("Src", 5, "right", dim=True, bold=True))
        inner.append(header)
        list_h = max(1, height - 2 - len(inner))
        start = _window(app.sel, len(results), list_h)
        for idx in range(start, min(start + list_h, len(results))):
            r = results[idx]
            here = idx == app.sel
            tag, tcolor = app.source_tag(r.source)
            sl = seed_leech(r, app.source_reports_health(r.source))
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
                    + cell(clean(r.name), name_w, color=T.TEXT) + " "
                    + cell(fmt_bytes(r.size), 9, "right", dim=True) + " "
                    + cell(sl, 9, "right", color=seed_color(r.seeders)) + " "
                    + cell(tag, 5, "right", color=tcolor))
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
                                               color=T.ACCENT if here else T.TEXT, bold=here)
                + cell(stats, stat_w, "right", dim=True))
            if d.status != "error":
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
                         + cell(clean(rec.get("name", "?")), inner_w - 2 - rw, color=T.TEXT)
                         + cell(right, rw, "right", dim=True))
    active = sum(1 for d in live if d.status in ("active", "waiting", "metadata"))
    title_count = f"({len(live)})" if live else None
    if active and title_count:
        title_count += f" · {active} active"
    return _wrap_panel("Downloads", inner, width, height, app.view == "downloads",
                       title_count)


def _settings_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    items = app.setting_items()
    body_h = max(1, height - 2)
    app.set_sel = app._snap_setting(app.set_sel)
    start = _window(app.set_sel, len(items), body_h)
    inner = []
    for idx in range(start, min(start + body_h, len(items))):
        kind, value = items[idx]
        selected = idx == app.set_sel
        sel_c = T.ACCENT if selected else None
        if kind == "section":
            inner.append(cell(str(value).upper(), inner_w, color=T.ALT, bold=True, dim=True))
            continue
        prefix = cell(T.PTR if selected else "", 2, color=T.ACCENT)
        if kind in ("dir", "provider", "meta-key", "theme"):
            if kind == "dir":
                label = "Download dir"
                shown = app.edit_buf + "▌" if app.edit_field == "dir" else app.download_dir or "(from aria2.conf)"
                on = bool(app.download_dir or app.edit_field == "dir")
            elif kind == "provider":
                label = "Metadata provider"
                shown = app.meta_provider.upper()
                on = True
            elif kind == "theme":
                label = "Theme"
                shown = app.theme.title()
                on = True
            else:
                label = f"{app.meta_provider.upper()} key"
                key = app.edit_buf if app.edit_field == "key" else app._provider_key() or ""
                shown = ("•" * min(len(key), 12) + ("▌" if app.edit_field == "key" else "")) or "(not set)"
                on = bool(key)
            inner.append(prefix + cell(label, 18, color=sel_c, bold=selected, dim=not selected and not on)
                         + cell(shown, inner_w - 20, color=sel_c, bold=selected,
                                dim=not selected and not on))
        elif kind == "source":
            on = value.id not in app.disabled_sources
            tag, tcolor = app.source_tag(value.id)
            inner.append(prefix
                         + cell("[✓]" if on else "[ ]", 4,
                                color=T.GOOD if (on and not selected) else (sel_c or T.RULE), bold=selected)
                         + cell(value.label, inner_w - 21, color=sel_c, bold=selected,
                                dim=not selected and not on)
                         + cell(value.group or "", 10, "right", color=sel_c, bold=selected,
                                dim=not selected and not on)
                         + cell(tag, 5, "right", color=sel_c if selected else tcolor, bold=selected,
                                dim=not selected and not on))
        elif kind == "feed":
            on = value["id"] not in app.disabled_sources
            if selected and app.edit_field == "feed-url":
                shown = redact_url(app.edit_buf, app._all_secrets()) + "▌"
            elif selected and app.edit_field == "feed-key":
                shown = "key: " + "•" * min(len(app.edit_buf), 12) + "▌"
            else:
                shown = torznab_label(value["url"])
            armed = "  [remove?]" if app.remove_feed == value["id"] else ""
            keymark = " · key" if value.get("api_key") else ""
            inner.append(prefix
                         + cell("[✓]" if on else "[ ]", 4,
                                color=T.GOOD if (on and not selected) else (sel_c or T.RULE), bold=selected)
                         + cell(f"{shown}{keymark}{armed}", inner_w - 6, color=sel_c, bold=selected,
                                dim=not selected and not on))
        else:  # add-feed
            shown = (redact_url(app.edit_buf, app._all_secrets()) + "▌"
                     if selected and app.edit_field == "feed-url" else "")
            text = shown or "+ Add Torznab feed"
            inner.append(prefix + cell(text, inner_w - 2, color=sel_c or T.ALT, bold=selected,
                                       dim=not selected))
    on_sources = sum(1 for s in SOURCES if s.id not in app.disabled_sources)
    return _wrap_panel("Settings", inner, width, height, True,
                       f"{on_sources}/{len(SOURCES)} sources on")


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


def _help_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    groups = [
        ("Search", [("type", "search (paste a magnet or link to grab)"), ("enter", "details"),
                     ("d", "download"), ("D", "download to a folder"), ("e", "save .torrent"),
                     ("o", "open page in browser"), ("y", "copy magnet"),
                     ("/  i", "edit query"), ("↑ ↓", "recall past searches"),
                     ("r", "retry failed sources"), ("z", "hide dead torrents"),
                     ("filters", "seeders: size: age: files: source: group:"),
                     ("examples", 'matrix -cam size:>1GiB group:movies'),
                     ("S", "cycle sort (seeders/size/newest)"), ("c", "clear results"),
                     ("← →", "filter category"), ("v", "grab magnet/link from clipboard")]),
        ("Details", [("← →", "cycle duplicate source variants"),
                     ("d / D / e", "download / to folder / save .torrent")]),
        ("Settings", [("enter / space", "edit or toggle the selected row"),
                       ("a", "add Torznab feed"),
                       ("on a feed row", "e endpoint · K separate key · x remove"),
                       ("g / esc", "close")]),
        ("Navigate", [("↑ ↓  j k", "move selection / scroll wheel"),
                      ("tab", "switch search / downloads")]),
        ("Downloads", [("p", "pause / resume"), ("x", "cancel (ask: delete or keep files)"),
                       ("r", "retry a failed download"), ("f", "choose files (season packs)"),
                       ("o", "reveal in Finder"), ("s", "resume partial downloads on disk")]),
        ("General", [("g", "settings (sources, download dir)"), ("?", "this help"),
                     ("q", "quit (confirm)"), ("ctrl-c", "quit now")]),
    ]
    rows = [cell("Keys", inner_w, color=T.ACCENT, bold=True), cell("", inner_w)]
    for title, items in groups:
        rows.append(cell(title, inner_w, color=T.ALT, bold=True))
        for keys, desc in items:
            rows.append("  " + cell(keys, 16, color=T.BRIGHT) + " " + cell(desc, inner_w - 20, dim=True))
        rows.append(cell("", inner_w))
    body_h = max(1, height - 2)
    max_scroll = max(0, len(rows) - body_h)
    app.help_scroll = min(app.help_scroll, max_scroll)
    start = app.help_scroll
    shown = rows[start:start + body_h]
    count = f"{start + 1}-{start + len(shown)}/{len(rows)}" if max_scroll else None
    return _wrap_panel("Help", shown, width, height, True, count)


def _footer(app: App, width: int) -> str:
    if app.cancel_prompt is not None:
        hints = [("d", "delete files"), ("k", "keep files"), ("esc", "abort")]
    elif app.folder_prompt is not None:
        hints = [("type", "path"), ("enter", "download"), ("esc", "cancel")]
    elif app.torrent_prompt is not None:
        hints = [("t", "contents"), ("f", ".torrent file"), ("esc", "cancel")]
    elif app.help:
        hints = [("↑↓", "scroll"), ("any key", "close")]
    elif app.settings:
        if app.edit_field:
            hints = [("type", "value"), ("enter", "save"), ("esc", "cancel")]
        elif app.remove_feed is not None:
            hints = [("x/enter", "confirm"), ("esc", "cancel")]
        else:
            items = app.setting_items()
            kind = items[app._snap_setting(app.set_sel)][0]
            if kind == "feed":
                hints = [("↑↓", "move"), ("enter/e", "endpoint"), ("K", "key"),
                         ("x", "remove"), ("space", "toggle"), ("a", "add"), ("g/esc", "close")]
            elif kind == "add-feed":
                hints = [("↑↓", "move"), ("enter", "add"), ("a", "add"), ("g/esc", "close")]
            elif kind == "source":
                hints = [("↑↓", "move"), ("space/enter", "toggle"), ("a", "add"), ("g/esc", "close")]
            elif kind in ("dir", "meta-key"):
                hints = [("↑↓", "move"), ("enter/space", "edit"), ("a", "add"), ("g/esc", "close")]
            else:  # provider / theme
                hints = [("↑↓", "move"), ("enter/space", "toggle"), ("a", "add"), ("g/esc", "close")]
    elif app.picker is not None:
        hints = [("↑↓", "move"), ("space", "toggle"), ("a", "all/none"),
                 ("enter", "apply"), ("esc", "cancel")]
    elif app.detail is not None:
        hints = [("←→", "variant"), ("d", "download"), ("D", "folder"), ("e", ".torrent"),
                 ("o", "page"), ("y", "copy"), ("p", "poster"), ("esc/q", "back")]
    elif app.show_errors:
        hints = [("esc/E", "close"), ("r", "retry"), ("q", "quit")]
    elif app.view == "search" and app.editing:
        hints = [("enter", "search"), ("↑↓", "history"), ("esc", "nav"), ("tab", "downloads"), ("^c", "quit")]
    elif app.view == "search":
        hints = [("↑↓", "move"), ("enter", "details"), ("d", "grab"), ("D", "folder"), ("e", ".torrent"),
                 ("o", "page"), ("y", "copy"),
                 ("r", "retry"), ("E", "errors"), ("z", "hide dead"), ("S", "sort"), ("←→", "category"),
                 ("v", "paste"), ("g", "settings"), ("q", "quit")]
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
    block += [_center(style("─" * box_w, T.RULE), box_w, cols)]
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


def _folder_box(cols: int, app: App) -> list[str]:
    _, name = app.folder_prompt or ("", "")
    label = f"Download to folder — {clean(name)}"
    box_w = min(max(dwidth(label) + 6, 30), cols - 2)
    pre = " " * max(0, (cols - box_w) // 2)
    inner_w = box_w - 4
    shown = dtrunc(clean(app.folder_buf), inner_w)
    caret = "\x1b[7m \x1b[0m" if dwidth(shown) >= inner_w else ""
    path = style("path: ", dim=True) + style(shown, T.TEXT) + caret
    hint = style("enter", T.ACCENT) + style(" download  ", dim=True) \
        + style("esc", T.ACCENT) + style(" cancel", dim=True)
    def _pad_styled(s: str) -> str:
        return s + " " * max(0, inner_w - dwidth(strip_ansi(s)))
    return [
        pre + style("╭" + "─" * (box_w - 2) + "╮", T.ACCENT),
        pre + style("│", T.ACCENT) + _pad_styled(style(dtrunc(label, inner_w), T.TEXT, bold=True)) + style("│", T.ACCENT),
        pre + style("│", T.ACCENT) + cell("", inner_w) + style("│", T.ACCENT),
        pre + style("│", T.ACCENT) + _pad_styled(path) + style("│", T.ACCENT),
        pre + style("│", T.ACCENT) + cell("", inner_w) + style("│", T.ACCENT),
        pre + style("│", T.ACCENT) + _pad_styled(hint) + style("│", T.ACCENT),
        pre + style("╰" + "─" * (box_w - 2) + "╯", T.ACCENT),
    ]


def _overlay(lines: list[str], app: App, cols: int, rows: int) -> list[str]:
    box = (_confirm(cols) if app.confirm_quit
           else _torrent_box(cols) if app.torrent_prompt
           else _cancel_box(cols) if app.cancel_prompt
           else _folder_box(cols, app) if app.folder_prompt is not None
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


@lru_cache(maxsize=8)
def _secret_re(forms: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(map(re.escape, sorted(forms, key=len, reverse=True))), re.I)


def _redact_frame(lines: list[str], app: App) -> list[str]:
    candidates = {form for secret in app._all_secrets() for form in
                  (secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret))
                  if form}
    if not candidates:
        return lines
    secrets = _secret_re(tuple(sorted(candidates)))

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
    use_rail = app.view in ("search", "downloads") and app.detail is None and not app.help \
        and not app.settings and app.picker is None
    content_w = cols - MARGIN - RAIL_W - GAP - 1 if use_rail else cols - MARGIN - 1

    if app.help:
        content = _help_panel(app, content_w, body_h)
    elif app.settings:
        content = _settings_panel(app, content_w, body_h)
    elif app.picker is not None:
        content = _picker_panel(app, content_w, body_h)
    else:
        content = _search_panel(app, content_w) + [""]
        if app.view == "search" and app.show_errors:
            content += _errors_panel(app, content_w, panel_h)
        elif app.view == "search" and app.detail is not None:
            content += _detail_panel(app, app.detail, content_w, panel_h)
        elif app.view == "search":
            content += _results_panel(app, content_w, panel_h)
        else:
            content += _downloads_panel(app, content_w, panel_h)
    content = (content + [""] * body_h)[:body_h]
    rail = _rail(app, body_h) if use_rail else []

    for i in range(body_h):
        lines.append(" " * MARGIN + (rail[i] + " " * GAP if use_rail else "") + content[i])

    lines.append("")
    lines.append(" " * MARGIN + _footer(app, cols - MARGIN))
    lines = (lines + [""] * rows)[:rows]
    return _redact_frame(_overlay(lines, app, cols, rows), app)

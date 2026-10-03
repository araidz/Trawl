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
import tempfile
import tty
import unicodedata
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Sequence
from functools import cache, lru_cache

from . import __version__, theme as T
from .aria2 import STATE_DIR, Aria2Error, Download, control_infohash, torrent_files
from .sources import (SOURCES, LocalQuery, Source, Replay, Result, ResultVariant, Search, SourceError, SourceUpdate, TorznabFeed,
                      build_magnet, dedupe, fetch_json, make_torznab_source, matches_query, torrentio_releases,
                      parse_magnet, parse_query, parse_release, parse_source, redact, redact_url,
                      result_identity, torznab_label, validate_torznab_url)
from .follow import CHECK_EVERY, check_sub, episode_of, fmt_ep, load_subs, make_sub, save_subs
from .meta import Meta, clean_title, kind_for, lookup

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

PENDING_FILE = STATE_DIR / "pending.jsonl"  # direct-http grabs, for scan_resume
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
RAIL_MIN_COLS = 80
MAX_DL_RANGE = (1, 20)  # the simultaneous-downloads setting  # narrower terminals drop the category rail so the panels fit
MARGIN = 2
GAP = 2

# -- ANSI + width primitives -------------------------------------------------

RESET = "\x1b[0m"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@lru_cache(maxsize=4096)
def _fg(hexc: str) -> str:
    if T.COLOR_MODE == "none":
        return ""
    n = int(hexc[1:], 16)
    r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
    if T.COLOR_MODE == "256":
        return f"\x1b[38;5;{T.rgb_to_256(r, g, b)}m"
    return f"\x1b[38;2;{r};{g};{b}m"


def style(text: str, color: str | None = None, bold: bool = False, dim: bool = False) -> str:
    pre = ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else "") + (_fg(color) if color else "")
    return f"{pre}{text}{RESET}" if pre else text


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


_ANSI_SPLIT = re.compile(r"(\x1b\[[0-9;?]*[A-Za-z])")


def clip(line: str, w: int) -> str:
    """Cut a styled line to `w` display columns, keeping its escape codes intact. A line wider
    than the terminal wraps and shoves every row below it down, so no frame line may exceed it."""
    if dwidth(strip_ansi(line)) <= w:
        return line
    out, used = [], 0
    for part in _ANSI_SPLIT.split(line):
        if part.startswith("\x1b["):
            out.append(part)
            continue
        for ch in part:
            cw = _cw(ch)
            if used + cw > w:
                return "".join(out) + RESET
            out.append(ch)
            used += cw
    return "".join(out) + RESET


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
    if i and n >= 1000:  # 1000-1023.99 would print 10 chars; the size column is 9 wide
        return f"{n:.1f} {units[i]}"
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


def fuzzy_score(query: str, text: str) -> int | None:
    """Lower is better; None if the query's letters don't appear in order in `text`.
    Spans that start early and stay tight win, and a match on a word start gets a bonus."""
    q, t, pos, last = query.lower().replace(" ", ""), text.lower(), [], -1
    for ch in q:
        last = t.find(ch, last + 1)
        if last < 0:
            return None
        pos.append(last)
    if not pos:
        return 0
    starts = sum(1 for i in pos if i == 0 or t[i - 1] in " :-/")
    return (pos[-1] - pos[0]) * 2 + pos[0] - starts * 3


def open_target(path: str, name: str) -> str:
    """What `enter` opens for a finished download: the torrent's own top-level
    folder for multi-file torrents (the ancestor named like the torrent), else the file."""
    p = os.path.dirname(path)
    while p != os.path.dirname(p):
        if os.path.basename(p) == name:
            return p
        p = os.path.dirname(p)
    return path


SPACE_MARGIN = 1 << 30  # keep 1 GiB free beyond the torrent itself
PEEK_TIMEOUT = 45  # seconds to wait for a magnet's file list before giving up
CACHE_TTL = 600  # a finished search is replayed from memory for 10 minutes
CACHE_MAX = 20
PALETTE_ROWS = 9  # command-palette list height
STATUS_TTL_ACK = 4.0    # "paused: X", "sorted by size"…: a key press acknowledged, gone quickly
STATUS_TTL = 8.0        # other information
STATUS_TTL_WARN = 20.0  # warnings and errors linger longer
_ACK = ("paused", "resumed", "sorted", "hiding", "showing", "copied", "magnet copied", "opened", "revealed",
        "cancelled", "marked", "unfollowed", "retrying")
SUB_RETRY = 600  # seconds before an unanswered follow check is tried again
QUARANTINE_AFTER = 3  # searches in a row a source may fail before it's paused for the session


def free_space(path: str) -> int | None:
    """Free bytes on the volume that will hold `path` (nearest existing parent)."""
    p = os.path.abspath(os.path.expanduser(path or "~"))
    while not os.path.exists(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


UPDATE_FILE = STATE_DIR / "update.json"
UPDATE_URL = "https://api.github.com/repos/araidz/Trawl/releases/latest"


def _newer(tag: str, current: str) -> bool:
    try:
        return tuple(map(int, tag.lstrip("v").split("."))) > tuple(map(int, current.split(".")))
    except ValueError:
        return False


def update_available() -> str:
    """The latest release tag if it's newer than this build, else "". Asks GitHub
    at most once a day (cached in update.json); any failure just means no notice."""
    try:
        cache = json.loads(UPDATE_FILE.read_text())
        tag, ts = str(cache["tag"]), float(cache["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        tag, ts = "", 0.0
    if time.time() - ts > 86400:
        try:
            tag = str(fetch_json(UPDATE_URL, timeout=5)["tag_name"])
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            UPDATE_FILE.write_text(json.dumps({"ts": time.time(), "tag": tag}))
        except (SourceError, KeyError, TypeError, OSError):
            pass
    return tag.lstrip("v") if _newer(tag, __version__) else ""


AWAKE_MODES = ("on", "ac", "off")  # keep the Mac awake while downloading: always / on the charger / never
_power: list = [0.0, True]  # (checked at, on AC) — pmset is asked at most every 30 s


def on_ac_power() -> bool:
    """True on the charger (or a desktop Mac, or if we can't tell)."""
    if time.monotonic() - _power[0] > 30:
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=2).stdout
            _power[:] = [time.monotonic(), "Battery Power" not in out.split("\n", 1)[0]]
        except (OSError, subprocess.SubprocessError):
            _power[:] = [time.monotonic(), True]
    return _power[1]


def start_caffeinate() -> subprocess.Popen | None:
    """Hold off idle *system* sleep (the screen may still sleep) for as long as this process
    lives: -w ties it to our pid, so a crash or kill can never leave the Mac stuck awake."""
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


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
_CTRL = {0x01: "ctrl-a", 0x05: "ctrl-e", 0x0b: "ctrl-k", 0x15: "ctrl-u", 0x17: "ctrl-w"}


def parse_keys(data: bytes) -> list[str]:
    keys: list[str] = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x1b:
            if data[i:i + 3] == b"\x1b[<":  # SGR mouse \x1b[<btn;x;y(M|m): keyboard only, so swallowed
                j = i + 3
                while j < n and data[j] not in (ord("M"), ord("m")):
                    j += 1
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
        # mouse reporting stays on only so the wheel arrives as mouse codes (which parse_keys drops):
        # with it off, Terminal.app turns wheel turns into arrow keys on full-screen apps
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
        self.peek_target: tuple[str, str] | None = None  # (uri, name) when the picker is a pre-grab peek
        self._peek: tuple | None = None  # in-flight (gid, infohash, uri, name, tmpdir, started)
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
        self.speed_limit: str | None = cfg.get("speed_limit")  # e.g. "2M"; None = unlimited
        md = cfg.get("max_downloads")
        lo, hi = MAX_DL_RANGE
        self.max_dl_set: int | None = md if isinstance(md, int) and lo <= md <= hi else None  # None = aria2.conf decides
        self.max_dl: int | None = self.max_dl_set  # what aria2 actually runs at once (main fills it in)
        self.clipboard_seen = ""  # last clipboard content offered for v
        self.dl_history: list[dict] = load_dl_history()  # completed downloads, oldest->newest
        self.settings = False  # settings overlay open
        self.set_sel = 0  # settings selection (never a 'section' row)
        self.edit_field: str | None = None  # settings text-edit: "dir" | "key"
        self.edit_buf = ""
        self.remove_feed: str | None = None
        self.show_errors = False  # per-source failure viewer over the results
        self.hide_dead = bool(cfg.get("hide_dead", False))
        self.update_check = bool(cfg.get("update_check", True))
        self.keep_awake = cfg.get("keep_awake") if cfg.get("keep_awake") in AWAKE_MODES else "on"
        self._caff: subprocess.Popen | None = None  # the caffeinate holding the Mac awake, if any
        self.update_tag = ""  # newer release found by check_update, shown in header/splash
        self._space_warned = ""  # uri whose low-disk warning was shown; pressing again overrides
        self.folder_prompt: tuple[list[tuple[str, str]], str, int] | None = None  # ([(uri, name)], label, bytes) for D
        self.marked: set[str] = set()  # _mkey of results ticked with space for a batch grab
        self.palette = False  # command palette open (ctrl-k, or : from a results/downloads view)
        self.rail_keys_shown = False  # render: the rail listed this screen's keys (the footer then shrinks)
        self.pal_buf = ""
        self.pal_sel = 0
        self.filtering = False  # typing into the live results filter (f)
        self.filter_buf = ""
        self.filter_q: LocalQuery | None = None
        self._cache: dict[tuple, tuple[float, tuple]] = {}  # (query, source ids) -> (when, updates)
        self._cache_key: tuple | None = None
        self._log: list = []  # this search's SourceUpdates, kept to fill the cache
        self.subs: list[dict] = load_subs()  # followed shows
        self.sub_new: dict[str, list[Result]] = {}  # show id -> releases newer than its last episode
        self.following = False  # the Following overlay (W)
        self.follow_sel = 0
        self._unfollow = ""  # show id waiting for a second x to confirm
        self._sub_busy = False
        self._sub_tried = 0.0  # when a check last started: a failing one isn't retried for SUB_RETRY
        self._sub_done: list = []  # (show id, new releases, sources answered) from the check thread; None = finished
        self._other: tuple | None = None  # (title, results, error) handed back by the Torrentio thread
        self._other_busy = False
        self._other_for = None  # the search that was showing when t was pressed
        self.last_update: dict[str, SourceUpdate] = {}  # newest answer per source, for the health view
        self.fail_streak: dict[str, int] = {}  # source id -> consecutive failed searches
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
                             and self.source_reports_health(r.source))
                     and (not self.filter_q or matches_query(r, self.filter_q, self.source_by_id)))
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
        self.sel = max(0, min(self.sel, len(rs) - 1))  # z/filters/categories can shrink the list under sel
        return rs[self.sel] if rs else None

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
        self.source_by_id["torrentio"] = Source("torrentio", "Torrentio", "Other", lambda q: [], browse=False)
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
        if source_id in {s.id for s in SOURCES} or source_id == "torrentio":
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

    def paused_sources(self) -> set[str]:
        """Sources skipped after failing QUARANTINE_AFTER searches in a row (R lifts it)."""
        return {sid for sid, n in self.fail_streak.items() if n >= QUARANTINE_AFTER}

    def enabled_sources(self) -> list:
        paused = self.paused_sources()
        return [s for s in self.sources if s.id not in self.disabled_sources and s.id not in paused]

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status, self._status_at = value, time.monotonic()

    def status_live(self) -> tuple[str, str] | None:
        """(message, colour) while the last status message is still fresh, else None.
        Warnings and errors (needing a second key press, a failure, no disk space) linger."""
        msg = self._status
        if not msg:
            return None
        low = msg.lower()
        if low.startswith(("error", "couldn't", "(no engine)")) or "failed" in low:
            color, ttl = T.BAD, STATUS_TTL_WARN
        elif "press " in low or low.startswith(("not enough", "only ", "nothing", "no ", "torrentio:")):
            color, ttl = T.WARN, STATUS_TTL_WARN
        elif low.startswith(("grabbing", "following", "saved", "opened", "resumed", "revealed", "unfollowed",
                             "marked", "new episodes", "downloading")) or "copied" in low:
            color, ttl = T.GOOD, STATUS_TTL
        else:
            color, ttl = T.ALT, STATUS_TTL
        if low.startswith(_ACK):
            ttl = STATUS_TTL_ACK
        return (msg, color) if time.monotonic() - self._status_at < ttl else None

    def activity(self) -> list[str]:
        """What the app is doing right now, for the live indicator (empty = idle)."""
        out = []
        if self.search is not None and self.search_done < self.search_total:
            out.append(f"searching {self.search_done}/{self.search_total}")
        if self._peek:
            out.append("reading file list")
        if self._other_busy:
            out.append("asking Torrentio")
        if self._sub_busy:
            out.append("checking followed shows")
        meta = sum(1 for d in self.downloads if d.status == "metadata")
        if meta:
            out.append(f"fetching metadata ×{meta}" if meta > 1 else "fetching metadata")
        if self._exports:
            out.append("saving .torrent")
        return out

    @property
    def tick(self) -> float:
        return (time.monotonic() - self.start) * 1000 / T.SHEEN_TICK_MS

    # -- actions
    def submit(self, fresh: bool = False) -> None:
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
        self._cache_key = (q, tuple(sorted(s.id for s in srcs)))
        self._log = []
        self.last_update = {}
        hit = None if fresh else self._cache.get(self._cache_key)
        age = time.monotonic() - hit[0] if hit else CACHE_TTL
        cached = age < CACHE_TTL
        self.search = Replay(hit[1], srcs) if cached else Search(self.local_query.remote, srcs)
        self.search_total = getattr(self.search, "total", len(srcs))
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.marked.clear()
        self._set_filter("")
        self.editing = False
        self.detail = None
        if q:
            self._add_history(q)
        self.status = f'searching "{clean(q)}"' if q else "loading latest"
        if cached:
            self.status = f"from cache ({int(age // 60)}m {int(age % 60)}s old) — R searches again"
        elif self.local_query.malformed:
            self.status = f"searching; ignored {len(self.local_query.malformed)} malformed filter(s)"

    def _finish_search(self) -> None:
        """Once every source has answered: count consecutive failures (a search where
        *everything* failed means we're offline, not that sources died) and cache the
        search if none failed."""
        key, log = self._cache_key, self._log
        self._cache_key = None
        if not key or not log or isinstance(self.search, Replay):
            return
        failed = {u.source for u in log if u.results is None}
        if len(failed) < len(log):
            for u in log:
                self.fail_streak[u.source] = self.fail_streak.get(u.source, 0) + 1 if u.results is None else 0
        if not failed:
            self._cache[key] = (time.monotonic(), tuple(log))
            while len(self._cache) > CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))

    def _set_filter(self, text: str) -> None:
        self.filter_buf = text
        self.filter_q = parse_query(text) if text.strip() else None
        self.sel = 0
        if not text:
            self.filtering = False

    def _filter_key(self, k: str) -> None:
        """Typing into the live results filter (`f`): same operators as the search box."""
        if k == "enter":
            self.filtering = False
        elif k == "esc":
            self._set_filter("")
        elif k == "backspace":
            self._set_filter(self.filter_buf[:-1])
            self.filtering = True
        elif k == "ctrl-u":
            self._set_filter("")
            self.filtering = True
        elif k in ("up", "down", "pageup", "pagedown"):
            self._move({"up": -1, "down": 1, "pageup": -8, "pagedown": 8}[k])
        elif len(k) == 1 and k >= " ":
            self._set_filter(self.filter_buf + k)

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
        self.marked.clear()
        self._set_filter("")
        self.query = ""
        self.cursor = 0
        self.editing = False
        self.detail = None
        self.variant_idx = 0
        self.show_errors = False
        self.status = ""

    def grab(self, magnet: str, name: str, dir_: str | None = None,
             select: Iterable[int] = ()) -> bool:
        secrets = self._all_secrets()
        if not self.eng:
            self.status = redact(f"(no engine) {clean(name)[:48]}", secrets)
            return False
        opts = {"dir": dir_} if dir_ else {}
        if select:  # 1-based file numbers; aria2 applies them once a magnet's metadata arrives
            opts["select-file"] = ",".join(map(str, sorted(select)))
        try:
            self.eng.add(magnet, opts or None)
            uri = magnet if magnet.lower().startswith(("http://", "https://")) else ""
            self._record_pending(uri, dir_)  # http links: remembered for scan_resume
            self.status = redact(f"grabbing: {clean(name)[:48]}", secrets)
            return True
        except Aria2Error as e:
            self.status = redact(f"error: {e}", secrets)
            return False

    def _mkey(self, r: Result) -> str:
        return result_identity(r) or r.magnet

    def _marked_results(self) -> list[Result]:
        """Marked rows that are currently visible, in list order."""
        return [r for r in self.visible_results() if self._mkey(r) in self.marked] if self.marked else []

    def _grab_items(self, items: list[tuple[str, str]], size: int, dir_: str | None = None) -> None:
        """Grab one or several (uri, name) items behind a single disk-space check.
        Several = the marked rows: marks clear and one summary replaces per-item status."""
        if not items or not self._space_ok("|".join(u for u, _ in items), size, dir_):
            return
        ok = sum(bool(self.grab(uri, name, dir_) if dir_ else self.grab(uri, name)) for uri, name in items)
        if len(items) > 1:
            self.status = (f"grabbing {ok} downloads · {fmt_bytes(size)}" if ok == len(items)
                           else f"grabbing {ok} of {len(items)} — {self.status}")
            self.marked.clear()

    def _record_pending(self, uri: str, dir_: str | None) -> None:
        """Stash direct-http grabs so scan_resume can re-add unfinished ones
        (.aria2 control files only carry BT infohashes)."""
        if not uri:
            return
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with PENDING_FILE.open("a") as f:
                f.write(json.dumps({"uri": uri, "dir": dir_}) + "\n")
        except OSError:
            pass

    def _space_ok(self, uri: str, size: int, dir_: str | None = None) -> bool:
        """False (once) when `size` won't fit with SPACE_MARGIN to spare; pressing
        the same grab key again overrides. Unknown size or free space never blocks."""
        free = free_space(dir_ or self.download_dir or (self.eng.download_dir() if self.eng else "") or "")
        if not size or free is None or size + SPACE_MARGIN <= free or self._space_warned == uri:
            self._space_warned = ""
            return True
        self._space_warned = uri
        self.status = f"only {fmt_bytes(free)} free, needs {fmt_bytes(size)} — press again to grab anyway"
        return False

    def _start_folder_prompt(self, items: list[tuple[str, str]], size: int = 0) -> None:
        label = items[0][1] if len(items) == 1 else f"{len(items)} downloads"
        self.folder_prompt = (items, label, size)
        self.folder_buf = self.last_dir or self.download_dir or ""

    def _commit_folder_prompt(self) -> None:
        items, _, size = self.folder_prompt or ([], "", 0)
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
        self._grab_items(items, size, path)
        if self.view == "search" and not self._space_warned:
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
        elif pm.kind == "file":
            self.grab_file(pm.magnet, pm.name)
        else:
            self.grab(pm.magnet, pm.name)

    def grab_file(self, path: str, name: str) -> None:
        """A local .torrent (dropped onto the window or passed as an argument)."""
        if not self.eng:
            self.status = f"(no engine) {clean(name)[:48]}"
            return
        try:
            self.eng.add_torrent_file(path)
            self.status = f"grabbing: {clean(name)[:48]}"
        except (Aria2Error, OSError) as e:
            self.status = f"error: {e}"

    def check_update(self) -> None:
        """Background thread: note a newer release for the header/splash."""
        if self.update_check:
            self.update_tag = update_available()

    def _quit(self) -> None:
        """Quit at once when nothing is in flight; otherwise ask first."""
        if any(d.status in ("active", "waiting", "metadata") for d in self.downloads):
            self.confirm_quit = True
        else:
            self.running = False

    def grab_torrent(self, url: str, name: str, contents: bool) -> None:
        """A .torrent link: follow-torrent=mem grabs its contents; =false saves
        just the .torrent file. aria2 handles both natively."""
        secrets = self._all_secrets()
        if not self.eng:
            self.status = redact(f"(no engine) {clean(name)[:48]}", secrets)
            return
        try:
            self.eng.add(url, {"follow-torrent": "mem" if contents else "false"})
            if url.lower().startswith(("http://", "https://")):
                self._record_pending(url, None)
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
        """Re-add incomplete downloads found on disk that aria2 isn't already
        running: BT via *.aria2 control files (infohash), direct-http via the
        remembered pending.jsonl."""
        if not self.eng:
            return 0
        have = self.eng.active_infohashes() | self.eng.active_uris()
        n = 0
        dir_path = self.eng.download_dir()
        if dir_path and os.path.isdir(dir_path):
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
        pending = self._load_pending()
        for rec in pending:
            uri = rec.get("uri", "")
            if not uri or uri in have:
                continue
            opts = {"dir": rec["dir"]} if rec.get("dir") else None
            try:
                self.eng.add(uri, opts)
                have.add(uri)
                n += 1
            except Aria2Error:
                pass
        if pending:
            self._clear_pending()
        return n

    def _load_pending(self) -> list[dict]:
        out: list[dict] = []
        try:
            for ln in PENDING_FILE.read_text().splitlines():
                try:
                    rec = json.loads(ln)
                    if isinstance(rec, dict) and rec.get("uri"):
                        out.append(rec)
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def _clear_pending(self) -> None:
        try:
            PENDING_FILE.unlink()
        except OSError:
            pass

    def check_clipboard(self) -> None:
        """If a grabbable magnet/link appears on the clipboard, offer v once."""
        clip = paste_clipboard()
        bare_hash = re.fullmatch(r"\s*[0-9a-fA-F]{40}\s*", clip or "")  # a git commit hash looks the same
        if clip and clip != self.clipboard_seen and not bare_hash and parse_source(clip) and not self.editing:
            self.clipboard_seen = clip
            self.status = "magnet or link detected in clipboard — press v to grab it"

    def _save_settings(self) -> None:
        self.config.update({"disabled_sources": sorted(self.disabled_sources),
                            "download_dir": self.download_dir, "speed_limit": self.speed_limit,
                            "max_downloads": self.max_dl_set,
                            "meta_provider": self.meta_provider,
                            "theme": self.theme, "hide_dead": self.hide_dead,
                            "update_check": self.update_check, "keep_awake": self.keep_awake,
                            "tmdb_key": self.tmdb_key, "omdb_key": self.omdb_key,
                            "torznab_feeds": [dict(feed) for feed in self.torznab_feeds]})
        save_config(self.config)

    def _set_theme(self, name: str) -> None:
        self.theme = T.set_theme(name)
        _logo_lines.cache_clear()  # the gradient logo is baked at first render
        _bar_cells.cache_clear()   # bar gradients read DEEP/BRIGHT, which the cache key omits
        _static_bar.cache_clear()

    def setting_items(self) -> list[tuple[str, object]]:
        return ([('section', 'General'), ('dir', None), ('concurrency', None), ('limit', None), ('provider', None),
                 ('meta-key', None), ('theme', None), ('updates', None), ('awake', None),
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
            elif k in ("left", "right") and kind == "concurrency":
                self.set_concurrency((self.max_dl or 5) + (1 if k == "right" else -1))
            elif k in ("enter", " ") and kind == "concurrency":
                self.edit_field, self.edit_buf = "concurrency", str(self.max_dl or "")
            elif k in ("enter", " ") and kind in ("dir", "limit"):
                self.edit_field = kind
                self.edit_buf = (self.download_dir or (self.eng.download_dir() if self.eng else "") or ""
                                 if kind == "dir" else self.speed_limit or "")
            elif k in ("enter", " ") and kind == "provider":
                self.meta_provider = "omdb" if self.meta_provider == "tmdb" else "tmdb"
                self.meta.clear()  # cached results are provider-specific
                self._save_settings()
            elif k in ("enter", " ") and kind == "meta-key":
                self.edit_field = "key"
                self.edit_buf = self._provider_key() or ""
            elif k in ("enter", " ") and kind == "theme":
                self._set_theme(T.THEMES[(T.THEMES.index(self.theme) + 1) % len(T.THEMES)])
                self._save_settings()
            elif k in ("enter", " ") and kind == "updates":
                self.update_check = not self.update_check
                self._save_settings()
            elif k in ("enter", " ") and kind == "awake":
                self.keep_awake = AWAKE_MODES[(AWAKE_MODES.index(self.keep_awake) + 1) % len(AWAKE_MODES)]
                self._save_settings()
                self.update_awake()
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

    def set_concurrency(self, n: int) -> None:
        """How many downloads run at once; the rest wait as queued. Applied live and remembered."""
        lo, hi = MAX_DL_RANGE
        n = max(lo, min(hi, n))
        self.max_dl = self.max_dl_set = n
        if self.eng:
            self.eng.set_max_concurrent(n)
        self._save_settings()
        self.status = f"downloads at once: {n}"

    def _commit_edit(self) -> None:
        if self.edit_field == "dir":
            self.download_dir = self.edit_buf.strip() or None
            if self.eng and self.download_dir:
                self.eng.set_dir(self.download_dir)
        elif self.edit_field == "limit":
            raw = self.edit_buf.strip()
            if raw and not re.fullmatch(r"\d+[KMG]?", raw, re.I):
                self.status = "speed limit: number + optional K/M/G suffix"
                return
            self.speed_limit = raw or None
            if self.eng:
                self.eng.set_limit(raw or "0")  # 0 = unlimited
        elif self.edit_field == "concurrency":
            raw, (lo, hi) = self.edit_buf.strip(), MAX_DL_RANGE
            if not (raw.isdigit() and lo <= int(raw) <= hi):
                self.status = f"downloads at once: a number from {lo} to {hi}"
                return
            self.edit_field = None
            self.set_concurrency(int(raw))
            return
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

    def start_peek(self, uri: str, name: str) -> None:
        """Details `f`: fetch just the metadata into a temp dir, then show the
        torrent's files so a pack can be inspected (and partly grabbed) up front."""
        pm = parse_magnet(uri)
        if not pm:
            self.status = "no file list for direct links"
        elif not self.eng:
            self.status = "(no engine)"
        elif self._peek:
            self.status = "still reading a file list…"
        else:
            tmp = tempfile.mkdtemp(prefix="trawl-peek-")
            try:
                gid = self.eng.save_metadata(uri, tmp)
            except Aria2Error as e:
                shutil.rmtree(tmp, ignore_errors=True)
                self.status = redact(f"error: {e}", self._all_secrets())
                return
            self._peek = (gid, pm.info_hash, uri, name, tmp, time.monotonic())
            self.status = "reading file list…"

    def end_peek(self) -> None:
        """Drop the metadata task and its temp dir (also runs on quit, so a stray
        task can't come back from the session file as a phantom download)."""
        if not self._peek:
            return
        gid, _, _, _, tmp, _ = self._peek
        self._peek = None
        try:
            if self.eng:
                self.eng.remove(gid)
        except Aria2Error:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    def _poll_peek(self) -> None:
        gid, ih, uri, name, tmp, started = self._peek
        st = self.eng.status(gid) if self.eng else "error"
        files = None
        if st == "complete":  # the .torrent write can lag the status flip: retry next tick
            try:
                with open(os.path.join(tmp, f"{ih}.torrent"), "rb") as f:
                    files = torrent_files(f.read())
            except (OSError, ValueError):
                pass
        if files is None and st != "error" and time.monotonic() - started <= PEEK_TIMEOUT:
            return
        self.end_peek()
        if files is None:
            self.status = "couldn't read the file list (no peers answered)"
        elif not (self.detail is not None and (v := self._variant()) and v.uri == uri):
            return  # the user moved on; nothing to show
        elif len(files) < 2:
            self.status = (f"single file: {clean(files[0]['path'])[:40]} · {fmt_bytes(files[0]['length'])}"
                           if files else "torrent lists no files")
        else:
            self.picker = Download("", name, "peek", 0, 0, 0, 0, None)
            self.peek_target = (uri, name)
            self.picker_files, self.picker_sel = files, 0
            self.picker_on = {f["index"] for f in files}
            self.picker_bytes = sum(f["length"] for f in files)
            self.status = ""

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
            self.picker = self.peek_target = None
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
            if self.peek_target:  # pre-grab peek: start the download with just these files
                uri, name = self.peek_target
                if not self._space_ok(uri, self.picker_bytes):
                    return
                every = len(self.picker_on) == n  # everything ticked: a plain grab
                self.grab(uri, name, select=() if every else self.picker_on)
                self.picker = self.peek_target = self.detail = None
                self.variant_idx = 0
                return
            ok = self.eng.select_files(self.picker.root, sorted(self.picker_on)) if self.eng else False
            self.status = (f"downloading {len(self.picker_on)}/{n} files" if ok
                           else "couldn't set file selection")
            self.picker = None

    def drain_search(self) -> bool:
        """Drain finished source updates into the result list. True if the
        search state changed (caller may then skip a redundant render)."""
        if self._other:
            self._take_releases()
        if self._sub_done:
            self._take_subs()
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
            self._log.append(u)
            self.last_update[u.source] = u
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
            if self.search_done >= self.search_total:
                self._finish_search()
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
        peek = self._peek[0] if self._peek else None
        for d in downloads:
            if d.root in self._exports or d.root == peek:
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
            if d.root == peek:
                continue
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
        self.dsel = min(self.dsel, max(0, len(kept) - 1))  # keys act on a real row after the list shrinks
        if self._peek:
            self._poll_peek()
        self.update_awake()

    @property
    def awake(self) -> bool:
        return self._caff is not None and self._caff.poll() is None

    def update_awake(self) -> None:
        """Keep the Mac from idle-sleeping exactly while something is downloading (a sleeping
        Mac freezes aria2 until you wake it). Paused, queued or finished downloads don't count."""
        want = (self.keep_awake != "off" and any(d.status in ("active", "metadata") for d in self.downloads)
                and (self.keep_awake == "on" or on_ac_power()))
        if want and not self.awake:
            self._caff = start_caffeinate()
        elif not want and self._caff is not None:
            self.release_awake()

    def release_awake(self) -> None:
        if self._caff is not None:
            try:
                self._caff.terminate()
                self._caff.wait(timeout=2)
            except Exception:  # already gone, or won't die: -w still ends it when trawl exits
                pass
            self._caff = None

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

    def start_releases(self) -> None:
        """Details `t`: every known release of this title (Cinemeta -> IMDb id -> Torrentio)."""
        r = self.detail
        if self._other_busy:
            self.status = "still asking Torrentio…"
            return
        group = self.result_group(r) if r else None
        title, year = clean_title(r.name) if r else ("", None)
        ep = re.search(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})\b", r.name, re.I) if r else None
        if group in ("Games", "Books", "Anime") or not title:
            self.status = f"no release lookup for {(group or 'this').lower()} results"
        elif (group == "TV" or ep) and not ep:
            self.status = "Torrentio lists releases per episode — open an SxxEyy result"
        else:
            kind, sn, en = ("series", int(ep.group(1)), int(ep.group(2))) if ep else ("movie", None, None)
            self._other_busy, self._other_for = True, self.search
            self.status = f'asking Torrentio about "{clean(title)}"…'
            threading.Thread(target=self._fetch_releases, args=(title, year, kind, sn, en), daemon=True).start()

    def _fetch_releases(self, title: str, year: str | None, kind: str, sn: int | None, en: int | None) -> None:
        try:
            self._other = (title, torrentio_releases(title, year, kind, sn, en), "")
        except SourceError as e:
            self._other = (title, [], str(e))
        except Exception as e:  # a thread must never die silently
            self._other = (title, [], str(e) or type(e).__name__)

    def _take_releases(self) -> None:
        title, rs, err = self._other
        self._other, self._other_busy = None, False
        if err or not rs:
            self.status = f"Torrentio: {err}" if err else f'Torrentio knows no releases of "{clean(title)}"'
            return
        if self.search is not self._other_for:  # the user ran another search meanwhile: don't replace it
            self.status = f'Torrentio found {len(rs)} releases of "{clean(title)}" — open it again and press t'
            return
        self._show_results(title, rs)
        self.status = f'{len(rs)} releases of "{clean(title)}" from Torrentio'

    def _show_results(self, query: str, rs: list[Result], source: str = "torrentio") -> None:
        """Replace the results list with `rs` (already-found releases), as if searched."""
        self.detail, self.variant_idx = None, 0
        self.local_query, self.query = LocalQuery(""), query
        self.search, self.search_total = Replay((SourceUpdate(source, rs),), []), 1
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.marked.clear()
        self._set_filter("")
        self._cache_key, self._log, self.last_update = None, [], {}
        self.view = "search"

    # -- following shows
    def follow_current(self) -> None:
        r = self.detail or self._cur()
        sub = make_sub(r.name, self.result_group(r) or "") if r else None
        if not sub:
            self.status = "open a TV or anime episode result (SxxEyy, or 'Show - 12') to follow its show"
            return
        old = next((x for x in self.subs if x["id"] == sub["id"]), None)
        if old:
            old.update(res=sub["res"], last=sub["last"], group=sub["group"])
            self.sub_new.pop(old["id"], None)
        else:
            self.subs.append(sub)
        save_subs(self.subs)
        q = f" ({sub['res']}p)" if sub["res"] else ""
        self.status = f"following {sub['title']}{q} — new episodes after {fmt_ep(sub['last'])} appear under W"

    def new_count(self) -> int:
        return sum(len(v) for v in self.sub_new.values())

    def start_check(self, force: bool = False) -> None:
        """Look for new episodes of followed shows (each at most every CHECK_EVERY seconds)."""
        due = [x for x in self.subs if force or time.time() - x["checked"] > CHECK_EVERY]
        if self._sub_busy or not due or (not force and time.time() - self._sub_tried < SUB_RETRY):
            return
        self._sub_busy, self._sub_tried = True, time.time()
        threading.Thread(target=self._check_subs, args=(due, self.enabled_sources()), daemon=True).start()

    def _check_subs(self, due: list[dict], sources: list) -> None:
        for sub in due:
            try:
                new, answered = check_sub(sub, [s for s in sources if s.group in (sub["group"], "Other")])
            except Exception:  # one show's failure must not stop the rest
                new, answered = [], 0
            self._sub_done.append((sub["id"], new, answered))
        self._sub_done.append(None)

    def _take_subs(self) -> None:
        found = []
        while self._sub_done:
            item = self._sub_done.pop(0)
            if item is None:
                self._sub_busy = False
                continue
            sid, new, answered = item
            sub = next((x for x in self.subs if x["id"] == sid), None)
            if not sub or not answered:
                continue
            sub["checked"] = time.time()
            self.sub_new[sid] = new
            if new and sub["auto"]:
                self._grab_new(sub)
            elif new:
                found.append((sub, new))
        save_subs(self.subs)
        if found:
            names = ", ".join(f"{x['title']} {fmt_ep(episode_of(n[-1].name))}" for x, n in found[:3])
            self.status = f"new episodes: {names}{' …' if len(found) > 3 else ''} — W"
            notify("trawl — new episodes", names)

    def _grab_new(self, sub: dict) -> bool:
        """Download the best release of every new episode of `sub`, then move its baseline."""
        new = self.sub_new.get(sub["id"]) or []
        size = sum(r.size for r in new)
        free = free_space(self.download_dir or (self.eng.download_dir() if self.eng else "") or "")
        if not new:
            return False
        if size and free is not None and size + SPACE_MARGIN > free:
            self.status = f"not enough disk space for {len(new)} new episode(s) of {sub['title']}"
            return False
        if not sum(bool(self.grab(r.magnet, r.name)) for r in new):
            return False
        sub["last"] = list(episode_of(new[-1].name))
        self.sub_new[sub["id"]] = []
        save_subs(self.subs)
        self.status = f"grabbing {len(new)} new episode(s) of {sub['title']} (now at {fmt_ep(sub['last'])})"
        notify("trawl — grabbing new episodes", f"{sub['title']} · {len(new)}")
        return True

    def open_following(self) -> None:
        self.following, self.follow_sel, self._unfollow = True, 0, ""

    def _following_key(self, k: str) -> None:
        n = len(self.subs)
        if k in ("esc", "W", "q"):
            self.following = False
        elif k in ("up", "k", "down", "j") and n:
            self.follow_sel = (self.follow_sel + (-1 if k in ("up", "k") else 1)) % n
            self._unfollow = ""
        elif k == "c":
            self.start_check(force=True)
            self.status = "checking followed shows…" if self.subs else "not following anything yet"
        elif n:
            self.follow_sel = min(self.follow_sel, n - 1)
            sub = self.subs[self.follow_sel]
            new = self.sub_new.get(sub["id"]) or []
            if k != "x":
                self._unfollow = ""
            if k == "enter":
                if new:
                    self._show_results(sub["title"], new, "follow")
                    self.following = False
                    self.status = f"{len(new)} new episode(s) of {sub['title']}"
                else:
                    self.status = f"no new episodes of {sub['title']}"
            elif k == "g":
                if not self._grab_new(sub) and not new:
                    self.status = f"no new episodes of {sub['title']}"
            elif k == "m" and new:
                sub["last"] = list(episode_of(new[-1].name))
                self.sub_new[sub["id"]] = []
                save_subs(self.subs)
                self.status = f"{sub['title']}: marked seen up to {fmt_ep(sub['last'])}"
            elif k == "a":
                sub["auto"] = not sub["auto"]
                save_subs(self.subs)
                self.status = (f"{sub['title']}: new episodes will download automatically" if sub["auto"]
                               else f"{sub['title']}: auto-grab off")
            elif k == "x":
                if self._unfollow == sub["id"]:
                    self.subs.remove(sub)
                    self.sub_new.pop(sub["id"], None)
                    self._unfollow, self.follow_sel = "", max(0, self.follow_sel - 1)
                    save_subs(self.subs)
                    self.status = f"unfollowed {sub['title']}"
                else:
                    self._unfollow = sub["id"]
                    self.status = f"press x again to unfollow {sub['title']}"

    def resume_partial(self) -> None:
        n = self.scan_resume()
        self.status = (f"resumed {n} download{'' if n == 1 else 's'}" if n
                       else "nothing to resume on disk")
        if n:
            self.view = "downloads"

    def palette_actions(self) -> list[tuple[str, str, Callable[[], object]]]:
        """(label, key hint, run) for what makes sense right now. Context-sensitive actions
        replay the same keys a person would press, so the palette can never drift from them."""
        keys = lambda *ks: (lambda: [self.on_key(k) for k in ks])
        out: list[tuple[str, str, Callable[[], object]]] = []
        if self.detail is not None:
            out += [("Download", "d", keys("d")), ("Download to a folder", "D", keys("D")),
                    ("Look inside the torrent", "f", keys("f")),
                    ("All releases of this title (Torrentio)", "t", keys("t")),
                    ("Follow this show", "w", keys("w")),
                    ("Save the .torrent file", "e", keys("e")),
                    ("Open the page in a browser", "o", keys("o")), ("Copy the magnet", "y", keys("y")),
                    ("Back to results", "esc", keys("esc"))]
        elif self.view == "search" and self.search is not None:
            out += [("Show details", "enter", keys("enter")),
                    ("Look inside the torrent", "enter f", keys("enter", "f")),
                    ("Download selected (or all marked)", "d", keys("d")),
                    ("Download to a folder", "D", keys("D")), ("Save the .torrent file", "e", keys("e")),
                    ("Open the page in a browser", "o", keys("o")), ("Copy the magnet", "y", keys("y")),
                    ("Mark / unmark and step down", "space", keys(" ")), ("Mark all / none", "a", keys("a")),
                    ("Follow this show", "w", keys("w")), ("Filter these results", "f", keys("f")), ("Search again, skipping the cache", "R", keys("R")),
                    ("Retry failed sources", "r", keys("r")), ("Source health", "E", keys("E")),
                    ("Cycle sort order", "S", keys("S")), ("Hide / show dead torrents", "z", keys("z")),
                    ("Edit the query", "/", keys("/")), ("Clear results", "c", keys("c"))]
            out += [(f"Category: {label}", "← →", lambda k=key: (setattr(self, "cat", k), setattr(self, "sel", 0)))
                    for key, label in CATS]
        elif self.view == "downloads":
            out += [("Open finished download", "enter", keys("enter")), ("Pause / resume", "p", keys("p")),
                    ("Cancel download", "x", keys("x")), ("Retry failed download", "r", keys("r")),
                    ("Choose files", "f", keys("f")), ("Reveal in Finder", "o", keys("o"))]
        if self.detail is None:
            out.append(("Show downloads" if self.view == "search" else "Show search", "tab", keys("tab")))
        out += [("Paste magnet or link from clipboard", "v",
                 lambda: (setattr(self, "view", "search"), self.on_key("v"))),
                ("Resume partial downloads on disk", "s", self.resume_partial),
                ("More downloads at once", "+", lambda: self.set_concurrency((self.max_dl or 5) + 1)),
                ("Fewer downloads at once", "-", lambda: self.set_concurrency((self.max_dl or 5) - 1)),
                ("Settings", "g", lambda: (setattr(self, "settings", True),
                                            setattr(self, "set_sel", self._snap_setting(0)))),
                ("Followed shows", "W", self.open_following),
                ("Keyboard help", "?", lambda: setattr(self, "help", True)),
                ("Quit", "q", self._quit)]
        if self.subs:
            out.append(("Check followed shows now", "", lambda: self.start_check(True)))
        out += [(f"Theme: {t}", "", lambda t=t: (self._set_theme(t), self._save_settings()))
                for t in T.THEMES if t != self.theme]
        return out

    def palette_items(self) -> list[tuple[str, str, Callable[[], object]]]:
        acts = self.palette_actions()
        if not self.pal_buf.strip():
            return acts
        scored = [(sc, i, a) for i, a in enumerate(acts) if (sc := fuzzy_score(self.pal_buf, a[0])) is not None]
        return [a for _, _, a in sorted(scored, key=lambda x: (x[0], x[1]))]

    def _palette_key(self, k: str) -> None:
        items = self.palette_items()
        if k in ("esc", "ctrl-k"):
            self.palette = False
        elif k == "enter":
            self.palette = False
            if items:
                items[min(self.pal_sel, len(items) - 1)][2]()
        elif k in ("up", "down", "pageup", "pagedown"):
            step = {"up": -1, "down": 1, "pageup": -PALETTE_ROWS, "pagedown": PALETTE_ROWS}[k]
            self.pal_sel = max(0, min(self.pal_sel + step, len(items) - 1)) if items else 0
        elif k == "backspace":
            self.pal_buf, self.pal_sel = self.pal_buf[:-1], 0
        elif k == "ctrl-u":
            self.pal_buf, self.pal_sel = "", 0
        elif len(k) == 1 and k >= " ":
            self.pal_buf, self.pal_sel = self.pal_buf + k, 0

    def open_palette(self) -> None:
        self.palette, self.pal_buf, self.pal_sel = True, "", 0

    def _cycle_cat(self, d: int) -> None:
        i = next((k for k, (key, _) in enumerate(CATS) if key == self.cat), 0)
        self.cat = CATS[(i + d) % len(CATS)][0]
        self.sel = 0

    def on_key(self, k: str) -> None:
        if k == "ctrl-c":  # always, whatever is open
            self.running = False
            return
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
        if self.palette:
            self._palette_key(k)
            return
        if k == "ctrl-k" and not (self.torrent_prompt or self.cancel_prompt or self.folder_prompt
                                  or self.edit_field or self.picker or self.settings or self.following):
            self.open_palette()
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
        if self.following:
            self._following_key(k)
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
                if self._space_ok(self._variant().uri, self.detail.size):
                    self.grab(self._variant().uri, self.detail.name)
                    self.detail = None
                    self.variant_idx = 0
            elif k == "D":
                self._start_folder_prompt([(self._variant().uri, self.detail.name)], self.detail.size)
            elif k == "e":
                self.export_torrent(self._variant().uri, self.detail.name)
            elif k == "f":
                self.start_peek(self._variant().uri, self.detail.name)
            elif k == "t":
                self.start_releases()
            elif k == "w":
                self.follow_current()
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
        if self.view == "search" and self.filtering:
            self._filter_key(k)
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
                self._quit()
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
            self._quit()
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
            self.resume_partial()
        elif k == ":":
            self.open_palette()
        elif k == "W":
            self.open_following()
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
                if self.last_update or self.errors or self.paused_sources():
                    self.show_errors = not self.show_errors
                else:
                    self.status = "no search yet — nothing to show"
            elif k == "w":
                self.follow_current()
            elif k == "f":
                if self.search is not None:
                    self.filtering = True
            elif k == "R":
                self.fail_streak.clear()  # a fresh search also gives paused sources another chance
                self.submit(fresh=True)
            elif k == "esc":
                if self.show_errors:
                    self.show_errors = False
                elif self.marked:
                    self.marked.clear()
                else:
                    self._set_filter("")
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
            elif k == " ":  # mark/unmark for a batch grab, then step down like a file manager
                if (r := self._cur()):
                    self.marked ^= {self._mkey(r)}
                    self._move(1)
            elif k == "a":  # mark every visible row, or clear when they're all marked
                keys = {self._mkey(r) for r in self.visible_results()}
                self.marked = set() if keys <= self.marked else self.marked | keys
            elif k in ("d", "D"):
                rs = self._marked_results() or ([r] if (r := self._cur()) else [])
                items, size = [(r.magnet, r.name) for r in rs], sum(r.size for r in rs)
                if k == "D" and items:
                    self._start_folder_prompt(items, size)
                else:
                    self._grab_items(items, size)
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
            if k in ("+", "=", "-"):
                self.set_concurrency((self.max_dl or 5) + (-1 if k == "-" else 1))
            elif k == "enter":
                if d.status != "complete" or not d.path:
                    self.status = "not finished yet — o reveals the folder"
                elif open_url(open_target(d.path, d.name)):
                    self.status = f"opened: {clean(d.name)[:40]}"
                else:
                    self.status = "couldn't open — was it moved or deleted?"
            elif k == "x":
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
                if not d.path or d.meta:
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
    rel = parse_release(r.name)
    if rel.detail():
        inner.append(field("Format", rel.detail(), T.BAD if rel.bad else None))
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
            elif ch in T.CATCH_GLYPHS:
                seg += style(ch, T.GOOD, bold=True)
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
    keys = _rail_keys(app, h - len(lines))  # sits in the lower-left corner, not under the categories
    lines += [cell("", RAIL_W)] * (h - len(lines) - len(keys)) + keys
    return lines[:h]


def _rail_keys(app: App, room: int) -> list[str]:
    """Every shortcut for this screen as a vertical list (action on the left, key on the right),
    with the keys that work anywhere (commands, all keys, settings, quit) pinned at its end. If the
    window is short, the least-used screen keys go first; `all keys ?` still lists them."""
    hints, rail = _hints(app)
    tail = [h for h in hints if h[0] in RAIL_GLOBAL] if rail else []
    body = [h for h in hints if h[0] not in RAIL_GLOBAL] if rail else []
    fit = room - 2  # a blank line and the KEYS heading
    if not rail or fit < len(tail) + 1:
        app.rail_keys_shown = False
        return []
    keys = body[:fit - len(tail)] + tail
    kw = max(dwidth(k) for k, _ in keys)
    app.rail_keys_shown = True
    out = [cell("", RAIL_W), "  " + cell("KEYS", RAIL_W - 2, color=T.ALT, bold=True, dim=True)]
    for k, label in keys:  # indented under the category icons; key column ends where the counts do
        out.append("  " + cell(label, RAIL_W - 3 - kw, dim=True) + " " + cell(k, kw, "right", color=T.ACCENT))
    return out


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
    """Source health: failures (with the reason), paused sources, then the rest by speed."""
    inner_w = width - 4
    ups = dict(app.last_update)
    for sid, msg in app.errors.items():
        ups.setdefault(sid, SourceUpdate(sid, None, msg))
    failed = [u for u in ups.values() if u.results is None]
    paused = sorted(app.paused_sources() - set(ups))
    good = sorted((u for u in ups.values() if u.results is not None), key=lambda u: -u.elapsed)
    inner = [cell("r retries failed sources · R searches again and un-pauses paused ones", inner_w, dim=True),
             cell("", inner_w)]
    for u in failed:
        inner.append(cell(f" {T.ERR} {app.source_label(u.source)}  {u.elapsed:.1f}s", inner_w, color=T.BAD, bold=True))
        for ln in _wrap(clean(u.error) or "unknown error", inner_w - 4):
            inner.append("    " + cell(ln, inner_w - 4, dim=True))
    for sid in paused:
        inner.append(cell(f" {T.PAUSE} {app.source_label(sid)}  paused — failed {app.fail_streak[sid]} searches in a row",
                          inner_w, color=T.WARN))
    for u in good:
        inner.append(cell(f" {T.DONE} {app.source_label(u.source)}", inner_w - 22, color=T.GOOD)
                     + cell(f"{u.elapsed:.1f}s", 7, "right", dim=True)
                     + cell(f"{len(u.results or [])} results", 15, "right", dim=True))
    n = f"({len(failed)} failed · {len(paused)} paused)" if failed or paused else None
    return _wrap_panel("Sources", inner, width, height, True, n)


def _following_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    inner = [cell("checked every 4 hours · c checks now · a = auto-grab: download new episodes without asking",
                  inner_w, dim=True), cell("", inner_w)]
    if not app.subs:
        inner += [cell("Not following anything yet.", inner_w, color=T.ALT),
                  cell("Open a TV or anime episode and press w to follow its show.", inner_w, dim=True)]
    name_w = max(8, inner_w - 37)
    for i, sub in enumerate(app.subs):
        here = i == app.follow_sel
        new = app.sub_new.get(sub["id"]) or []
        ago = fmt_rel(sub["checked"]) if sub["checked"] else "unchecked"
        inner.append(cell(T.PTR if here else "", 2, color=T.ACCENT) + " "
                     + cell(sub["title"] + (f" {sub['res']}p" if sub["res"] else ""), name_w,
                            color=T.ACCENT if here else T.TEXT, bold=here) + " "
                     + cell(fmt_ep(sub["last"]), 7, dim=True) + " "
                     + cell(f"{len(new)} new" if new else "—", 8, "right",
                            color=T.GOOD if new else None, bold=bool(new), dim=not new) + " "
                     + cell("auto" if sub["auto"] else "", 6, color=T.WARN) + " "
                     + cell(ago, 9, "right", dim=True))
    sel = app.subs[app.follow_sel] if 0 <= app.follow_sel < len(app.subs) else None
    new = (app.sub_new.get(sel["id"]) or []) if sel else []
    if new:
        inner += [cell("", inner_w),
                  cell(f"New for {sel['title']} — enter shows them · g grabs them · m marks them seen",
                       inner_w, color=T.ALT, bold=True)]
        for r in new[:6]:
            tag, tcolor = app.source_tag(r.source)
            ep = episode_of(r.name)
            inner.append(cell(f"  {fmt_ep(ep) if ep else '':6}  {clean(r.name)}", inner_w - 15, color=T.GOOD)
                         + cell(fmt_bytes(r.size), 10, "right", dim=True)
                         + cell(tag, 5, "right", color=tcolor))
    return _wrap_panel("Following", inner, width, height, True, f"({len(app.subs)})" if app.subs else None)


def _search_panel(app: App, width: int) -> list[str]:
    editing = app.view == "search" and app.editing
    return _wrap_panel("Search", [_search_line(app, width - 4)], width, 3, editing)


def _status_line(app: App, results: Sequence[Result], inner_w: int) -> str:
    if app.filtering:
        return cell(f"filter: {clean(app.filter_buf)}▌   {len(results)} of {len(app.results)}"
                    "  —  enter keeps · esc clears", inner_w, color=T.ALT)
    if marked := app._marked_results():
        return cell(f"{len(marked)} marked · {fmt_bytes(sum(r.size for r in marked))}"
                    "  —  d grabs them · D to a folder · esc clears", inner_w, color=T.GOOD)
    if app.search and app.search_done < app.search_total:
        return cell(f"searching… {app.search_done}/{app.search_total} sources", inner_w, dim=True)
    errs = len(app.errors)
    paused = len(app.paused_sources())
    pause_note = f"  ({paused} paused — E)" if paused else ""
    if not results:
        if app.search is None:
            return cell("Type to search. Enter runs it; paste a magnet or link to grab it.", inner_w, dim=True)
        if app.filter_buf and app.results:
            return cell(f'Nothing matches "{clean(app.filter_buf)}" — esc clears the filter.', inner_w, color=T.WARN)
        if errs >= app.search_total:
            return cell("Couldn't reach any source — they may be down.", inner_w, color=T.WARN)
        q = clean(app.query)
        return cell((f'No results for "{q}".' if q else "Nothing new right now.") + pause_note,
                    inner_w, dim=True)
    note = (f"  ({errs} source{'' if errs == 1 else 's'} down)" if errs else "") + pause_note
    head = "popular now" if not app.query.strip() else f"{len(results)} result{'' if len(results) == 1 else 's'}"
    if app.filter_buf:
        head = f'{len(results)} of {len(app.results)} match "{clean(app.filter_buf)}"'
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


_BADGE_W = 15      # "2160p REMUX HDR"
_BADGE_MIN_W = 76  # inner panel columns (≈103-col terminal) before the badge column appears


def _results_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    results = app.visible_results()
    app.sel = min(app.sel, max(0, len(results) - 1))
    badge_w = _BADGE_W + 1 if inner_w >= _BADGE_MIN_W else 0  # narrow panels keep the old layout
    name_w = max(8, inner_w - 29 - badge_w)  # ptr2 + name + 9 + 9 + 5 + 4 seps
    inner: list[str] = [_status_line(app, results, inner_w)]
    if results:
        header = (cell("", 2) + " " + cell("Name", name_w, dim=True, bold=True) + " "
                  + (cell("Release", _BADGE_W, dim=True, bold=True) + " " if badge_w else "")
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
            rel = parse_release(r.name)
            mark = cell(T.DONE, 1, color=T.GOOD, bold=True) if app.marked and app._mkey(r) in app.marked else " "
            bcell = ""
            if badge_w:
                bcolor = T.BAD if rel.bad else T.BRIGHT if (rel.res >= 2160 or rel.hdr) else None
                bcell = cell(rel.badge(), _BADGE_W, color=bcolor or (T.ACCENT if here else None),
                             bold=here or rel.bad, dim=bcolor is None and not here) + " "
            if here:  # selected row: the whole line lights up in accent
                inner.append(
                    cell(T.PTR, 1, color=T.ACCENT) + mark + " "
                    + cell(clean(r.name), name_w, color=T.ACCENT, bold=True) + " " + bcell
                    + cell(fmt_bytes(r.size), 9, "right", color=T.ACCENT, bold=True) + " "
                    + cell(sl, 9, "right", color=T.ACCENT, bold=True) + " "
                    + cell(tag, 5, "right", color=T.ACCENT, bold=True))
            else:
                inner.append(
                    " " + mark + " "
                    + cell(clean(r.name), name_w, color=T.TEXT) + " " + bcell
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
            elif d.status == "waiting":  # queued behind the downloads-at-once limit
                icon, ic, base = "…", T.RULE, T.RULE
                stats = f"queued  {pct}%" if d.progress else "queued"
            else:  # active
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
    active = sum(1 for d in live if d.status in ("active", "metadata"))
    queued = sum(1 for d in live if d.status == "waiting")
    title_count = f"({len(live)})" if live else None
    if title_count:
        title_count += (f" · {active} active" if active else "") + (f" · {queued} queued" if queued else "") \
            + (f" · {app.max_dl} at once" if app.max_dl and (active or queued) else "")
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
        if kind in ("dir", "limit", "provider", "meta-key", "theme", "updates", "concurrency", "awake"):
            if kind == "awake":
                label = "Keep Mac awake"
                shown = {"on": "while downloading", "ac": "while downloading, on the charger",
                         "off": "off (the Mac may sleep mid-download)"}[app.keep_awake]
                on = app.keep_awake != "off"
            elif kind == "concurrency":
                label = "Downloads at once"
                shown = (app.edit_buf + "▌" if app.edit_field == "concurrency"
                         else f"{app.max_dl or '?'}  ← →" + ("" if app.max_dl_set else "  (from aria2.conf)"))
                on = True
            elif kind == "updates":
                label = "Update check"
                shown = "daily (GitHub)" if app.update_check else "off"
                on = app.update_check
            elif kind == "dir":
                label = "Download dir"
                shown = app.edit_buf + "▌" if app.edit_field == "dir" else app.download_dir or "(from aria2.conf)"
                on = bool(app.download_dir or app.edit_field == "dir")
            elif kind == "limit":
                label = "Speed limit"
                shown = app.edit_buf + "▌" if app.edit_field == "limit" else app.speed_limit or "(unlimited)"
                on = bool(app.speed_limit or app.edit_field == "limit")
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
        ("Search", [("type", "search (paste a magnet, infohash or link; drop a .torrent)"), ("enter", "details"),
                     ("space", "mark a result, step down (a all/none, esc clear)"),
                     ("d / D", "download / to a folder: all marked, else the selected"),
                     ("e", "save .torrent"),
                     ("o", "open page in browser"), ("y", "copy magnet"),
                     ("/  i", "edit query"), ("↑ ↓", "recall past searches"),
                     ("f  R", "filter results live · search again, skipping the cache"),
                     ("r", "retry failed sources"), ("E", "source health: speed, failures, paused"),
                     ("z", "hide dead torrents"),
                     ("filters", "seeders: size: age: files: source: group: res: codec:"),
                     ("examples", 'matrix -cam size:>1GiB group:movies'),
                     ("S", "cycle sort (seeders/size/newest)"), ("c", "clear results"),
                     ("← →", "filter category"), ("v", "grab magnet/link from clipboard")]),
        ("Details", [("← →", "cycle duplicate source variants"),
                     ("d / D / e", "download / to folder / save .torrent"),
                     ("f", "look inside; tick files, enter downloads just those"),
                     ("t", "every known release of this title (asks Torrentio)"),
                     ("w", "follow this show (TV/anime); W lists followed shows")]),
        ("Settings", [("enter / space", "edit or toggle the selected row"),
                       ("a", "add Torznab feed"),
                       ("on a feed row", "e endpoint · K separate key · x remove"),
                       ("g / esc", "close")]),
        ("Navigate", [("ctrl-k  :", "command palette: every action, searchable"),
                      ("↑ ↓  j k", "move selection (keyboard only; the mouse is ignored)"),
                      ("tab", "switch search / downloads")]),
        ("Downloads", [("enter", "open a finished download"), ("+ / -", "more / fewer downloads at once"),
                       ("☕", "the Mac is kept awake while downloading (settings: Keep Mac awake)"),
                       ("p", "pause / resume"), ("x", "cancel (ask: delete or keep files)"),
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


RAIL_GLOBAL = (":", "?", "g", "q", "^c")  # keys that work anywhere: pinned at the end of the rail list


def _hints(app: App) -> tuple[list[tuple[str, str]], bool]:
    """(key, action) shortcuts for what's on screen, most useful first. The bool says whether this
    is a screen with the left rail, whose KEYS list then carries everything but RAIL_GLOBAL."""
    rail = False
    if app.palette:
        hints = [("type", "to find"), ("↑↓", "move"), ("↵", "run"), ("esc", "close"), ("^c", "quit")]
    elif app.cancel_prompt is not None:
        hints = [("d", "delete files"), ("k", "keep files"), ("esc", "abort")]
    elif app.folder_prompt is not None:
        hints = [("type", "path"), ("enter", "download"), ("esc", "cancel")]
    elif app.torrent_prompt is not None:
        hints = [("t", "contents"), ("f", ".torrent file"), ("esc", "cancel")]
    elif app.help:
        hints = [("↑↓", "scroll"), ("any key", "close")]
    elif app.following:
        hints = [("↑↓", "move"), ("↵", "new episodes"), ("g", "grab new"), ("m", "mark seen"),
                 ("a", "auto-grab"), ("c", "check now"), ("x", "unfollow"), ("esc", "back")]
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
            elif kind == "concurrency":
                hints = [("↑↓", "move"), ("←→", "change"), ("enter", "type a number"), ("g/esc", "close")]
            elif kind in ("dir", "meta-key", "limit"):
                hints = [("↑↓", "move"), ("enter/space", "edit"), ("a", "add"), ("g/esc", "close")]
            else:  # provider / theme
                hints = [("↑↓", "move"), ("enter/space", "toggle"), ("a", "add"), ("g/esc", "close")]
    elif app.picker is not None:
        hints = [("↑↓", "move"), ("space", "toggle"), ("a", "all/none"),
                 ("enter", "download ticked" if app.peek_target else "apply"), ("esc", "cancel")]
    elif app.detail is not None:
        hints = [("←→", "variant"), ("d", "download"), ("D", "folder"), ("e", ".torrent"), ("f", "files"),
                 ("t", "releases"),
                 ("o", "page"), ("y", "copy"), ("p", "poster"), ("esc/q", "back")]
    elif app.show_errors:
        rail, hints = True, [("esc/E", "close"), ("r", "retry"), ("R", "re-search"), ("q", "quit")]
    elif app.view == "search" and app.filtering:
        rail, hints = True, [("type", "filter"), ("↑↓", "move"), ("enter", "keep"), ("esc", "clear"), ("^c", "quit")]
    elif app.view == "search" and app.editing:
        rail, hints = True, [("enter", "search"), ("↑↓", "history"), ("esc", "leave box"), ("tab", "downloads"),
                             ("^c", "quit")]
    elif app.view == "search":
        n = len(app._marked_results())
        rail, hints = True, [("↑↓", "move"), ("enter", "details"), ("space", "mark"),
                             ("d", f"grab {n}" if n else "grab"), ("D", "folder"), ("f", "filter"), ("w", "follow"),
                             ("S", "sort"), ("←→", "category"), ("z", "hide dead"), ("e", ".torrent"),
                             ("o", "page"), ("y", "copy"), ("v", "paste"), ("r", "retry"), ("E", "sources"),
                             ("R", "re-search"), (":", "commands"), ("?", "all keys"), ("g", "settings"),
                             ("q", "quit")]
    else:
        rail, hints = True, [("↑↓", "move"), ("↵", "open"), ("p", "pause/play"), ("x", "cancel"), ("r", "retry"),
                             ("+/-", "at once"), ("f", "files"), ("o", "reveal"), ("s", "resume"),
                             ("tab", "search"), (":", "commands"), ("?", "all keys"), ("g", "settings"),
                             ("q", "quit")]
    return hints, rail


def _footer(app: App, width: int, rail_keys: bool = False) -> str:
    """The bottom line: empty while the rail lists this screen's keys, else every shortcut."""
    hints, rail = _hints(app)
    if (rail_keys and rail) or not hints:
        return ""
    out, used = "", 0
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
    if app.update_tag:
        note = dtrunc(_update_note(app), cols - 4)
        block += ["", _center(style(note, T.WARN), dwidth(note), cols)]
    top = max(0, (rows - len(block)) // 2)
    lines = ([""] * top + block + [""] * rows)[:rows]
    bar, bar_w = _status_bar(app, cols - 2 * MARGIN)
    if bar and not lines[0].strip():  # top-right corner, same place as every other screen
        lines[0] = " " * (cols - MARGIN - bar_w) + bar
    return lines

def _modal_box(cols: int, label: str, hints: list[tuple[str, str]], color: str) -> list[str]:
    # narrow terminals: shorten the label, then the hints ("the .torrent" -> "the"), then keys only
    for hs in (hints, [(k, v.split()[0]) for k, v in hints], [(k, "") for k, _ in hints]):
        hint_w = dwidth("  ·  ".join(f"{k} {v}".strip() for k, v in hs))
        if hint_w + 7 + 6 + 4 <= cols or not hs[0][1]:
            break
    hints = hs
    label = dtrunc(label, max(6, cols - 4 - 7 - hint_w))
    plain = "  " + label + "   " + "  ·  ".join(f"{k} {v}".strip() for k, v in hints) + "  "
    box_w = dwidth(plain) + 2
    pre = " " * max(0, (cols - box_w) // 2)
    inner = "  " + style(label, color, bold=True) + "   "
    for i, (k, v) in enumerate(hints):
        if i:
            inner += style("  ·  ", dim=True)
        inner += style(k, T.ACCENT) + (style(" " + v, dim=True) if v else "")
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
    _, name, _ = app.folder_prompt or ([], "", 0)
    label = f"Download to folder — {clean(name)}"
    box_w = min(max(dwidth(label) + 6, 30), cols - 2)
    pre = " " * max(0, (cols - box_w) // 2)
    inner_w = box_w - 4
    buf, room = clean(app.folder_buf), max(1, inner_w - 7)  # "path: " + the caret
    while dwidth(buf) > room:  # keep the end of a long path: that's the part being typed
        buf = "…" + buf[2:] if buf.startswith("…") else "…" + buf[1:]
    path = style("path: ", dim=True) + style(buf, T.TEXT) + "\x1b[7m \x1b[0m"
    hint = style("enter", T.ACCENT) + style(" download  ", dim=True) \
        + style("esc", T.ACCENT) + style(" cancel", dim=True)
    def row(s: str) -> str:
        return pre + style("│", T.ACCENT) + " " + s + " " * max(0, inner_w - dwidth(strip_ansi(s))) + " " + style("│", T.ACCENT)
    return [
        pre + style("╭" + "─" * (box_w - 2) + "╮", T.ACCENT),
        row(style(dtrunc(label, inner_w), T.TEXT, bold=True)), row(""), row(path), row(""), row(hint),
        pre + style("╰" + "─" * (box_w - 2) + "╯", T.ACCENT),
    ]


def _palette_box(app: App, cols: int, rows: int) -> list[str]:
    items = app.palette_items()
    app.pal_sel = sel = min(app.pal_sel, max(0, len(items) - 1))
    box_w = min(max(64, cols // 2), cols - 4)
    inner_w = box_w - 4
    pre = " " * ((cols - box_w) // 2)
    n = min(PALETTE_ROWS, max(3, rows - 10))
    start = _window(sel, len(items), n)
    c = T.ACCENT
    edge = lambda body: pre + style("│", c) + " " + body + " " + style("│", c)
    head = "╭─ Commands "
    out = [pre + style(head, c, bold=True) + style("─" * (box_w - dwidth(head) - 1) + "╮", c),
           edge(cell("› " + clean(app.pal_buf) + "▌", inner_w, color=T.TEXT)),
           edge(style("─" * inner_w, T.RULE))]
    for i in range(start, start + n):
        if i < len(items):
            label, hint, _ = items[i]
            here = i == sel
            out.append(edge(cell(T.PTR if here else "", 2, color=c)
                            + cell(label, inner_w - 14, color=c if here else None, bold=here)
                            + cell(hint, 12, "right", dim=True)))
        else:
            out.append(edge(cell("No matching command" if not items and i == start else "", inner_w, dim=True)))
    out.append(pre + style("╰" + "─" * (box_w - 2) + "╯", c))
    return out


def _overlay(lines: list[str], app: App, cols: int, rows: int) -> list[str]:
    if app.palette:
        for j, b in enumerate(_palette_box(app, cols, rows)):
            if 3 + j < len(lines):  # row 2 is the rule with the status bar: keep it visible
                lines[3 + j] = b
        return lines
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


def _status_bar(app: App, budget: int) -> tuple[str, int]:
    """The top-right indicator: [fading message]  [spinner + what's running]  [download speed].
    Returns (styled text, display width) squeezed into `budget` columns. The message matters
    most (it may be a warning that needs a key press), so the speed is dropped first, then the
    activity text is shortened, and only then does the message give way."""
    sep = "   "
    live = app.status_live()
    act = app.activity()
    spin = T.SPIN[int(time.monotonic() * 12) % len(T.SPIN)]
    item = {"msg": dtrunc(redact(clean(live[0]), app._all_secrets()), 200) if live else "",
            "act": f"{spin} " + " · ".join(act) if act else "",
            "stat": (f"{T.DOWN} {fmt_speed(app.down_speed)} · {app.num_active} active"
                     if app.down_speed > 0 or app.num_active > 0 else "")
            + (" · ☕" if app.awake and (app.down_speed > 0 or app.num_active > 0) else "☕" if app.awake else "")}

    def width() -> int:
        shown = [v for v in item.values() if v]
        return sum(dwidth(v) for v in shown) + len(sep) * max(0, len(shown) - 1)

    while width() > budget:
        over = width() - budget
        if item["stat"]:
            item["stat"] = ""
        elif dwidth(item["act"]) > 4:  # shrink toward just the spinner; the message is worth more
            item["act"] = dtrunc(item["act"], max(4, dwidth(item["act"]) - over))
        elif dwidth(item["msg"]) > 12:
            item["msg"] = dtrunc(item["msg"], max(12, dwidth(item["msg"]) - over))
        elif item["msg"]:
            item["msg"] = ""
        elif item["act"]:
            item["act"] = dtrunc(item["act"], budget)
        else:
            break
    color = {"msg": live[1] if live else T.ALT, "act": T.ACCENT, "stat": T.ALT}
    parts = [style(v, color[k], bold=k == "act") for k, v in item.items() if v]
    return sep.join(parts), width() if parts else 0


def _update_note(app: App) -> str:
    return f"trawl {app.update_tag} available · brew upgrade trawl"


def render(app: App, cols: int, rows: int) -> list[str]:
    cols = max(40, cols)
    rows = max(12, rows)
    if app.view == "search" and app.search is None and not app.help and not app.settings and not app.following:
        return [clip(x, cols) for x in _redact_frame(_overlay(_splash(app, cols, rows), app, cols, rows), app)]
    lines: list[str] = []
    for L in _logo_lines():
        lines.append(" " * MARGIN + L)
    rule_w = max(0, cols - 2 * MARGIN)
    notes = ([f"{app.new_count()} new episode(s) — W"] if app.new_count() else []) \
        + ([_update_note(app)] if app.update_tag else [])
    note = f" {dtrunc('  ·  '.join(notes), max(0, rule_w - 6))} " if notes else ""
    bar, bar_w = _status_bar(app, max(0, rule_w - dwidth(note) - 8))
    if bar or note:
        dashes = max(0, rule_w - dwidth(note) - (bar_w + 2 if bar else 0) - 2)
        lines.append(" " * MARGIN + style("─", T.RULE) + style(note, T.WARN) + style("─" * dashes, T.RULE)
                     + (" " + bar + " " if bar else "") + style("─", T.RULE))
    else:
        lines.append(" " * MARGIN + style("─" * rule_w, T.RULE))

    body_h, panel_h = _main_heights(rows)
    use_rail = app.view in ("search", "downloads") and app.detail is None and not app.help \
        and not app.settings and not app.following and app.picker is None and cols >= RAIL_MIN_COLS
    content_w = cols - MARGIN - RAIL_W - GAP - 1 if use_rail else cols - MARGIN - 1

    if app.help:
        content = _help_panel(app, content_w, body_h)
    elif app.settings:
        content = _settings_panel(app, content_w, body_h)
    elif app.following:
        content = _following_panel(app, content_w, body_h)
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
    app.rail_keys_shown = False
    rail = _rail(app, body_h) if use_rail else []

    for i in range(body_h):
        lines.append(" " * MARGIN + (rail[i] + " " * GAP if use_rail else "") + content[i])

    lines.append("")
    lines.append(" " * MARGIN + _footer(app, cols - MARGIN, rail_keys=use_rail and app.rail_keys_shown))
    lines = (lines + [""] * rows)[:rows]
    return [clip(x, cols) for x in _redact_frame(_overlay(lines, app, cols, rows), app)]

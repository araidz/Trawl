"""Raw-ANSI terminal UI: state, render, input. No third-party deps.

Full-redraw renderer (truecolor) in torlink's look — logo header, left rail,
search/results/downloads panels, sheen progress bars, footer hints. The run
loop lives in __main__.py; this module is the view + state and is importable
without a terminal or aria2 (so render/keys are testable).
"""

from __future__ import annotations

import glob
import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
import unicodedata

from . import theme as T
from .aria2 import STATE_DIR, Aria2Error, Download, control_infohash
from .sources import (SOURCES, Result, Search, build_magnet, dedupe, parse_source)

GROUP_OF = {s.id: s.group for s in SOURCES}
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


def render_bar(progress: float, width: int, tick: float, animate: bool,
               base: str = T.ACCENT) -> str:
    if width <= 0:
        return ""
    filled = round(max(0.0, min(1.0, progress)) * width)
    empty = width - filled
    denom = max(1, width - 1)
    period = T.sheen_period(width)
    center = T.sheen_center(tick, period)
    cells = []
    for i in range(filled):
        c = T.progress_ramp(i / denom, T.DEEP, base, T.BRIGHT)
        if animate:
            inten = T.sheen_intensity(i, center)
            if inten > 0:
                c = T.lerp_hex(c, T.SHEEN_PEAK, inten)
        cells.append(c)
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

    def enter(self) -> None:
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        # alt-screen + clear + SGR mouse reporting; trawl owns the whole tab
        sys.stdout.write("\x1b[?1049h\x1b[3J\x1b[2J\x1b[H\x1b[?25l\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()

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

    def write(self, lines: list[str]) -> None:
        buf = ["\x1b[H"]
        for i, ln in enumerate(lines):
            buf.append(ln + "\x1b[K")
            if i < len(lines) - 1:
                buf.append("\r\n")
        buf.append("\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()


# -- app state ---------------------------------------------------------------


class App:
    def __init__(self, eng=None):
        self.eng = eng
        self.view = "search"  # search | downloads
        self.editing = False  # splash is a landing (q quits); typing/"/" starts editing
        self.query = ""
        self.history = load_history()  # past queries, oldest -> newest
        self.hist_idx = len(self.history)  # cursor; == len means "live draft"
        self.draft = ""  # query in progress before browsing history
        self.results: list[Result] = []
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
        self.detail: Result | None = None  # search result shown in the details view
        cfg = load_config()
        self.disabled_sources: set[str] = set(cfg.get("disabled_sources", []))
        self.download_dir: str | None = cfg.get("download_dir")
        self.dl_history: list[dict] = load_dl_history()  # completed downloads, oldest->newest
        self.settings = False  # settings overlay open
        self.set_sel = 0  # 0 = download-dir row, 1.. = sources
        self.dir_editing = False
        self.dir_buf = ""
        self.start = time.monotonic()

    # -- derived
    def visible_results(self) -> list[Result]:
        if self.cat == "all":
            return self.results
        g = CAT_GROUP[self.cat]
        return [r for r in self.results if (r.group or GROUP_OF.get(r.source)) == g]

    def animating(self) -> bool:
        return any(d.status in ("active", "metadata") for d in self.downloads)

    def enabled_sources(self) -> list:
        return [s for s in SOURCES if s.id not in self.disabled_sources]

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
            self.editing = False
            return
        srcs = self.enabled_sources()
        self.search = Search(q, srcs)
        self.search_total = len(srcs)
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.editing = False
        self.detail = None
        if q:
            self._add_history(q)
        self.status = f'searching "{clean(q)}"' if q else "loading latest"

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

    def clear(self) -> None:
        """Drop search results and return to the splash."""
        self.search = None
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.query = ""
        self.editing = False
        self.detail = None
        self.status = ""

    def grab(self, magnet: str, name: str) -> None:
        if not self.eng:
            self.status = f"(no engine) {clean(name)[:48]}"
            return
        try:
            self.eng.add(magnet)
            self.status = f"grabbing: {clean(name)[:48]}"
        except Aria2Error as e:
            self.status = f"error: {e}"

    def _grab_source(self, pm) -> None:
        """Grab a parsed input; a .torrent link first asks file-vs-contents."""
        if pm.kind == "torrent":
            self.torrent_prompt = pm
        else:
            self.grab(pm.magnet, pm.name)

    def grab_torrent(self, url: str, name: str, contents: bool) -> None:
        """A .torrent link: follow-torrent=mem grabs its contents; =false saves
        just the .torrent file. aria2 handles both natively."""
        if not self.eng:
            self.status = f"(no engine) {clean(name)[:48]}"
            return
        try:
            self.eng.add(url, {"follow-torrent": "mem" if contents else "false"})
            self.status = (f"grabbing torrent: {clean(name)[:40]}" if contents
                           else f"downloading .torrent file: {clean(name)[:40]}")
            self.view = "downloads"
        except Aria2Error as e:
            self.status = f"error: {e}"

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
        save_config({"disabled_sources": sorted(self.disabled_sources),
                     "download_dir": self.download_dir})

    def _settings_key(self, k: str) -> None:
        rows = 1 + len(SOURCES)  # row 0 = download dir, 1.. = sources
        if self.dir_editing:
            if k == "enter":
                self.download_dir = self.dir_buf.strip() or None
                if self.eng and self.download_dir:
                    self.eng.set_dir(self.download_dir)
                self._save_settings()
                self.dir_editing = False
            elif k == "esc":
                self.dir_editing = False
            elif k == "backspace":
                self.dir_buf = self.dir_buf[:-1]
            elif len(k) == 1 and k >= " ":
                self.dir_buf += k
            return
        if k in ("g", "esc", "q"):
            self.settings = False
        elif k in ("up", "k"):
            self.set_sel = (self.set_sel - 1) % rows
        elif k in ("down", "j"):
            self.set_sel = (self.set_sel + 1) % rows
        elif k in ("enter", " "):
            if self.set_sel == 0:
                self.dir_editing = True
                self.dir_buf = self.download_dir or (self.eng.download_dir() if self.eng else "") or ""
            else:
                sid = SOURCES[self.set_sel - 1].id
                self.disabled_sources.symmetric_difference_update({sid})
                self._save_settings()

    def drain_search(self) -> None:
        if not self.search:
            return
        changed = False
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
                self.results.extend(u.results)
        if changed:
            self.results = self._apply_sort(dedupe(self.results))
            self.sel = min(self.sel, max(0, len(self.visible_results()) - 1))

    def _apply_sort(self, results: list[Result]) -> list[Result]:
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
        if self.settings:
            self._settings_key(k)
            return
        if self.detail is not None:
            if k in ("esc", "enter", "q"):
                self.detail = None
            elif k == "d":
                self.grab(self.detail.magnet, self.detail.name)
                self.detail = None
            elif k == "o":
                page = self.detail.page
                self.status = ("opened in browser" if page and open_url(page)
                               else "no page for this source" if not page
                               else "couldn't open browser")
            elif k == "y":
                self.status = ("magnet copied to clipboard"
                               if copy_clipboard(self.detail.magnet) else "copy failed")
            return
        if self.view == "search" and self.editing:
            if k == "enter":
                self.submit()
            elif k == "esc":
                self.editing = False
            elif k == "backspace":
                self.query = self.query[:-1]
                self.hist_idx = len(self.history)
            elif k == "tab":
                self.view, self.editing = "downloads", False
            elif k == "up":
                self._hist(-1)
            elif k == "down":
                self._hist(1)
            elif len(k) == 1 and k >= " ":
                self.query += k
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
                self.query += k
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
            elif k == "d":
                rs = self.visible_results()
                if rs and 0 <= self.sel < len(rs):
                    self.grab(rs[self.sel].magnet, rs[self.sel].name)
            elif k == "enter":
                rs = self.visible_results()
                if rs and 0 <= self.sel < len(rs):
                    self.detail = rs[self.sel]
            elif k == "y":
                rs = self.visible_results()
                if rs and 0 <= self.sel < len(rs):
                    ok = copy_clipboard(rs[self.sel].magnet)
                    self.status = "magnet copied to clipboard" if ok else "copy failed"
            elif k == "o":
                rs = self.visible_results()
                if rs and 0 <= self.sel < len(rs):
                    page = rs[self.sel].page
                    if not page:
                        self.status = "no page for this source"
                    elif open_url(page):
                        self.status = "opened in browser"
                    else:
                        self.status = "couldn't open browser"
        elif self.view == "downloads":
            if not self.downloads or not (0 <= self.dsel < len(self.downloads)):
                return
            d = self.downloads[self.dsel]
            if k == "x":
                if self.eng:
                    self.eng.remove(d.root)
                self.status = f"cancelled: {clean(d.name)[:40]}"
                self.dsel = max(0, self.dsel - 1)
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


def _detail_panel(r: Result, width: int, height: int) -> list[str]:
    inner_w = width - 4
    inner = [cell(line, inner_w, color=T.ACCENT, bold=True) for line in _wrap(clean(r.name), inner_w)]
    inner.append(cell("", inner_w))

    def field(label: str, value: str, color: str | None = None) -> str:
        return "  " + cell(label, 7, dim=True) + cell(value, inner_w - 9, color=color)

    tag, _ = T.source_style(r.source)
    health = (f"{r.seeders} seeders · {r.leechers} leechers"
              if (r.seeders or r.leechers) else "unknown")
    inner.append(field("Source", tag))
    inner.append(field("Size", fmt_bytes(r.size)))
    inner.append(field("Health", health, T.GOOD if r.seeders else None))
    if r.added:
        inner.append(field("Added", fmt_rel(r.added)))
    if r.num_files:
        inner.append(field("Files", str(r.num_files)))
    inner.append(field("Hash", r.info_hash))
    if r.page:
        inner.append(field("Page", r.page))
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


def _logo_lines() -> list[str]:
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
    return out


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


def _search_line(app: App, inner_w: int) -> str:
    editing = app.view == "search" and app.editing
    prompt = style(T.PTR + " ", T.ACCENT)
    if app.query:
        text = dtrunc(app.query, inner_w - 3)
        cur = style("█", T.ACCENT) if editing else ""
        line = prompt + style(text, T.TEXT) + cur
        return line + " " * max(0, inner_w - 2 - dwidth(text) - (1 if editing else 0))
    ph = dtrunc("Search, or paste a magnet or link…", inner_w - 2)
    return prompt + style(ph, dim=True) + " " * max(0, inner_w - 2 - dwidth(ph))


def _search_panel(app: App, width: int) -> list[str]:
    editing = app.view == "search" and app.editing
    return _wrap_panel("Search", [_search_line(app, width - 4)], width, 3, editing)


def _status_line(app: App, results: list[Result], inner_w: int) -> str:
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
    head = "newest across all sources" if not app.query.strip() else f"{len(results)} result{'' if len(results) == 1 else 's'}"
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
            tag, tcolor = T.source_style(r.source)
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


def _downloads_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    body_h = height - 2
    live = app.downloads
    here_names = {d.name for d in live}
    # Recently = past-session completions not currently in the live list (no dupes)
    recent = [r for r in reversed(app.dl_history) if r.get("name") not in here_names]
    inner: list[str] = []
    if not live and not recent:
        inner.append(cell("No downloads yet. Find something and press d to grab it.", inner_w, dim=True))
        inner.append(cell("Press s to resume partial downloads on disk.", inner_w, dim=True))
        return _wrap_panel("Downloads", inner, width, height, app.view == "downloads")
    app.dsel = min(app.dsel, max(0, len(live) - 1))
    reserve = 2 if recent else 0
    max_items = max(0, (body_h - reserve) // 3)
    if live and max_items:
        list_h = min(len(live), max(1, max_items))
        start = _window(app.dsel, len(live), list_h)
        for idx in range(start, min(start + list_h, len(live))):
            d = live[idx]
            here = idx == app.dsel
            pct = int(d.progress * 100)
            if d.status == "error":
                icon, ic, base = T.ERR, T.BAD, T.BAD
                stats = dtrunc(d.error or "failed", 28)
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
    sel0 = app.set_sel == 0
    val = (app.dir_buf + "▌") if app.dir_editing else (app.download_dir or "(from aria2.conf)")
    inner = [
        cell("Download dir", inner_w, color=T.ALT, bold=True),
        cell(T.PTR if sel0 else "", 2, color=T.ACCENT)
        + cell(val, inner_w - 2, color=T.TEXT if (app.download_dir or app.dir_editing) else None,
               dim=not (app.download_dir or app.dir_editing)),
        cell("", inner_w),
        cell("Sources  (enter / space toggles)", inner_w, color=T.ALT, bold=True),
    ]
    for i, s in enumerate(SOURCES):
        sel = app.set_sel == i + 1
        on = s.id not in app.disabled_sources
        inner.append(cell(T.PTR if sel else "", 2, color=T.ACCENT)
                     + cell("[x]" if on else "[ ]", 4, color=T.GOOD if on else T.RULE)
                     + cell(f"{s.label}  ·  {s.group}", inner_w - 6,
                            color=T.ACCENT if sel else None, bold=sel, dim=not sel and not on))
    return _wrap_panel("Settings", inner, width, height, True)


def _help_panel(width: int, height: int) -> list[str]:
    inner_w = width - 4
    groups = [
        ("Search", [("type", "search (paste a magnet or link to grab)"), ("enter", "details"),
                    ("d", "download"), ("o", "open page in browser"), ("y", "copy magnet"),
                    ("/  i", "edit query"), ("↑ ↓", "recall past searches"),
                    ("S", "cycle sort (seeders/size/newest)"), ("c", "clear results"),
                    ("← →", "filter category"), ("v", "grab magnet/link from clipboard")]),
        ("Navigate", [("↑ ↓  j k", "move selection / scroll wheel"),
                      ("tab", "switch search / downloads")]),
        ("Downloads", [("p", "pause / resume"), ("x", "cancel / remove"),
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
    if app.torrent_prompt is not None:
        hints = [("t", "contents"), ("f", ".torrent file"), ("esc", "cancel")]
    elif app.help:
        hints = [("any key", "close")]
    elif app.settings:
        hints = ([("type", "path"), ("enter", "save"), ("esc", "cancel")] if app.dir_editing
                 else [("↑↓", "move"), ("enter/space", "toggle/edit"), ("g/esc", "close")])
    elif app.detail is not None:
        hints = [("d", "download"), ("o", "page"), ("y", "copy magnet"), ("esc", "back"), ("q", "back")]
    elif app.view == "search" and app.editing:
        hints = [("enter", "search"), ("↑↓", "history"), ("esc", "nav"), ("tab", "downloads"), ("^c", "quit")]
    elif app.view == "search":
        hints = [("↑↓", "move"), ("enter", "details"), ("d", "grab"), ("o", "page"), ("y", "copy"),
                 ("S", "sort"), ("←→", "category"), ("v", "paste"), ("g", "settings"), ("q", "quit")]
    else:
        hints = [("↑↓", "move"), ("p", "pause/resume"), ("x", "cancel"), ("o", "reveal"),
                 ("s", "resume"), ("g", "settings"), ("tab", "search"), ("q", "quit")]
    out, used = "", 0
    if app.status:
        st = dtrunc(clean(app.status), max(10, width // 2))
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
        st = dtrunc(clean(app.status), cols - 4)
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


def _overlay(lines: list[str], app: App, cols: int, rows: int) -> list[str]:
    box = _confirm(cols) if app.confirm_quit else _torrent_box(cols) if app.torrent_prompt else None
    if not box:
        return lines
    mid = max(0, rows // 2 - 1)
    for j, b in enumerate(box):
        if mid + j < len(lines):
            lines[mid + j] = b
    return lines


def render(app: App, cols: int, rows: int) -> list[str]:
    cols = max(40, cols)
    rows = max(12, rows)
    if app.view == "search" and app.search is None and not app.help and not app.settings:
        return _overlay(_splash(app, cols, rows), app, cols, rows)
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

    header_h = len(T.LOGO_LINES) + 1
    footer_h = 2
    body_h = rows - header_h - footer_h
    content_w = cols - MARGIN - RAIL_W - GAP - 1

    if app.help:
        content = _help_panel(content_w, body_h)
        rail = [cell("", RAIL_W)] * body_h
    elif app.settings:
        content = _settings_panel(app, content_w, body_h)
        rail = [cell("", RAIL_W)] * body_h
    else:
        rail = _rail(app, body_h)
        search_h = 3
        content = _search_panel(app, content_w) + [""]
        panel_h = body_h - search_h - 1
        if app.view == "search" and app.detail is not None:
            content += _detail_panel(app.detail, content_w, panel_h)
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
    return _overlay(lines, app, cols, rows)


# -- self-check --------------------------------------------------------------


def selftest() -> None:
    # width primitives
    assert dwidth("abc") == 3 and dwidth("日本") == 4, "east-asian width"
    assert dwidth(strip_ansi(cell("hi", 10))) == 10, "cell pads to width"
    assert dtrunc("hello world", 5) == "hell…", dtrunc("hello world", 5)
    assert strip_ansi(render_bar(0.5, 10, 0, False)).count("█") == 5, "bar half full"

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
    assert app.search is None and app.results == [] and not app.editing, "c clears to splash"
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

    appd = App(eng=_FakeEng())
    appd.view = "downloads"
    appd.downloads = [Download("g", "F", "active", 100, 10, 5, 1, None, root="r1")]
    appd.on_key("p")
    assert calls == [("pause", "r1")], calls
    appd.downloads[0].status = "paused"
    appd.on_key("p")
    assert calls[-1] == ("resume", "r1"), calls
    appd.on_key("x")
    assert calls[-1] == ("remove", "r1"), calls
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
    # settings overlay: toggle a source (persisted), edit download dir (persisted)
    gc = globals()
    o_load_cfg, o_save_cfg, saved_cfg = gc["load_config"], gc["save_config"], {}
    gc["load_config"] = lambda: {}
    gc["save_config"] = lambda c: saved_cfg.update(c)
    appg = App(eng=None)
    appg.view = "downloads"  # g on the splash landing types; open from a nav view
    assert appg.disabled_sources == set() and not appg.settings
    appg.on_key("g")
    assert appg.settings, "g opens settings"
    appg.set_sel = 1  # first source row
    sid = SOURCES[0].id
    appg.on_key(" ")
    assert sid in appg.disabled_sources and sid in saved_cfg["disabled_sources"], saved_cfg
    assert SOURCES[0] not in appg.enabled_sources()
    appg.on_key(" ")
    assert sid not in appg.disabled_sources, "toggle back on"
    appg.set_sel = 0  # download-dir row
    appg.on_key("enter")
    assert appg.dir_editing
    for ch in "/tmp/dl":
        appg.on_key(ch)
    appg.on_key("enter")
    assert appg.download_dir == "/tmp/dl" and saved_cfg["download_dir"] == "/tmp/dl", saved_cfg
    appg.on_key("g")
    assert not appg.settings, "g closes settings"
    gc["load_config"], gc["save_config"] = o_load_cfg, o_save_cfg
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
    assert "results" in f and "quit" in f and "Result 0" in f, "search chrome"
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

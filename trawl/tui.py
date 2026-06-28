"""Raw-ANSI terminal UI: state, render, input. No third-party deps.

Full-redraw renderer (truecolor) in torlink's look — logo header, left rail,
search/results/downloads panels, sheen progress bars, footer hints. The run
loop lives in __main__.py; this module is the view + state and is importable
without a terminal or aria2 (so render/keys are testable).
"""

from __future__ import annotations

import os
import queue
import re
import select
import shutil
import sys
import termios
import time
import tty
import unicodedata

from . import theme as T
from .aria2 import Aria2Error, Download
from .sources import (SOURCES, Result, Search, dedupe, parse_magnet, sort_results)

GROUP_OF = {s.id: s.group for s in SOURCES}
CATS = [("all", "All"), ("games", "Games"), ("movies", "Movies"),
        ("tv", "TV"), ("anime", "Anime")]
CAT_GROUP = {"games": "Games", "movies": "Movies", "tv": "TV", "anime": "Anime"}

RAIL_W = 13
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
            if i + 2 < n and data[i + 1] in (ord("["), ord("O")) and bytes([data[i + 2]]) in _ARROWS:
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
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()

    def leave(self) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
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
        self.editing = True
        self.query = ""
        self.results: list[Result] = []
        self.errors: dict[str, str] = {}
        self.search: Search | None = None
        self.search_done = 0
        self.search_total = len(SOURCES)
        self.sel = 0
        self.cat = "all"
        self.downloads: list[Download] = []
        self.dsel = 0
        self.help = False
        self.status = ""
        self.running = True
        self.start = time.monotonic()

    # -- derived
    def visible_results(self) -> list[Result]:
        if self.cat == "all":
            return self.results
        g = CAT_GROUP[self.cat]
        return [r for r in self.results if GROUP_OF.get(r.source) == g]

    def animating(self) -> bool:
        return any(d.status in ("active", "metadata") for d in self.downloads)

    @property
    def tick(self) -> float:
        return (time.monotonic() - self.start) * 1000 / T.SHEEN_TICK_MS

    # -- actions
    def submit(self) -> None:
        q = self.query.strip()
        pm = parse_magnet(q)
        if pm:
            self.grab(pm.magnet, pm.name)
            self.query = ""
            self.editing = False
            return
        self.search = Search(q)
        self.results, self.errors, self.search_done, self.sel = [], {}, 0, 0
        self.editing = False
        self.status = f'searching "{clean(q)}"' if q else "loading latest"

    def grab(self, magnet: str, name: str) -> None:
        if not self.eng:
            self.status = f"(no engine) {clean(name)[:48]}"
            return
        try:
            self.eng.add(magnet)
            self.status = f"grabbing: {clean(name)[:48]}"
        except Aria2Error as e:
            self.status = f"error: {e}"

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
            self.results = sort_results(dedupe(self.results))
            self.sel = min(self.sel, max(0, len(self.visible_results()) - 1))

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
        if self.view == "search" and self.editing:
            if k == "enter":
                self.submit()
            elif k == "esc":
                self.editing = False
            elif k == "backspace":
                self.query = self.query[:-1]
            elif k == "tab":
                self.view, self.editing = "downloads", False
            elif k == "up":
                self._move(-1)
            elif k == "down":
                self._move(1)
            elif len(k) == 1 and k >= " ":
                self.query += k
            return
        # nav (results) / downloads
        if k == "q":
            self.running = False
        elif k == "?":
            self.help = True
        elif k == "tab":
            self.view = "downloads" if self.view == "search" else "search"
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
            elif k in ("d", "enter"):
                rs = self.visible_results()
                if rs and 0 <= self.sel < len(rs):
                    self.grab(rs[self.sel].magnet, rs[self.sel].name)
        elif self.view == "downloads":
            if k == "x" and self.downloads and 0 <= self.dsel < len(self.downloads):
                d = self.downloads[self.dsel]
                if self.eng:
                    self.eng.remove(d.root)
                self.status = f"removed: {clean(d.name)[:40]}"
                self.dsel = max(0, self.dsel - 1)


# -- rendering ---------------------------------------------------------------


def _window(sel: int, total: int, h: int) -> int:
    if total <= h:
        return 0
    return max(0, min(sel - h // 2, total - h))


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
    ph = dtrunc("Search or paste a magnet link…", inner_w - 2)
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
            return cell("Type to search. Enter runs it; paste a magnet to grab it.", inner_w, dim=True)
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
            ptr = cell(T.PTR if here else "", 2, color=T.ACCENT)
            inner.append(
                ptr + " "
                + cell(clean(r.name), name_w, color=T.ACCENT if here else None, bold=here, dim=not here) + " "
                + cell(fmt_bytes(r.size), 9, "right", dim=True) + " "
                + cell(sl, 9, "right", color=T.GOOD if r.seeders else None, dim=not r.seeders) + " "
                + cell(tag, 5, "right", color=tcolor, dim=not here))
    title = "Latest" if (app.search is not None and not app.query.strip()) else "Results"
    count = f"({len(results)})" if results else None
    return _wrap_panel(title, inner, width, height, app.view == "search" and not app.editing, count)


def _downloads_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    inner: list[str] = []
    if not app.downloads:
        inner.append(cell("No downloads yet. Find something and press d to grab it.", inner_w, dim=True))
    else:
        app.dsel = min(app.dsel, len(app.downloads) - 1)
        per = 2
        list_h = max(1, (height - 2) // per)
        start = _window(app.dsel, len(app.downloads), list_h)
        for idx in range(start, min(start + list_h, len(app.downloads))):
            d = app.downloads[idx]
            here = idx == app.dsel
            if d.status == "error":
                icon, ic = T.ERR, T.BAD
            elif d.status == "complete":
                icon, ic = T.DONE, T.GOOD
            else:
                icon, ic = T.DOWN, T.ACCENT
            pct = int(d.progress * 100)
            if d.status == "active":
                stats = f"{pct}%  {fmt_speed(d.speed)}  {T.PEER}{d.peers}" + (f"  {fmt_eta(d.eta)}" if d.eta else "")
            elif d.status == "metadata":
                stats = "fetching metadata…"
            elif d.status == "complete":
                stats = "done"
            elif d.status == "error":
                stats = dtrunc(d.error or "failed", 28)
            else:
                stats = d.status
            stat_w = min(len(stats) + 1, inner_w - 6)
            name_w = inner_w - 2 - stat_w
            inner.append(
                cell(icon, 2, color=ic) + cell(clean(d.name) or "…", name_w,
                                               color=T.ACCENT if here else None, bold=here, dim=not here)
                + cell(stats, stat_w, "right", dim=True))
            animate = d.status in ("active", "metadata")
            base = T.GOOD if d.status == "complete" else (T.BAD if d.status == "error" else T.ACCENT)
            bar = render_bar(d.progress, inner_w - 2, app.tick, animate, base)
            inner.append("  " + bar)
    n = len(app.downloads)
    return _wrap_panel("Downloads", inner, width, height, app.view == "downloads",
                           f"({n})" if n else None)


def _help_panel(width: int, height: int) -> list[str]:
    inner_w = width - 4
    groups = [
        ("Search", [("type", "edit query"), ("enter", "run search / grab magnet"),
                    ("esc", "leave edit"), ("/  i", "edit query")]),
        ("Navigate", [("↑ ↓  j k", "move selection"), ("← →", "filter category"),
                      ("tab", "switch search / downloads")]),
        ("Actions", [("d  enter", "download selected"), ("x", "remove download")]),
        ("General", [("?", "this help"), ("q", "quit"), ("ctrl-c", "quit")]),
    ]
    inner = [cell("Keys", inner_w, color=T.ACCENT, bold=True), cell("", inner_w)]
    for title, items in groups:
        inner.append(cell(title, inner_w, color=T.ALT, bold=True))
        for keys, desc in items:
            inner.append("  " + cell(keys, 12, color=T.BRIGHT) + " " + cell(desc, inner_w - 15, dim=True))
        inner.append(cell("", inner_w))
    return _wrap_panel("Help", inner, width, height, True)


def _footer(app: App, width: int) -> str:
    if app.help:
        hints = [("any key", "close")]
    elif app.view == "search" and app.editing:
        hints = [("enter", "search"), ("esc", "nav"), ("tab", "downloads"), ("^c", "quit")]
    elif app.view == "search":
        hints = [("↑↓", "move"), ("d", "grab"), ("←→", "category"), ("/", "edit"),
                 ("tab", "downloads"), ("?", "keys"), ("q", "quit")]
    else:
        hints = [("↑↓", "move"), ("x", "remove"), ("tab", "search"), ("?", "keys"), ("q", "quit")]
    out, used = "", 0
    if app.status:
        st = dtrunc(clean(app.status), max(10, width // 2))
        out = style(st, T.ALT) + "   "
        used = dwidth(st) + 3
    sep = "  " + T.DOT + "  "
    sep_w = dwidth(sep)
    first = True
    for k, v in hints:
        add = dwidth(k) + 1 + dwidth(v) + (0 if first else sep_w)
        if used + add > width:
            break
        if not first:
            out += style(sep, dim=True)
        out += style(k, T.ACCENT) + style(" " + v, dim=True)
        used += add
        first = False
    return out


TAGLINE = "A curated, terminal-native torrent finder."
CATS_LINE = "games  ·  movies  ·  tv  ·  anime"


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
    parts, plain = [], 0
    for i, (k, v) in enumerate([("↵", "search"), ("↵", "browse"), ("^c", "quit")]):
        if i:
            parts.append(style("   ", dim=True))
            plain += 3
        parts.append(style(k, T.ALT) + style(" " + v, dim=True))
        plain += dwidth(k) + 1 + dwidth(v)
    block += ["", _center("".join(parts), plain, cols)]
    top = max(0, (rows - len(block)) // 2)
    return ([""] * top + block + [""] * rows)[:rows]

def render(app: App, cols: int, rows: int) -> list[str]:
    cols = max(40, cols)
    rows = max(12, rows)
    if app.view == "search" and app.search is None and not app.help:
        return _splash(app, cols, rows)
    lines: list[str] = []
    for L in _logo_lines():
        lines.append(" " * MARGIN + L)
    lines.append(" " * MARGIN + style("─" * max(0, cols - 2 * MARGIN), T.RULE))

    header_h = len(T.LOGO_LINES) + 1
    footer_h = 2
    body_h = rows - header_h - footer_h
    content_w = cols - MARGIN - RAIL_W - GAP - 1

    if app.help:
        content = _help_panel(content_w, body_h)
        rail = [cell("", RAIL_W)] * body_h
    else:
        rail = _rail(app, body_h)
        search_h = 3
        content = _search_panel(app, content_w) + [""]
        panel_h = body_h - search_h - 1
        if app.view == "search":
            content += _results_panel(app, content_w, panel_h)
        else:
            content += _downloads_panel(app, content_w, panel_h)
    content = (content + [""] * body_h)[:body_h]

    for i in range(body_h):
        lines.append(" " * MARGIN + rail[i] + " " * GAP + content[i])

    lines.append("")
    lines.append(" " * MARGIN + _footer(app, cols - MARGIN))
    return (lines + [""] * rows)[:rows]


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
    print("primitives ok")

    # interaction: edit -> type -> submit -> nav -> tab -> downloads
    app = App(eng=None)
    assert app.view == "search" and app.editing
    for ch in "matrix":
        app.on_key(ch)
    assert app.query == "matrix", app.query
    app.on_key("backspace")
    assert app.query == "matri"
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
    app.on_key("right")  # cycle category off "all"
    assert app.cat == "games", app.cat
    app.on_key("tab")
    assert app.view == "downloads"
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
    ]
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

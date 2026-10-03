"""trawl entry point + run loop.

Single-threaded: poll stdin, dispatch keys, drain the search queue, and check
aria2 no more often than every 500ms (about 600ms when idle). Each frame is
rebuilt; Terminal emits only changed rows, or nothing when unchanged.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
import traceback

from . import __version__
from .aria2 import STATE_DIR, Aria2, Aria2Error
from .sources import parse_magnet, parse_source, refresh_trackers
from .tui import App, Terminal, paste_clipboard, render

HELP = ("trawl — terminal torrent finder over aria2.\n"
        "  trawl               start (press s to resume partial downloads on disk)\n"
        "  trawl <query>       start and search for a query\n"
        "  trawl <magnet|url|file.torrent>  start and grab a magnet, link, or torrent file")


_logged: set[str] = set()


def log_crash(e: BaseException) -> None:
    """Append the traceback to crash.log in the state dir (the UI keeps running). Each distinct
    error is written once per run, so a recurring one can't grow the file without bound."""
    tb = "".join(traceback.format_exception(e))
    if tb in _logged:
        return
    _logged.add(tb)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "crash.log", "a") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} trawl {__version__}\n{tb}")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    initial = None
    for a in argv:
        if a in ("-h", "--help"):
            print(HELP)
            return 0
        if a in ("-V", "--version"):
            print(f"trawl {__version__}")
            return 0
        if not initial and parse_source(a):
            initial = a
        elif not initial:
            initial = a  # a plain search query
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("trawl needs an interactive terminal.")
        return 1

    eng = Aria2()
    try:
        eng.start()
    except Aria2Error as e:
        print(f"aria2 failed to start: {e}\nIs aria2 installed? (brew install aria2)")
        return 1
    threading.Thread(target=refresh_trackers, daemon=True).start()

    app = App(eng)
    threading.Thread(target=app.check_update, daemon=True).start()
    app.start_check()  # followed shows: look for new episodes in the background
    if app.download_dir:
        eng.set_dir(app.download_dir)
    if app.speed_limit:
        eng.set_limit(app.speed_limit)
    if app.max_dl_set:
        eng.set_max_concurrent(app.max_dl_set)
    app.max_dl = app.max_dl_set or eng.max_concurrent()
    app.clipboard_seen = paste_clipboard()
    if initial:
        pm = parse_source(initial)
        if pm:
            app._grab_source(pm)
            if not app.torrent_prompt:  # a .torrent link waits on the file/contents prompt
                app.view, app.editing = "downloads", False
        else:  # a plain query: seed and run the search
            app.query = initial
            app.submit()
    elif parse_magnet(app.clipboard_seen):
        app.status = "magnet detected in clipboard — press v to grab it"

    term = Terminal()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    term.enter()
    last_poll = 0.0
    prev_title = None
    try:
        failures = 0
        while app.running:
            try:
                cols, rows = term.size()
                busy = bool(app.activity())  # the spinner needs a steady ~10 fps while something runs
                dirty = bool(keys := term.read_keys(0.04 if app.animating(rows) else 0.1 if busy else 0.2))
                for k in keys:
                    app.on_key(k)
                    dirty = True  # also covers keys that open/close views without state change
                if not app.running:
                    break
                if app.drain_search():
                    dirty = True
                now = time.monotonic()
                if now - last_poll > 0.5:
                    try:
                        app.update_downloads(eng.poll())
                        g = eng.global_stat()
                        app.down_speed = int(g.get("downloadSpeed", 0) or 0)
                        app.num_active = int(g.get("numActive", 0) or 0)
                    except Aria2Error:
                        pass
                    last_poll = now
                    app.start_check()  # no-op unless a followed show is due
                    dirty = True
                    app.check_clipboard()
                if busy or app.animating(rows):
                    dirty = True  # spinner / sheen animation frames
                if dirty or term.size() != (cols, rows):
                    cols, rows = term.size()
                    term.write(render(app, cols, rows), (cols, rows))
                    title = app.page_title
                    if title != prev_title:
                        term.set_title(title)
                        prev_title = title
                failures = 0
            except Exception as e:  # a bug must not take the downloads down with the app
                failures += 1
                log_crash(e)
                app.status = "error: something went wrong — details saved to crash.log"
                if failures > 20:  # failing every tick (e.g. the terminal is gone): give up
                    raise
    except KeyboardInterrupt:
        pass
    finally:
        term.leave()
        app.end_peek()
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

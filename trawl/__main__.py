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

from . import __version__
from .aria2 import Aria2, Aria2Error
from .sources import parse_magnet, parse_source, refresh_trackers
from .tui import App, Terminal, paste_clipboard, render

HELP = ("trawl — terminal torrent finder over aria2.\n"
        "  trawl               start (press s to resume partial downloads on disk)\n"
        "  trawl <query>       start and search for a query\n"
        "  trawl <magnet|url>  start and grab a magnet or direct link")


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
    if app.download_dir:
        eng.set_dir(app.download_dir)
    if initial:
        pm = parse_source(initial)
        if pm:
            app._grab_source(pm)
            if not app.torrent_prompt:  # a .torrent link waits on the file/contents prompt
                app.view, app.editing = "downloads", False
        else:  # a plain query: seed and run the search
            app.query = initial
            app.submit()
    elif parse_magnet(paste_clipboard()):
        app.status = "magnet detected in clipboard — press v to grab it"

    term = Terminal()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    term.enter()
    last_poll = 0.0
    try:
        while app.running:
            cols, rows = term.size()
            dirty = bool(keys := term.read_keys(0.04 if app.animating(rows) else 0.2))
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
                dirty = True
            if app.animating(rows):
                dirty = True  # the sheen needs the animation frame rate
            if dirty or term.size() != (cols, rows):
                cols, rows = term.size()
                term.write(render(app, cols, rows), (cols, rows))
    except KeyboardInterrupt:
        pass
    finally:
        term.leave()
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

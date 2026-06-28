"""trawl entry point + run loop.

Single-threaded: poll stdin with a frame-interval timeout, dispatch keys, drain
the search queue, poll aria2 every 500ms, full-redraw. aria2 does the
downloading in its own process; daemon search threads die with us on quit.
"""

from __future__ import annotations

import glob
import os
import signal
import sys
import time

from .aria2 import Aria2, Aria2Error, control_infohash
from .sources import build_magnet, parse_magnet
from .tui import App, Terminal, render

HELP = ("trawl — terminal torrent finder over aria2.\n"
        "  trawl              start (resumes partial downloads found on disk)\n"
        "  trawl --no-resume  start without scanning for resumables\n"
        "  trawl <magnet>     start and grab a magnet")


def auto_resume(eng: Aria2) -> int:
    """Re-add any incomplete BT downloads (*.aria2 control files) in the download
    dir that aria2 isn't already running, so they resume from where they stopped."""
    dir_path = eng.download_dir()
    if not dir_path or not os.path.isdir(dir_path):
        return 0
    have = eng.active_infohashes()
    n = 0
    for ctrl in glob.glob(os.path.join(dir_path, "*.aria2")):
        ih = control_infohash(ctrl)
        if not ih or ih in have:
            continue
        try:
            eng.add(build_magnet(ih, os.path.basename(ctrl)[:-7]), {"dir": dir_path})
            have.add(ih)
            n += 1
        except Aria2Error:
            pass
    return n


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    initial = None
    no_resume = False
    for a in argv:
        if a in ("-h", "--help"):
            print(HELP)
            return 0
        if a == "--no-resume":
            no_resume = True
        if a.lower().startswith("magnet:?"):
            initial = a
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("trawl needs an interactive terminal.")
        return 1

    eng = Aria2()
    try:
        eng.start()
    except Aria2Error as e:
        print(f"aria2 failed to start: {e}\nIs aria2 installed? (brew install aria2)")
        return 1

    resumed = 0 if no_resume else auto_resume(eng)
    app = App(eng)
    if initial:
        pm = parse_magnet(initial)
        if pm:
            app.grab(pm.magnet, pm.name)
            app.view, app.editing = "downloads", False
    if resumed:
        app.view = "downloads"
        app.status = f"resumed {resumed} download{'' if resumed == 1 else 's'}"

    term = Terminal()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    term.enter()
    last_poll = 0.0
    try:
        while app.running:
            for k in term.read_keys(0.04 if app.animating() else 0.2):
                app.on_key(k)
            if not app.running:
                break
            app.drain_search()
            now = time.monotonic()
            if now - last_poll > 0.5:
                try:
                    app.downloads = eng.poll()
                    g = eng.global_stat()
                    app.down_speed = int(g.get("downloadSpeed", 0) or 0)
                    app.num_active = int(g.get("numActive", 0) or 0)
                except Aria2Error:
                    pass
                last_poll = now
            cols, rows = term.size()
            term.write(render(app, cols, rows))
    except KeyboardInterrupt:
        pass
    finally:
        term.leave()
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

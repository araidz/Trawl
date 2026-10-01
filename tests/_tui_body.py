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
assert parse_keys(b"\x1b[5~\x1b[6~") == ["pageup", "pagedown"], "page keys"
assert parse_keys(b"\x1b[H\x1b[F\x1b[1~\x1b[4~\x1b[7~\x1b[8~") == \
    ["home", "end", "home", "end", "home", "end"], "home/end variants"
assert parse_keys(b"\x01\x05\x15\x17") == ["ctrl-a", "ctrl-e", "ctrl-u", "ctrl-w"]
assert parse_keys("café".encode() + b"\x1b[200~paste me\x1b[201~") == \
    ["c", "a", "f", "é", "p", "a", "s", "t", "e", " ", "m", "e"], "bracketed paste"
assert parse_keys(b"\x1b[200~a\x03b\x1b[201~") == ["a", "b"], "paste drops control chars"
print("primitives ok")

# interaction: edit -> type -> submit -> nav -> tab -> downloads
app = App(eng=None)
assert app.view == "search" and not app.editing and app.search is None, "splash landing"
app.on_key("q")
assert not app.running and not app.confirm_quit, "idle q quits at once"
app.running = True
app.downloads = [Download("g", "F", "active", 100, 10, 5, 1, None, root="r")]
app.on_key("q")
assert app.confirm_quit and app.running, "q asks first while a download is running"
app.on_key("esc")
assert not app.confirm_quit
app.downloads = []
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
# editor movement keys: home/end, ctrl-a/e/u/w
app.on_key("end")
assert app.cursor == 5, app.cursor
app.on_key("home")
assert app.cursor == 0, app.cursor
app.on_key("ctrl-e")
assert app.cursor == 5, app.cursor
app.on_key("ctrl-a")
app.on_key("ctrl-w")  # no word before the caret -> nothing to kill
assert app.query == "matri" and app.cursor == 0, (app.query, app.cursor)
app.on_key("ctrl-e")
app.on_key("ctrl-w")  # kills the whole word
assert app.query == "" and app.cursor == 0, (app.query, app.cursor)
for ch in "foo bar baz":
    app.on_key(ch)
assert app.query == "foo bar baz" and app.cursor == 11, (app.query, app.cursor)
app.on_key("ctrl-a")
for _ in range(4):
    app.on_key("right")  # caret after "foo "
app.on_key("ctrl-w")  # kills "foo " including the gap
assert app.query == "bar baz" and app.cursor == 0, (app.query, app.cursor)
app.on_key("end")
app.on_key("ctrl-u")  # kill to start
assert app.query == "", app.query
app.query, app.cursor = "matri", 5  # restore for the nav checks below
app.search = Search.__new__(Search)  # simulate a completed search -> browse nav
app.search_done = app.search_total
app.results = [
    Result("a" * 40, "The Matrix 1999 [1080p]", 1_500_000_000, 900, 30, "yts", "magnet:?xt=m"),
    Result("b" * 40, "The Matrix Reloaded", 2_000_000_000, 0, 0, "fitgirl", "magnet:?xt=m"),
]
app.editing = False
app.on_key("down")
assert app.sel == 1, app.sel
app.on_key("pageup")  # paged nav clamps to the top
assert app.sel == 0, app.sel
app.on_key("end")
assert app.sel == 1, app.sel
app.on_key("home")
assert app.sel == 0, app.sel
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
appq.downloads = [Download("g", "F", "active", 100, 10, 5, 1, None, root="r")]
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

# D opens the folder prompt, enter commits a per-download dir; esc cancels.
import tempfile as _tf
_dirs = []
class _DirEng:
    def add(self, uri, options=None): _dirs.append((uri, options))
appf = App(eng=_DirEng())
appf.search = Search.__new__(Search)
appf.editing = False
appf.results = [Result("b" * 40, "Folder Test", 1, 5, 1, "yts", "magnet:?xt=fold")]
appf.on_key("D")
assert appf.folder_prompt is not None, "D opens folder prompt"
appf.folder_buf = ""  # prompt defaults to the download dir; type a fresh path
_target = os.path.join(_tf.mkdtemp(), "nest", "dl")
for ch in _target:
    appf.on_key(ch)
appf.on_key("enter")
assert appf.folder_prompt is None and appf.last_dir == _target, appf.last_dir
assert os.path.isdir(_target), "folder prompt created the target dir"
assert _dirs[-1] == ("magnet:?xt=fold", {"dir": _target}), _dirs
appf.view = "search"  # a successful folder grab jumps to downloads
appf.on_key("D")
assert appf.folder_buf == _target, "last_dir reused as prompt default"
appf.on_key("esc")
assert appf.folder_prompt is None, "esc cancels folder prompt"
appf.on_key("D")
appf.folder_buf = "/nonexistent-root-xyz/blocked"
appf.on_key("enter")
assert appf.folder_prompt is None and "couldn't create" in appf.status, appf.status
assert _dirs[-1] == ("magnet:?xt=fold", {"dir": _target}), "failed dir did not enqueue"

# e exports the .torrent via save_metadata (magnet only); once the fetch
# completes the <hash>.torrent is renamed to the item's name.
_expdir = _tf.mkdtemp()
_meta, _removed, _status = [], [], {}
class _MetaEng:
    def download_dir(self): return _expdir
    def save_metadata(self, uri, d): _meta.append((uri, d)); return "xgid"
    def remove(self, r): _removed.append(r)
    def status(self, r): return _status.get(r, "")
appe = App(eng=_MetaEng())
appe.search = Search.__new__(Search)
appe.editing = False
appe.results = [Result("b" * 40, "Meta Test", 1, 5, 1, "yts", f"magnet:?xt=urn:btih:{'b' * 40}"),
                Result("c" * 40, "Link Test", 1, 5, 1, "yts", "https://x.invalid/f.torrent")]
appe.on_key("e")
assert _meta == [(f"magnet:?xt=urn:btih:{'b' * 40}", _expdir)], _meta
assert appe.status.startswith("fetching .torrent"), appe.status
assert appe._exports["xgid"][:2] == ("b" * 40, "Meta Test"), appe._exports
# still fetching (raw status active): the row stays hidden and no error fires
_status["xgid"] = "active"
appe.update_downloads([type("_P", (), {"root": "xgid", "status": "metadata",
                                       "name": "", "total": 0, "path": ""})()])
assert appe._exports and appe.downloads == [] and "couldn't fetch" not in appe.status, \
    (appe._exports, appe.status)
# raw status flips complete -> rename <hash>.torrent to the item name
open(os.path.join(_expdir, "b" * 40 + ".torrent"), "w").close()  # what aria2 writes
_status["xgid"] = "complete"
appe.update_downloads([type("_Done", (), {"root": "xgid", "status": "metadata",
                                          "name": "", "total": 0, "path": ""})()])
assert os.path.exists(os.path.join(_expdir, "Meta Test.torrent")), "renamed to item name"
assert not os.path.exists(os.path.join(_expdir, "b" * 40 + ".torrent")), "hash file replaced"
assert appe._exports == {} and appe.downloads == [] and _removed == ["xgid"], (appe._exports, _removed)
assert appe.status.startswith("saved Meta Test.torrent"), appe.status
# an errored fetch reports failure; a hung fetch times out
appe._exports["xerr"] = ("c" * 40, "E", _expdir, time.monotonic())
_status["xerr"] = "error"
appe.update_downloads([type("_F", (), {"root": "xerr", "status": "error",
                                       "name": "", "total": 0, "path": ""})()])
assert "couldn't fetch .torrent metadata" == appe.status and _removed == ["xgid", "xerr"], \
    (appe.status, _removed)
appe._exports["xhun"] = ("d" * 40, "H", _expdir, time.monotonic() - 61)
_status["xhun"] = "active"
appe.update_downloads([type("_T", (), {"root": "xhun", "status": "active",
                                       "name": "", "total": 0, "path": ""})()])
assert appe.status.endswith("(timed out)") and "xhun" not in appe._exports, appe.status
appe.on_key("j"); appe.on_key("e")
assert len(_meta) == 1, "link source does not export"
assert "not a magnet" in appe.status, appe.status

# z toggles hide-dead: swarm-less sources always show, zero-seeder swarm sources hide
appz = App(eng=None)
appz.search = Search.__new__(Search)
appz.editing = False
appz.results = [Result("0" * 40, "Dead", 1, 0, 0, "yts", "magnet:?xt=0"),
                Result("1" * 40, "Unknown Health", 1, 0, 0, "fitgirl", "magnet:?xt=1")]
assert {r.info_hash for r in appz.visible_results()} == {"0" * 40, "1" * 40}
appz.on_key("z")
assert appz.hide_dead is True and {r.info_hash for r in appz.visible_results()} == {"1" * 40}, \
    "hide-dead keeps swarm-less rows, drops dead swarm rows"
appz.on_key("z")
assert appz.hide_dead is False
assert seed_leech(appz.results[1], reports_health=False) == "—", "unknown health shows dash"
assert seed_leech(appz.results[0], reports_health=True) == "-", "dead swarm shows dash-minus"

# s scans for resumables via the engine; metadata reveal gives a clear message
from pathlib import Path
class _ScanEng:
    def download_dir(self): return "/no-such-dir-xyz"
    def active_infohashes(self): return set()
    def active_uris(self): return set()
    def add(self, uri, options=None): raise AssertionError("nothing should be re-added")

globals()["PENDING_FILE"] = Path(_tf.mkdtemp()) / "pending.jsonl"
apps = App(eng=_ScanEng())
apps.view = "downloads"
apps.on_key("s")
assert apps.status == "nothing to resume on disk", apps.status
# direct-http resume: pending.jsonl re-adds, dedupes in-flight uris, clears
pend = Path(tempfile.mkdtemp()) / "pending.jsonl"
pend.write_text(json.dumps({"uri": "https://x.invalid/book.epub", "dir": "/dl"}) + "\n")
added = []
class _PendEng:
    def download_dir(self): return None
    def active_infohashes(self): return set()
    def active_uris(self): return set()
    def add(self, uri, options=None): added.append((uri, options))
globals()["PENDING_FILE"] = pend
appp = App(eng=_PendEng())
assert appp.scan_resume() == 1 and added == [("https://x.invalid/book.epub", {"dir": "/dl"})], added
assert not pend.exists(), "pending file cleared after a scan"
pend2 = Path(tempfile.mkdtemp()) / "pending.jsonl"
pend2.write_text(json.dumps({"uri": "https://x.invalid/live.iso", "dir": None}) + "\n")
class _LiveEng:
    def download_dir(self): return None
    def active_infohashes(self): return set()
    def active_uris(self): return {"https://x.invalid/live.iso"}
    def add(self, uri, options=None): raise AssertionError("in-flight uri re-added")
globals()["PENDING_FILE"] = pend2
appq = App(eng=_LiveEng())
assert appq.scan_resume() == 0, "in-flight uri skipped"
assert not pend2.exists(), "pending cleared even with nothing to re-add"
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
assert appi.visible_results() == cached, "visible results re-render stable"
appi.results = appi.results[:]
assert appi.visible_results() == cached, "replacement keeps order and content"
cached = appi.visible_results()
appi.cat = "anime"
assert {r.name for r in appi.visible_results()} == {"anime1", "anime2", "anime3"}, "category filter"
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
before_results = appc.results
update = type("Update", (), {"source": "yts", "results": [second], "error": ""})()
appc.search.updates.put(update)
appc.drain_search()
assert appc.results is not before_results, "update replaces the results tuple"
assert {r.name for r in appc.visible_results()} == {"first", "second"}, "append-like update"
appc._cycle_sort()
assert appc.results[0] is second, "sort replacement"
appc.results = []
assert appc.visible_results() == (), "clear replacement"
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
# clipboard watcher (check_clipboard) offers v once per new grabbable clipboard
def _clip_seq():
    _clip_seq.n = getattr(_clip_seq, "n", 0) + 1
    return ["", "magnet:?xt=urn:btih:" + "a" * 40, "magnet:?xt=urn:btih:" + "a" * 40][min(_clip_seq.n - 1, 2)]
gp["paste_clipboard"] = _clip_seq
appw = App(eng=None)
appw.editing = False
appw.clipboard_seen = ""
appw.check_clipboard()
assert appw.status == "", "empty clipboard offers nothing"
appw.check_clipboard()
assert "press v" in appw.status, appw.status
appw.status = ""
appw.check_clipboard()
assert appw.status == "", "same clipboard is not re-offered"
appw.editing = True
appw.clipboard_seen = ""
appw.check_clipboard()
assert appw.status == "", "no offer while editing"
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
# opening lands on the first selectable row (never a section header)
assert appg.setting_items()[appg.set_sel][0] == "dir", "g did not snap to first row"
# provider toggle flips tmdb<->omdb and persists
assert appg.meta_provider == "tmdb"
appg.set_sel = next(i for i, it in enumerate(appg.setting_items()) if it[0] == "provider")
appg.on_key(" ")
assert appg.meta_provider == "omdb" and saved_cfg["meta_provider"] == "omdb", saved_cfg
appg.on_key(" ")
assert appg.meta_provider == "tmdb", "provider toggles back"
# theme toggle flips the global palette and persists
assert appg.theme == "violet"
appg.set_sel = next(i for i, it in enumerate(appg.setting_items()) if it[0] == "theme")
appg.on_key("enter")
assert appg.theme == "light" and saved_cfg["theme"] == "light" and T.ACCENT == "#6d4fc9", \
    "theme toggle did not flip palette/persist"
seen = [appg.theme]
for _ in range(len(T.THEMES) - 1):   # every palette is reachable, then it wraps to violet
    appg.on_key(" ")
    seen.append(appg.theme)
assert seen == [*T.THEMES[1:], "violet"] and len(set(seen)) == len(T.THEMES) >= 7, seen
assert appg.theme == "violet" and T.ACCENT == "#a78bfa" and saved_cfg["theme"] == "violet", "theme cycle wraps"
# key entry writes the active provider's key
appg.set_sel = next(i for i, it in enumerate(appg.setting_items()) if it[0] == "meta-key")
appg.tmdb_key = None
appg.on_key("enter")
assert appg.edit_field == "key"
for ch in "abc123":
    appg.on_key(ch)
appg.on_key("enter")
assert appg.tmdb_key == "abc123" and saved_cfg["tmdb_key"] == "abc123", saved_cfg
# source toggle
appg.set_sel = next(i for i, it in enumerate(appg.setting_items()) if it[0] == "source")
sid = SOURCES[0].id
appg.on_key(" ")
assert sid in appg.disabled_sources and sid in saved_cfg["disabled_sources"], saved_cfg
assert SOURCES[0] not in appg.enabled_sources()
appg.on_key(" ")
assert sid not in appg.disabled_sources, "toggle back on"
# navigation never lands on a section header
appg.set_sel = 0
for _ in range(3 * len(appg.setting_items())):
    appg.on_key("down")
    assert appg.setting_items()[appg.set_sel][0] != "section", "down landed on a section"
    appg.on_key("j")
    assert appg.setting_items()[appg.set_sel][0] != "section", "j landed on a section"
# download dir
appg.set_sel = next(i for i, it in enumerate(appg.setting_items()) if it[0] == "dir")
appg.on_key("enter")
assert appg.edit_field == "dir"
for ch in "/tmp/dl":
    appg.on_key(ch)
appg.on_key("enter")
assert appg.download_dir == "/tmp/dl" and saved_cfg["download_dir"] == "/tmp/dl", saved_cfg
# speed limit row: edit, validate, persist a cap, then clear it
appg.set_sel = next(i for i, it in enumerate(appg.setting_items()) if it[0] == "limit")
appg.on_key("enter")
assert appg.edit_field == "limit"
for ch in "2M":
    appg.on_key(ch)
appg.on_key("enter")
assert appg.speed_limit == "2M" and saved_cfg["speed_limit"] == "2M", saved_cfg
appg.on_key("enter")
appg.edit_buf = "abc"
appg.on_key("enter")
assert appg.speed_limit == "2M" and "limit" in appg.status, (appg.speed_limit, appg.status)
appg.on_key("esc")
appg.on_key("enter")  # reopen, clear, commit -> unlimited
appg.on_key("ctrl-u")
appg.on_key("enter")
assert appg.speed_limit is None, appg.speed_limit
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
    aset.on_key("K"); aset.edit_buf = old_key_secret; aset.on_key("enter")
    aset.on_key("K"); aset.edit_buf = "separate"; aset.on_key("enter")
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
    aset.set_sel = next(i for i, it in enumerate(aset.setting_items()) if it[0] == "source")
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

    # E toggles the per-source error viewer; the panel shows labels + messages
    err_app = App(eng=None)
    err_app.search = Search.__new__(Search)
    err_app.errors = {"tpb-tv": "HTTP 403", "annas": "blocked by Cloudflare"}
    err_app.editing = False
    err_app.on_key("E")
    assert err_app.show_errors, "E opens the errors viewer"
    ep = "\n".join(strip_ansi(x) for x in render(err_app, 100, 30))
    assert "Sources" in ep and "2 failed" in ep and "TPB" in ep and "HTTP 403" in ep, ep
    assert "blocked by Cloudflare" in ep, ep
    err_app.on_key("esc")
    assert not err_app.show_errors, "esc closes the errors viewer"
    err_app.errors = {}
    err_app.on_key("E")
    assert not err_app.show_errors and "nothing" in err_app.status, err_app.status

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
    key_row = next(i for i, item in enumerate(secret_app.setting_items()) if item[0] == "meta-key")
    secret_app.set_sel, secret_app.edit_field, secret_app.edit_buf = key_row, "key", sentinel
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

    # Settings footer is context-sensitive — source rows never show feed-only actions.
    faf = App(); faf.settings = True
    faf.set_sel = next(i for i, it in enumerate(faf.setting_items()) if it[0] == "source")
    src_ftr = strip_ansi(_footer(faf, 100))
    assert "endpoint" not in src_ftr and "remove" not in src_ftr, \
        f"source-row footer leaked feed keys: {src_ftr}"
    faf.torznab_feeds = [feed]; faf._rebuild_sources()
    faf.set_sel = next(i for i, item in enumerate(faf.setting_items()) if item[0] == "feed")
    feed_ftr = strip_ansi(_footer(faf, 100))
    assert "endpoint" in feed_ftr and "remove" in feed_ftr, \
        f"feed-row footer missing feed keys: {feed_ftr}"

    # Settings panel renders section headers, no repeated "Sources ·" prefix,
    # and a header count; selected row stays on a selectable row.
    sp = App(); sp.settings = True; sp.set_sel = len(sp.setting_items()) - 1
    panel = "\n".join(strip_ansi(x) for x in _settings_panel(sp, 90, 60))
    for head in ("GENERAL", "SOURCES", "TORZNAB FEEDS", "+ Add Torznab feed"):
        assert head in panel, f"settings panel missing {head!r}"
    assert "Theme" in panel and "Violet" in panel, "settings panel theme row missing"
    assert panel.count("+ Add Torznab feed") == 1, "settings panel add-feed duplicated"
    assert "Sources · " not in panel and "sources on" in panel, \
        "settings panel kept the repeated prefix or lost its count"
    assert sp.set_sel == len(sp.setting_items()) - 1, "panel render moved the selection"
    sp.set_sel = next(i for i, it in enumerate(sp.setting_items()) if it[0] == "section")
    _settings_panel(sp, 90, 60)
    assert sp.setting_items()[sp.set_sel][0] != "section", "render left selection on a section"
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
app2.downloads[1].status = "active"  # second bar visible; overlays no longer stop it
for prompt in ("confirm_quit", "torrent_prompt", "cancel_prompt"):
    setattr(app2, prompt, True)
    assert app2.animating(24), f"overlay must not stop animation: {prompt}"
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
assert "Settings" in joined and "SOURCES" in joined and "FitGirl" in joined, "settings view"
app2.settings = False
# details view renders, width-safe
app2.view, app2.detail = "search", app2.results[0]
df = render(app2, 100, 30)
for ln in df:
    assert dwidth(strip_ansi(ln)) <= 100, "details overflow"
assert any("Details" in strip_ansi(x) for x in df) and any("Health" in strip_ansi(x) for x in df), "details view"
app2.detail = None
# health bar uses block/track glyphs; seed column drops ":0" and tiers color
assert ":" in seed_leech(app2.results[0]) or seed_leech(app2.results[0]) != "0:0"
app2.view, app2.detail = "search", app2.results[0]
dh = "\n".join(strip_ansi(x) for x in render(app2, 100, 30))
assert "s · " in dh and (T.BLOCK in dh or T.TRACK in dh), "details health bar"
app2.detail = None
# rail: categories show glyph + per-category result counts on the results view
app2.view = "search"
rl = "\n".join(strip_ansi(x) for x in render(app2, 100, 30))
for glyph in CAT_GLYPH.values():
    assert glyph in rl, f"rail glyph {glyph!r} missing"
assert f"  {len(app2.results)} " in rl, "rail 'all' count missing"
# help: scrollable, key column wide enough to avoid truncation
app2.help, app2.help_scroll = True, 0
hp = "\n".join(strip_ansi(x) for x in render(app2, 140, 72))  # tall enough for all groups (help scrolls, so it keeps growing)
assert "enter / space" in hp and "on a feed row" in hp, "help keys truncated"
assert "Downloads" in hp and "ctrl-c" in hp, "help last groups not visible at tall size"
app2.on_key("down"); app2.on_key("down"); app2.on_key("down")
assert app2.help_scroll == 3, "help down did not scroll"
app2.on_key("x")
assert not app2.help, "help any-key close"
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

# session 2: layout, disk-space guard, open finished, file drop, update notice
gs = globals()
apr = App(eng=None)
apr.search, apr.query = object(), "x"
apr.results = [Result("a" * 40, "Dune.2021.1080p.WEB-DL.x265", 10 ** 9, 5, 1, "yts", "m")]
for w in (45, 60, 79, 80, 120):  # panel widths; below ~41 the 8-col name minimum overflows (old limit)
    assert {dwidth(strip_ansi(x)) for x in _results_panel(apr, w, 6)} == {w}, f"results panel width {w}"
assert any("Release" in strip_ansi(x) for x in _results_panel(apr, 80, 6)), "badge at inner 76"
assert not any("Release" in strip_ansi(x) for x in _results_panel(apr, 79, 6)), "no badge below it"
assert open_target("/d/Show.S01/E01.mkv", "Show.S01") == "/d/Show.S01", "multi-file opens its folder"
assert open_target("/d/Pack/CD1/a.mkv", "Pack") == "/d/Pack", "nested file opens the top folder"
assert open_target("/d/movie.mkv", "movie.mkv") == "/d/movie.mkv", "single file opens the file"
assert isinstance(free_space("/nonexistent/a/b"), int), "free space walks up to an existing parent"
assert _newer("v0.6.0", "0.5.0") and _newer("1.0.0", "0.9.9") and not _newer("v0.5.0", "0.5.0")
assert not _newer("nightly", "0.5.0") and not _newer("", "0.5.0")
# disk-space guard: warn once, the same key again overrides
o_free = gs["free_space"]
gs["free_space"] = lambda p: 5 * (1 << 30)
aps = App(eng=None)
assert aps._space_ok("m1", 1 << 30) and aps._space_ok("m1", 0), "fits / unknown size"
assert not aps._space_ok("m2", 6 * (1 << 30)) and "press again" in aps.status, aps.status
assert aps._space_ok("m2", 6 * (1 << 30)) and not aps._space_warned, "second press overrides"
assert not aps._space_ok("m3", 6 * (1 << 30)) and not aps._space_ok("m4", 6 * (1 << 30)), "other uri re-warns"
gs["free_space"] = lambda p: None
assert aps._space_ok("m5", 10 ** 15), "unknown free space never blocks"
gs["free_space"] = o_free
# enter on a downloads row opens finished items only
opened = []
o_open = gs["open_url"]
gs["open_url"] = lambda u: opened.append(u) or True
apo = App(eng=None)
apo.view = "downloads"
apo.downloads = [Download("g", "Show.S01", "complete", 1, 1, 0, 0, None, root="r", path="/d/Show.S01/E01.mkv")]
apo.on_key("enter")
assert opened == ["/d/Show.S01"] and apo.status.startswith("opened"), (opened, apo.status)
apo.downloads[0].status = "active"
apo.on_key("enter")
assert opened == ["/d/Show.S01"] and "not finished" in apo.status, "active rows don't open"
gs["open_url"] = o_open
# a dropped .torrent path (escaped/quoted as terminals paste it) goes to addTorrent
_tor = Path(tempfile.mkdtemp()) / "My Show.torrent"
_tor.write_bytes(b"d4:infod4:name1:xee")
added = []
class _FileEng:
    def add_torrent_file(self, p): added.append(p)
apf = App(eng=_FileEng())
for pasted in (str(_tor).replace(" ", "\\ ") + " ", f"'{_tor}'", str(_tor)):
    apf.query, apf.editing = pasted, True
    apf.submit()
assert added == [str(_tor)] * 3 and apf.status.startswith("grabbing: My Show.torrent"), (added, apf.status)
apf.query = "not a file.torrent"
apf.submit()
assert added == [str(_tor)] * 3 and apf.search is not None, "a missing path is just a search"
# themes: every palette is complete and renders; color modes degrade cleanly
import re as _re
_vio = T._PALETTES["violet"]
_tb = App(eng=None); _tb.search, _tb.query = object(), "x"
_tb.results = [Result("a" * 40, "Dune.2021.1080p.WEB", 10 ** 9, 5, 1, "yts", "m")]
_tb.downloads = [Download("g", "F", "active", 100, 40, 5, 1, 60.0, root="r")]
for _t in T.THEMES:
    _p = T._PALETTES[_t]
    assert set(_p) == set(_vio) and set(_p["SOURCE_COLOR"]) == set(_vio["SOURCE_COLOR"]), f"{_t}: incomplete palette"
    assert all(_re.fullmatch(r"#[0-9a-f]{6}", v) for k, v in _p.items() if k != "SOURCE_COLOR"), _t
    assert all(_re.fullmatch(r"#[0-9a-f]{6}", v) for v in _p["SOURCE_COLOR"].values()), _t
    T.set_theme(_t)
    for _v in ("search", "downloads"):
        _tb.view = _v
        assert all(dwidth(strip_ansi(x)) <= 100 for x in render(_tb, 100, 30)), f"{_t}/{_v} overflow"
    assert render(App(eng=None), 100, 30), _t
T.set_theme("violet")
_tb.view = "search"
_det = T.detect_color_mode
assert _det({"COLORTERM": "truecolor"}) == "truecolor" and _det({"COLORTERM": "24bit"}) == "truecolor"
assert _det({"TERM_PROGRAM": "iTerm.app"}) == "truecolor" and _det({"TERM": "xterm-kitty"}) == "truecolor"
assert _det({"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal"}) == "256", "no positive signal: 256"
assert _det({}) == "256" and _det({"TERM": "dumb"}) == "none"
assert _det({"COLORTERM": "truecolor", "NO_COLOR": "1"}) == "none" and _det({"NO_COLOR": ""}) == "256"
assert _det({"NO_COLOR": "1", "TRAWL_COLOR": "256"}) == "256" and _det({"TRAWL_COLOR": "bogus"}) == "256"
assert T.rgb_to_256(0, 0, 0) == 16 and T.rgb_to_256(255, 255, 255) == 231
assert T.rgb_to_256(255, 0, 0) == 196 and T.rgb_to_256(0, 255, 0) == 46 and T.rgb_to_256(95, 135, 175) == 67
assert T.rgb_to_256(128, 128, 128) == 244, "greys use the grey ramp"
assert all(16 <= T.rgb_to_256(r, g, b) <= 255 for r in range(0, 256, 51) for g in range(0, 256, 51) for b in range(0, 256, 51))
_om = T.COLOR_MODE
try:
    for _mode, _has, _not in (("truecolor", "38;2;", "38;5;"), ("256", "38;5;", "38;2;"), ("none", "\x1b[1m", "38;")):
        T.COLOR_MODE = _mode
        for _c in (_fg, _logo_lines, _bar_cells, _static_bar):
            _c.cache_clear()
        _raw = "\n".join(render(_tb, 100, 30))
        assert _has in _raw and _not not in _raw, f"{_mode}: expected {_has!r}, never {_not!r}"
    assert _fg("#a78bfa") == "" and style("x", "#a78bfa") == "x", "no color: plain text"
finally:
    T.COLOR_MODE = _om
    for _c in (_fg, _logo_lines, _bar_cells, _static_bar):
        _c.cache_clear()

# live results filter (f) + search cache (replay, expiry, R refresh, never cache failures)
from .sources import SourceUpdate
def _cols(a, rows=30):
    return [strip_ansi(x) for x in render(a, 120, rows)]
_made = []
_canned = {"yts": [Result("1" * 40, "Dune.2021.2160p.WEB", 9 << 30, 90, 1, "yts", "m1"),
                   Result("2" * 40, "Dune Part Two 2024 1080p", 4 << 30, 80, 1, "yts", "m2")],
           "eztv": [Result("3" * 40, "Dune 1984 720p", 1 << 30, 70, 1, "eztv", "m3")]}
_src_log = []
class _FakeSearch:
    def __init__(self, q, sources):
        _made.append(q)
        _src_log.append([s.id for s in sources])
        self.updates, self.total, self.sources = queue.Queue(), len(sources), {s.id: s for s in sources}
        for s in sources:
            u = (SourceUpdate(s.id, None, "boom") if q == "offline" or (q == "bad" and s.id == "yts")
                 else SourceUpdate(s.id, list(_canned.get(s.id, []))))
            u.elapsed = 1.5 if s.id == "yts" else 0.2
            self.updates.put(u)
    def retry(self, ids): return ()
_orig_search, _orig_hist = gs["Search"], gs["save_history"]
gs["Search"], gs["save_history"] = _FakeSearch, lambda h: None
apc = App(eng=None)
apc.query = "dune"; apc.submit(); apc.drain_search()
assert _made == ["dune"] and len(apc.results) == 3 and len(apc._cache) == 1, (_made, len(apc.results))
apc.submit(); apc.drain_search()
assert _made == ["dune"] and len(apc.results) == 3 and "from cache" in apc.status, "second search is replayed"
assert len(apc._cache) == 1 and isinstance(apc.search, Replay)
apc.on_key("R")
assert _made == ["dune", "dune"] and "from cache" not in apc.status, "R bypasses the cache"
apc.drain_search()
_k = next(iter(apc._cache)); apc._cache[_k] = (time.monotonic() - CACHE_TTL - 1, apc._cache[_k][1])
apc.submit()
assert len(_made) == 3, "an expired entry is searched again"
apc.drain_search()
apc.query = "bad"; apc.submit(); apc.drain_search()
assert apc.errors and not any(k[0] == "bad" for k in apc._cache), "a search with a failed source is never cached"
for i in range(CACHE_MAX + 5):
    apc.query = f"q{i}"; apc.submit(); apc.drain_search()
assert len(apc._cache) == CACHE_MAX, "cache is bounded"
apc.query = "dune"; apc.submit(fresh=True); apc.drain_search()
apc.on_key("f")
assert apc.filtering
for ch in "part":
    apc.on_key(ch)
assert [r.name for r in apc.visible_results()] == ["Dune Part Two 2024 1080p"] and apc.sel == 0
assert any("1 of 3" in x for x in _cols(apc)) and "keep" in strip_ansi(_footer(apc, 120))
apc.on_key("enter")
assert not apc.filtering and len(apc.visible_results()) == 1
assert any('1 of 3 match "part"' in x for x in _cols(apc)), "filter stays visible after enter"
apc.on_key("esc")
assert not apc.filter_buf and len(apc.visible_results()) == 3, "esc clears an applied filter"
apc.on_key("f")
for ch in "res:>=1080":
    apc.on_key(ch)
assert sorted(r.name[:4] for r in apc.visible_results()) == ["Dune", "Dune"], "filter takes search operators"
apc.on_key("ctrl-u"); assert apc.filtering and len(apc.visible_results()) == 3, "ctrl-u empties but keeps typing"
for ch in "zzz":
    apc.on_key(ch)
assert any("0 of 3" in x for x in _cols(apc)) and all(dwidth(x) <= 120 for x in _cols(apc))
apc.on_key("enter")
assert any('Nothing matches "zzz"' in x for x in _cols(apc)), "empty applied filter explains itself"
apc.on_key("f")
apc.on_key("backspace"); apc.on_key("backspace"); apc.on_key("backspace")
assert apc.filtering and not apc.filter_buf
apc.on_key("down"); assert apc.sel == 1, "arrows move while typing"
apc.on_key("esc"); assert not apc.filtering
apc.on_key("f"); apc.on_key("d"); apc.query = "dune"; apc.submit()
assert not apc.filter_buf and not apc.filtering, "a new search drops the filter"
apc.results = []; apc.search = None; apc.on_key("f")
assert not apc.filtering, "no filter on the splash"
# quarantine: 3 failed searches in a row pause a source; offline never counts; R lifts it
apq = App(eng=None)
for i in range(3):
    apq.query = "offline"; apq.submit(); apq.drain_search()
assert not apq.fail_streak and not apq.paused_sources(), "a search where everything failed says nothing about sources"
for i in range(3):
    apq.query = "bad"; apq.submit(); apq.drain_search()
    assert apq.fail_streak["yts"] == i + 1 and apq.paused_sources() == (set() if i < 2 else {"yts"}), i
assert apq.fail_streak["eztv"] == 0
apq.query = "bad"; apq.submit()
assert "yts" not in _src_log[-1] and "eztv" in _src_log[-1], "a paused source is skipped"
apq.drain_search()
assert any("1 paused" in x for x in _cols(apq)), "the results panel says a source is paused"
apq.on_key("E")
assert apq.show_errors
_ep = "\n".join(_cols(apq))
assert "paused — failed 3 searches in a row" in _ep and "R searches again" in _ep and "1 paused" in _ep, _ep
apq.on_key("esc"); assert not apq.show_errors
apq.on_key("R")
assert not apq.fail_streak or apq.fail_streak.get("yts") == 1, apq.fail_streak
assert "yts" in _src_log[-1], "R gives paused sources another chance"
apq.drain_search()
apq.query = "dune"; apq.submit(); apq.drain_search()
assert apq.fail_streak["yts"] == 0, "a successful search resets the streak"
# source health panel: slowest first, failures show reason + time
apq.on_key("E")
_ep = _cols(apq)
_yi = next(i for i, x in enumerate(_ep) if "YTS" in x and "1.5s" in x and "2 results" in x)
_ei = next(i for i, x in enumerate(_ep) if "EZTV" in x and "0.2s" in x)
assert _yi < _ei, "slowest source first"
apq.on_key("esc")
apq.query = "bad"; apq.submit(); apq.drain_search(); apq.on_key("E")
_ep = "\n".join(_cols(apq))
assert "✗ YTS  1.5s" in _ep and "boom" in _ep and "1 failed" in _ep, _ep
assert all(dwidth(x) <= 120 for x in _cols(apq))
gs["Search"], gs["save_history"] = _orig_search, _orig_hist

# multi-select: mark rows, batch grab, batch to a folder, guards, modal renders
def _batch_app(fail_on=None):
    a = App(eng=None)
    a.view, a.search, a.query = "search", object(), "x"
    a.results = [Result(f"{i:040x}", f"Show.S01E0{i}.1080p", (i + 1) << 28, 10 - i, 0, "eztv",
                        build_magnet(f"{i:040x}", f"E{i}")) for i in range(5)]
    a.calls = []
    a.grab = lambda m, n, *rest: a.calls.append((n, rest)) or n != fail_on  # type: ignore[method-assign]
    return a
def _cols(a, rows=30):
    return [strip_ansi(x) for x in render(a, 120, rows)]
apm = _batch_app()
apm.on_key(" "); apm.on_key(" ")
assert apm.sel == 2 and [r.name[-9:] for r in apm._marked_results()] == ["E00.1080p", "E01.1080p"], "space marks and steps down"
scr = _cols(apm)
assert sum(x.count(T.DONE) for x in scr if "Show.S01" in x) == 2, "two ✓ on result rows"
assert any("2 marked · 768.00 MB" in x for x in scr) and all(dwidth(x) <= 120 for x in scr)
assert {dwidth(strip_ansi(x)) for x in _results_panel(apm, 80, 8)} == {80}, "marks don't shift the layout"
assert "grab 2" in strip_ansi(_footer(apm, 140)) and "mark" in strip_ansi(_footer(apm, 140))
apm.results = apm.results[::-1]   # a re-sort must not lose marks (keyed by identity)
assert [r.name[-9:] for r in apm._marked_results()] == ["E01.1080p", "E00.1080p"], "marks survive re-sorting"
apm.results = apm.results[::-1]
apm.on_key("d")
assert [c[0][-9:] for c in apm.calls] == ["E00.1080p", "E01.1080p"] and not apm.marked, apm.calls
assert apm.status == "grabbing 2 downloads · 768.00 MB", apm.status
apm.on_key("a"); assert len(apm.marked) == 5
apm.on_key("a"); assert not apm.marked, "a toggles all/none"
apm.on_key(" "); apm.on_key("esc"); assert not apm.marked, "esc clears marks"
apm.on_key(" "); apm.clear(); assert not apm.marked, "clear() drops marks"
apm = _batch_app(); apm.on_key("d")
assert len(apm.calls) == 1 and apm.calls[0][1] == (), "no marks: d grabs the selected row as before"
apm = _batch_app(); apm.on_key(" "); apm.cat = "anime"
apm.on_key("d")
assert not apm.calls and apm._marked_results() == [], "marks hidden by a filter are not grabbed"
apm = _batch_app(fail_on="Show.S01E01.1080p"); apm.on_key(" "); apm.on_key(" "); apm.on_key("d")
assert apm.status.startswith("grabbing 1 of 2"), apm.status
gs["free_space"] = lambda p: 1 << 30   # 1 GiB free; the batch needs 768 MiB plus the 1 GiB margin
apm = _batch_app(); apm.on_key(" "); apm.on_key(" "); apm.on_key("d")
assert not apm.calls and "press again" in apm.status and len(apm.marked) == 2, "one disk check for the batch"
apm.on_key("d")
assert len(apm.calls) == 2 and not apm.marked, "second press overrides"
gs["free_space"] = o_free
# D: one folder for the whole batch; the modal renders (regression: it crashed on unpack)
_bd = tempfile.mkdtemp()
apm = _batch_app(); apm.on_key(" "); apm.on_key(" "); apm.on_key("D")
assert apm.folder_prompt and len(apm.folder_prompt[0]) == 2 and apm.folder_prompt[1] == "2 downloads"
assert any("Download to folder — 2 downloads" in x for x in _cols(apm)), "batch folder modal renders"
apm.folder_buf = os.path.join(_bd, "TV")
apm.on_key("enter")
assert [c[1] for c in apm.calls] == [(os.path.join(_bd, "TV"),)] * 2 and apm.view == "downloads", apm.calls
assert os.path.isdir(os.path.join(_bd, "TV")) and not apm.marked
apm = _batch_app(); apm.on_key("D")
assert apm.folder_prompt[1] == "Show.S01E00.1080p" and any("Download to folder — Show.S01E00" in x for x in _cols(apm))
apm.on_key("esc"); assert apm.folder_prompt is None
apd = _batch_app(); apd.on_key("enter"); apd.on_key("D")   # from the details view
assert apd.folder_prompt and any("Download to folder" in x for x in _cols(apd)), "details D modal renders"
class _AddEng:
    def add(self, uri, opts=None): pass
assert App(eng=None).grab("m", "n") is False and App(eng=_AddEng()).grab("m", "n") is True, "grab reports success"

assert fmt_bytes(1070_000_000) == "1020.4 MB" and len(fmt_bytes(1070_000_000)) <= 9, "just under 1 GiB fits the column"
assert fmt_bytes(953 * 1024 ** 2) == "953.00 MB" and fmt_bytes(1 << 30) == "1.00 GB", "other sizes keep two decimals"
# Torrentio: t in details swaps the list for every known release of the title
_trc = []
def _fake_tr(title, year, kind, sn, en):
    _trc.append((title, year, kind, sn, en))
    if title == "Nothing":
        return []
    if title == "Boom":
        raise SourceError("blocked by Cloudflare's browser check")
    return [Result(f"{i:040x}", f"{title}.{i}.1080p.WEB-DL", (i + 1) << 30, 50 - i, 0, "torrentio", "m", group="Movies")
            for i in range(4)]
_otr = gs["torrentio_releases"]; gs["torrentio_releases"] = _fake_tr
def _tr_app(name, source):
    a = App(eng=None); a.view, a.search, a.query = "search", Replay((), []), "x"
    a.results = [Result("a" * 40, name, 10 ** 9, 5, 1, source, "m")]
    a.on_key("enter"); assert a.detail is not None
    return a
def _tr_wait(a):
    for _ in range(200):
        a.drain_search()
        if not a._other_busy:
            return
        time.sleep(0.01)
    raise AssertionError("Torrentio thread never answered")
apt = _tr_app("Dune.Part.Two.2024.1080p.WEB-DL", "yts")
apt.on_key("t")
assert apt._other_busy and 'asking Torrentio about "Dune Part Two"' in apt.status
apt.on_key("t"); assert apt.status == "still asking Torrentio…"
_tr_wait(apt)
assert _trc == [("Dune Part Two", "2024", "movie", None, None)], _trc
assert apt.detail is None and apt.query == "Dune Part Two" and not apt._other_busy
assert len(apt.results) == 4 and {r.source for r in apt.results} == {"torrentio"}, apt.results
assert apt.status == '4 releases of "Dune Part Two" from Torrentio' and apt.search_done == apt.search_total == 1
scr = _cols(apt)
assert any("TRNT" in x for x in scr) and any("4 results" in x for x in scr) and all(dwidth(x) <= 120 for x in scr)
assert apt._cache_key is None and not apt.marked and not apt.filter_buf
apt.on_key("f"); apt.on_key("w"); apt.on_key("enter")
assert len(apt.visible_results()) == 4, "the live filter works on the releases list"
apt = _tr_app("Severance.S02E05.1080p.WEB.H264-GRP", "eztv"); apt.on_key("t"); _tr_wait(apt)
assert _trc[-1] == ("Severance", None, "series", 2, 5), _trc[-1]
n_before = len(_trc)
for name, src, msg in (("Severance.S02.COMPLETE.1080p", "eztv", "per episode"),
                       ("[Grp] Frieren - 12 [1080p].mkv", "nyaa", "no release lookup for anime"),
                       ("Cyberpunk 2077 [FitGirl Repack]", "fitgirl", "no release lookup for games")):
    apt = _tr_app(name, src); apt.on_key("t")
    assert msg in apt.status and not apt._other_busy and len(_trc) == n_before, (name, apt.status)
apt = _tr_app("Boom.2020.1080p", "yts"); apt.on_key("t"); _tr_wait(apt)
assert apt.status == "Torrentio: blocked by Cloudflare's browser check" and apt.detail is not None, apt.status
apt = _tr_app("Nothing.2020.1080p", "yts"); apt.on_key("t"); _tr_wait(apt)
assert apt.status == 'Torrentio knows no releases of "Nothing"' and apt.detail is not None
apt.on_key("t"); assert apt._other_busy, "a failed lookup can be retried"; _tr_wait(apt)
gs["torrentio_releases"] = _otr

# following shows: w, background checks, the Following overlay (W), auto-grab, guards
from .follow import make_sub
_saved, _toasts, _checks = [], [], []
_og = {k: gs[k] for k in ("save_subs", "check_sub", "notify", "free_space")}
gs["save_subs"] = lambda subs: _saved.append(json.loads(json.dumps(subs)))
gs["notify"] = lambda title, msg: _toasts.append((title, msg))
gs["free_space"] = lambda p: 500 << 30
_fr = lambda n, src="eztv", seeds=10: Result(f"{n:040x}", f"Severance.S02E{n:02d}.1080p.WEB", (n % 5 + 1) << 29, seeds, 0, src, f"m{n}")
def _fake_check(sub, sources, timeout=45.0):
    _checks.append((sub["id"], sorted({s.group for s in sources}), tuple(sub["last"])))
    return ([_fr(6), _fr(7)], 2) if sub["id"] == "severance" else ([], 0)
gs["check_sub"] = _fake_check
def _fol_app(name="Severance.S02E05.1080p.WEB-DL", source="eztv"):
    a = App(eng=None); a.subs = []
    a.view, a.search, a.query = "search", Replay((), []), "severance"
    a.results = [Result("a" * 40, name, 10 ** 9, 5, 1, source, "m")]
    return a
def _fol_wait(a):
    for _ in range(300):
        a.drain_search()
        if not a._sub_busy:
            return
        time.sleep(0.01)
    raise AssertionError("follow check never finished")
apf = _fol_app()
apf.on_key("w")
assert [s["id"] for s in apf.subs] == ["severance"] and apf.subs[0]["last"] == [2, 5] and apf.subs[0]["res"] == 1080
assert "following Severance (1080p)" in apf.status and "after S02E05" in apf.status and _saved[-1][0]["id"] == "severance"
apf.results = [Result("b" * 40, "Severance.S02E08.720p.HDTV", 10 ** 9, 5, 1, "eztv", "m")]
apf.on_key("w")
assert len(apf.subs) == 1 and apf.subs[0]["last"] == [2, 8] and apf.subs[0]["res"] == 720, "following again moves the baseline"
for name, src, msg in (("Dune.2021.1080p.WEB-DL", "yts", "open a TV or anime episode"),
                       ("Severance.S02E05.1080p", "yts", "open a TV or anime episode"),
                       ("Cyberpunk [FitGirl Repack]", "fitgirl", "open a TV or anime episode")):
    a2 = _fol_app(name, src); a2.on_key("w")
    assert not a2.subs and msg in a2.status, (name, a2.status)
a3 = _fol_app(); a3.on_key("enter"); a3.on_key("w")
assert len(a3.subs) == 1 and a3.detail is not None, "w works from the details view too"
# background check: found episodes notify once, the header says so, nothing hammers the sources
apf = _fol_app(); apf.on_key("w"); apf.subs[0]["last"] = [2, 5]
_checks.clear(); _toasts.clear()
apf.start_check(); _fol_wait(apf)
assert _checks == [("severance", ["Other", "TV"], (2, 5))], _checks
assert [r.name[-13:] for r in apf.sub_new["severance"]] == ["E06.1080p.WEB", "E07.1080p.WEB"]
assert apf.subs[0]["checked"] > 0 and apf.new_count() == 2
assert "new episodes: Severance S02E07" in apf.status and _toasts == [("trawl — new episodes", "Severance S02E07")], (apf.status, _toasts)
assert any("2 new episode(s) — W" in x for x in _cols(apf)), "header notice"
apf.start_check(); assert len(_checks) == 1, "not due again for hours"
apf.subs[0]["checked"] = 0.0; apf.start_check(); assert len(_checks) == 1, "retry floor stops back-to-back sweeps"
apf.start_check(force=True); _fol_wait(apf); assert len(_checks) == 2, "force bypasses both"
apf.subs.append({**make_sub("Nothing.S01E01.1080p", "TV")})
apf.start_check(force=True); _fol_wait(apf)
assert [c for c in _checks if c[0] == "nothing"] and apf.subs[1]["checked"] == 0.0, "no source answered: not marked checked"
apf.subs.pop()
# the overlay
apf.on_key("W"); assert apf.following
scr = _cols(apf)
assert any("Following" in x and "(1)" in x for x in scr) and any("Severance 1080p" in x and "S02E05" in x and "2 new" in x for x in scr), scr
assert any("S02E06" in x and "Severance.S02E06.1080p.WEB" in x for x in scr), "new releases listed under the show"
assert all(dwidth(x) <= 120 for x in scr) and "auto-grab" in strip_ansi(_footer(apf, 140))
apf.on_key("ctrl-k"); assert not apf.palette, "palette stays closed inside the overlay"
apf.on_key("down"); apf.on_key("up")
apf.on_key("esc"); assert not apf.following
apf.on_key("W"); apf.on_key("W"); assert not apf.following, "W toggles"
apf.on_key("W"); apf.on_key("enter")
assert not apf.following and apf.view == "search" and apf.query == "Severance" and apf.detail is None
apf.drain_search()
assert [r.name[-13:] for r in apf.results] == ["E06.1080p.WEB", "E07.1080p.WEB"] or len(apf.results) == 2, apf.results
assert "2 new episode(s) of Severance" in apf.status
# grab new: downloads each, baseline moves, list clears
got = []
apf.grab = lambda m, n, *a: got.append(n) or True
apf.on_key("W"); apf.on_key("g")
assert got == ["Severance.S02E06.1080p.WEB", "Severance.S02E07.1080p.WEB"], got
assert apf.subs[0]["last"] == [2, 7] and apf.sub_new["severance"] == [] and "now at S02E07" in apf.status
assert _toasts[-1][0] == "trawl — grabbing new episodes" and _saved[-1][0]["last"] == [2, 7]
apf.on_key("g"); assert "no new episodes" in apf.status and len(got) == 2, "nothing to grab twice"
apf.on_key("enter"); assert "no new episodes" in apf.status and apf.following
# mark seen / auto / unfollow (needs a second x)
apf.sub_new["severance"] = [_fr(9)]
apf.on_key("m")
assert apf.subs[0]["last"] == [2, 9] and not apf.sub_new["severance"] and "marked seen up to S02E09" in apf.status
apf.on_key("a"); assert apf.subs[0]["auto"] is True and _saved[-1][0]["auto"] is True and "automatically" in apf.status
assert any("auto" in x for x in _cols(apf))
apf.on_key("a"); assert apf.subs[0]["auto"] is False
apf.on_key("x"); assert apf.subs and "press x again" in apf.status
apf.on_key("down"); apf.on_key("up"); apf.on_key("x"); assert apf.subs, "moving cancels the confirmation"
apf.on_key("x"); assert not apf.subs and "unfollowed Severance" in apf.status and _saved[-1] == []
assert any("Not following anything yet" in x for x in _cols(apf)) and apf.follow_sel == 0
apf.on_key("c"); assert "not following" in apf.status
apf.on_key("esc")
# auto-grab: a check that finds episodes downloads them without asking
apf = _fol_app(); apf.on_key("w"); apf.subs[0]["auto"] = True; apf.subs[0]["last"] = [2, 5]
got.clear(); apf.grab = lambda m, n, *a: got.append(n) or True; _toasts.clear()
apf.start_check(); _fol_wait(apf)
assert len(got) == 2 and apf.subs[0]["last"] == [2, 7] and apf.new_count() == 0, (got, apf.subs[0])
assert [t[0] for t in _toasts] == ["trawl — grabbing new episodes"], "auto-grab doesn't also announce 'found'"
# ... but never past the disk-space guard
apf = _fol_app(); apf.on_key("w"); apf.subs[0]["auto"] = True; apf.subs[0]["last"] = [2, 5]
got.clear(); apf.grab = lambda m, n, *a: got.append(n) or True
gs["free_space"] = lambda p: 1 << 30
apf.start_check(); _fol_wait(apf)
assert not got and "not enough disk space" in apf.status and apf.subs[0]["last"] == [2, 5] and apf.new_count() == 2
gs["free_space"] = lambda p: 500 << 30
# the palette knows about it
apf = _fol_app(); apf.on_key("ctrl-k")
_pl = [a[0] for a in apf.palette_items()]
assert "Follow this show" in _pl and "Followed shows" in _pl and "Check followed shows now" not in _pl
apf.on_key("esc"); apf.subs = [make_sub("Severance.S02E05.1080p", "TV")]; apf.on_key("ctrl-k")
assert "Check followed shows now" in [a[0] for a in apf.palette_items()]
for ch in "followed":
    apf.on_key(ch)
apf.on_key("enter"); assert apf.following, "palette opens the overlay"
for k, v in _og.items():
    gs[k] = v

# command palette: ctrl-k anywhere, : from nav views, fuzzy find, runs the real actions
assert parse_keys(b"\x0b") == ["ctrl-k"]
assert fuzzy_score("dl", "Download selected") is not None and fuzzy_score("zz", "Download") is None
assert fuzzy_score("", "anything") == 0 and fuzzy_score("DOWN", "download") is not None, "case-insensitive"
assert fuzzy_score("sort", "Cycle sort order") < fuzzy_score("sort", "Hide / show dead torrents, then sort"), "tighter span wins"
apk = _batch_app()
apk.on_key("ctrl-k")
assert apk.palette and apk.pal_buf == ""
_labels = [a[0] for a in apk.palette_items()]
assert "Download selected (or all marked)" in _labels and "Pause / resume" not in _labels, "results context"
assert "Theme: nord" in _labels and f"Theme: {apk.theme}" not in _labels, "the current theme isn't offered"
apk.on_key("q")
assert apk.running and apk.palette and apk.pal_buf == "q", "typing never leaks to normal keys"
apk.on_key("ctrl-u")
for ch in "sort":
    apk.on_key(ch)
assert apk.palette_items()[0][0] == "Cycle sort order"
scr = _cols(apk)
assert any("Commands" in x for x in scr) and any("› sort▌" in x for x in scr) and any("Cycle sort order" in x for x in scr)
apk.on_key("enter")
assert not apk.palette and apk.sort == "size", "enter runs the action"
apk.on_key(":"); assert apk.palette
apk.on_key("esc"); assert not apk.palette
apk.on_key(":")
for ch in "zzzq":
    apk.on_key(ch)
assert apk.palette_items() == [] and any("No matching command" in x for x in _cols(apk))
apk.on_key("enter"); assert not apk.palette, "enter on no match just closes"
for sc in ((100, 30), (140, 40)):
    apk.on_key("ctrl-k")
    assert all(dwidth(strip_ansi(x)) <= sc[0] for x in render(apk, *sc)), f"palette overflow at {sc}"
    apk.on_key("esc")
apk.on_key("ctrl-k")
for sc in ((60, 14), (45, 12), (40, 12)):   # the box itself must fit; the view beneath has its own narrow limit
    assert all(dwidth(strip_ansi(x)) <= sc[0] for x in _palette_box(apk, *sc)), f"palette box overflow at {sc}"
apk.on_key("esc")
apk.on_key("ctrl-k")
for _ in range(80):
    apk.on_key("down")
assert apk.pal_sel == len(apk.palette_items()) - 1, "down clamps at the end"
assert any("Quit" in x for x in _cols(apk)), "the list scrolls to keep the selection visible"
for _ in range(80):
    apk.on_key("up")
assert apk.pal_sel == 0
apk.on_key("pagedown"); assert apk.pal_sel == PALETTE_ROWS
apk.on_key("esc")
# actions do what the keys do
apk = _batch_app(); apk.on_key(":")
for ch in "folder":
    apk.on_key(ch)
apk.on_key("enter"); assert apk.folder_prompt is not None and apk.folder_prompt[1] == "Show.S01E00.1080p"
apk.on_key("ctrl-k"); assert not apk.palette, "ctrl-k is ignored inside a prompt"
apk.on_key("esc")
apk = _batch_app(); apk.on_key(":")
for ch in "movies":
    apk.on_key(ch)
apk.on_key("enter"); assert apk.cat == "movies" and apk.sel == 0
apk.on_key(":")
for ch in "theme nord":
    apk.on_key(ch)
_sv = {}; _osv = gs["save_config"]; gs["save_config"] = lambda c: _sv.update(c)
apk.on_key("enter")
assert apk.theme == "nord" and T.ACCENT == "#88c0d0" and _sv["theme"] == "nord", "theme action persists"
gs["save_config"] = _osv; apk._set_theme("violet")
for word, attr in (("settings", "settings"), ("keyboard help", "help")):
    apk = _batch_app(); apk.on_key(":")
    for ch in word:
        apk.on_key(ch)
    apk.on_key("enter"); assert getattr(apk, attr), word
apk = _batch_app(); apk.on_key(":")
for ch in "quit":
    apk.on_key(ch)
apk.on_key("enter"); assert not apk.running, "quit from the palette (idle)"
# other contexts
apd2 = App(eng=None); apd2.view = "downloads"
apd2.on_key(":")
_dl = [a[0] for a in apd2.palette_items()]
assert "Pause / resume" in _dl and "Download selected (or all marked)" not in _dl and "Show search" in _dl, _dl
apd2.on_key("esc")
aps2 = App(eng=None)           # splash: ":" is just text, ctrl-k opens the palette
aps2.on_key(":"); assert not aps2.palette and aps2.editing and aps2.query == ":"
aps2 = App(eng=None); aps2.on_key("ctrl-k")
assert aps2.palette and any("Commands" in x for x in _cols(aps2)), "palette over the splash"
aps2.on_key("esc"); assert not aps2.palette
aps2.on_key("/"); aps2.on_key("d"); aps2.on_key("ctrl-k")
assert aps2.palette; aps2.on_key("esc")
assert aps2.editing and aps2.query == "d", "ctrl-k mid-typing keeps the query"
apk = _batch_app(); apk.on_key("enter"); apk.on_key("ctrl-k")
_dc = [a[0] for a in apk.palette_items()]
assert "Look inside the torrent" in _dc and "Back to results" in _dc and "Show downloads" not in _dc, _dc
for ch in "back":
    apk.on_key(ch)
apk.on_key("enter"); assert apk.detail is None, "details context: back to results"
assert "ctrl-k" in "\n".join(strip_ansi(l) for l in _help_panel(apk, 140, 70)), "help documents the palette"

# peek inside: metadata-only fetch -> file picker -> grab just the ticked files
_ben2 = lambda x: (b"i%de" % x if isinstance(x, int) else b"%d:%s" % (len(x), x.encode() if isinstance(x, str) else x)
                   if isinstance(x, (str, bytes)) else b"l" + b"".join(map(_ben2, x)) + b"e" if isinstance(x, list)
                   else b"d" + b"".join(_ben2(k) + _ben2(v) for k, v in sorted(x.items())) + b"e")
_pk = _ben2({"info": {"name": "Pack", "piece length": 1, "pieces": b"x" * 20, "files": [
    {"length": 5 << 30, "path": ["a.mkv"]}, {"length": 2 << 30, "path": ["b.mkv"]}, {"length": 10, "path": ["c.srt"]}]}})
_pih = "a" * 40
_pmag = build_magnet(_pih, "Pack")
ev = []
class _PeekEng:
    st = "active"
    def save_metadata(self, uri, d):
        ev.append(("meta", uri))
        self.dir = d
        return "pg"
    def status(self, g): return self.st
    def remove(self, g): ev.append(("remove", g))
    def add(self, uri, opts=None): ev.append(("add", uri, opts))
    def download_dir(self): return None
def _peek_app(res_magnet=_pmag):
    a = App(eng=_PeekEng())
    a.view = "search"
    a.results = [Result(_pih, "Pack", 7 << 30, 9, 1, "yts", res_magnet)]
    a.search, a.sel = object(), 0
    a.on_key("enter")
    assert a.detail is not None
    return a
gs["free_space"] = lambda p: 100 << 30
app_p = _peek_app()
app_p.on_key("f")
assert app_p._peek and app_p.status == "reading file list…" and ev[-1][0] == "meta", ev
app_p.on_key("f")
assert app_p.status == "still reading a file list…" and sum(1 for e in ev if e[0] == "meta") == 1
app_p.update_downloads([Download("g", "[METADATA]Pack", "active", 0, 0, 0, 0, None, root="pg")])
assert not app_p.downloads and app_p.picker is None, "the metadata task never shows as a download row"
app_p.eng.st = "complete"
app_p.update_downloads([])
assert app_p.picker is None and app_p._peek, "complete but .torrent not written yet: keep waiting"
open(os.path.join(app_p.eng.dir, f"{_pih}.torrent"), "wb").write(_pk)
app_p.update_downloads([])
assert app_p.picker is not None and app_p.peek_target and not app_p._peek, "picker opens with the file list"
assert [f["path"] for f in app_p.picker_files] == ["Pack/a.mkv", "Pack/b.mkv", "Pack/c.srt"]
assert app_p.picker_on == {1, 2, 3} and app_p.picker_bytes == (7 << 30) + 10
assert ("remove", "pg") in ev and not os.path.isdir(app_p.eng.dir), "metadata task + temp dir cleaned up"
scr = [strip_ansi(x) for x in render(app_p, 100, 30)]
assert any("a.mkv" in x and "5.00 GB" in x for x in scr) and all(dwidth(x) <= 100 for x in scr)
assert "download ticked" in strip_ansi(_footer(app_p, 100))
app_p.on_key("down"); app_p.on_key(" ")   # untick b.mkv
assert app_p.picker_on == {1, 3}
gs["free_space"] = lambda p: 3 << 30
app_p.on_key("enter")
assert app_p.picker is not None and "press again" in app_p.status, "disk guard uses the ticked size"
gs["free_space"] = lambda p: 100 << 30
app_p.on_key("enter")
assert ev[-1] == ("add", _pmag, {"select-file": "1,3"}), ev[-1]
assert app_p.picker is None and app_p.detail is None and app_p.peek_target is None
# everything ticked = a plain grab; esc backs out to the details view
app_p = _peek_app(); app_p.on_key("f"); app_p.eng.st = "complete"
open(os.path.join(app_p.eng.dir, f"{_pih}.torrent"), "wb").write(_pk)
app_p.update_downloads([])
app_p.on_key("esc")
assert app_p.picker is None and app_p.peek_target is None and app_p.detail is not None
app_p.on_key("f"); app_p.eng.st = "complete"
open(os.path.join(app_p.eng.dir, f"{_pih}.torrent"), "wb").write(_pk)
app_p.update_downloads([]); app_p.on_key("enter")
assert ev[-1] == ("add", _pmag, None), "all files ticked sends no select-file"
# failure paths: timeout, engine error, direct links, single file, user left the details view
app_p = _peek_app(); app_p.on_key("f")
gid, ih, uri, nm, tmp, t0 = app_p._peek
app_p._peek = (gid, ih, uri, nm, tmp, t0 - PEEK_TIMEOUT - 1)
app_p.update_downloads([])
assert not app_p._peek and "couldn't read" in app_p.status and not os.path.isdir(tmp), app_p.status
app_p = _peek_app(); app_p.on_key("f"); app_p.eng.st = "error"; app_p.update_downloads([])
assert not app_p._peek and "couldn't read" in app_p.status
app_p = _peek_app("https://x.org/book.epub"); app_p.on_key("f")
assert app_p.status == "no file list for direct links" and not app_p._peek
app_p = _peek_app(); app_p.on_key("f"); app_p.eng.st = "complete"
open(os.path.join(app_p.eng.dir, f"{_pih}.torrent"), "wb").write(_ben2({"info": {"name": "one.iso", "length": 3 << 20}}))
app_p.update_downloads([])
assert app_p.picker is None and app_p.status.startswith("single file: one.iso"), app_p.status
app_p = _peek_app(); app_p.on_key("f"); app_p.eng.st = "complete"
open(os.path.join(app_p.eng.dir, f"{_pih}.torrent"), "wb").write(_pk)
app_p.on_key("esc"); assert app_p.detail is None
app_p.update_downloads([])
assert app_p.picker is None and not app_p._peek, "result arriving after leaving details is dropped"
app_p = _peek_app(); app_p.on_key("f"); tmp2 = app_p._peek[4]
app_p.end_peek()
assert not app_p._peek and not os.path.isdir(tmp2) and ev[-1] == ("remove", "pg"), "quit cleanup"
gs["free_space"] = o_free
# update notice: header on results views, splash line, settings toggle persists
apu = App(eng=None)
apu.update_tag = "9.9.9"
apu.search, apu.results = object(), apr.results
for scr in (render(apu, 100, 30), render(App(eng=None), 100, 30)):
    assert all(dwidth(strip_ansi(x)) <= 100 for x in scr)
assert "9.9.9 available" in strip_ansi(render(apu, 100, 30)[7]) or any(
    "9.9.9 available" in strip_ansi(x) for x in render(apu, 100, 30)[:9]), "header notice"
apu.search = None
apu.update_tag = "9.9.9"
assert any("brew upgrade trawl" in strip_ansi(x) for x in render(apu, 100, 30)), "splash notice"
apu.down_speed, apu.num_active = 2_000_000, 1
assert any("active" in strip_ansi(x) for x in render(apu, 100, 30)), "splash speed line"
saved_u = {}
o_save = gs["save_config"]
gs["save_config"] = lambda c: saved_u.update(c)
apu.settings = True
apu.set_sel = [k for k, _ in apu.setting_items()].index("updates")
apu._settings_key("enter")
assert apu.update_check is False and saved_u["update_check"] is False, saved_u
apu.update_tag = ""
apu.check_update()
assert apu.update_tag == "", "disabled update check never runs"
gs["save_config"] = o_save
# update_available: one GitHub call a day, failures are silent
_ufile = Path(tempfile.mkdtemp()) / "update.json"
o_ufile, o_fetch = gs["UPDATE_FILE"], gs["fetch_json"]
gs["UPDATE_FILE"] = _ufile
calls_u = []
gs["fetch_json"] = lambda url, **kw: calls_u.append(url) or {"tag_name": "v9.9.9"}
assert update_available() == "9.9.9" and len(calls_u) == 1
assert update_available() == "9.9.9" and len(calls_u) == 1, "cached for a day"
_ufile.write_text(json.dumps({"ts": 0, "tag": "v0.0.1"}))
assert update_available() == "9.9.9" and len(calls_u) == 2, "stale cache refetches"
_ufile.unlink()
def _boom(url, **kw): raise SourceError("offline")
gs["fetch_json"] = _boom
assert update_available() == "", "offline: no notice, no crash"
gs["UPDATE_FILE"], gs["fetch_json"] = o_ufile, o_fetch
print("render ok")
print("\nPhase 3 selftest passed.")



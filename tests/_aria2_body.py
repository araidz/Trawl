"""Exercise the engine end to end without depending on swarm health.

ponytail: deliberately does NOT wait for real download bytes (that needs a
live swarm + burns bandwidth). It proves spawn/RPC/add/poll/remove/shutdown
and the pure metadata/eta mapping. Real progress is verified in the TUI E2E.
"""
# pure logic — no network
meta = to_download({"gid": "a", "status": "active", "totalLength": "0",
                    "completedLength": "0", "downloadSpeed": "0",
                    "files": [{"path": "/x/[METADATA]Some.Movie"}]})
assert meta.status == "metadata" and meta.name == "Some.Movie", meta
live = to_download({"gid": "b", "status": "active", "totalLength": "100",
                   "completedLength": "50", "downloadSpeed": "10",
                   "connections": "7", "files": [{"path": "/x/Some.Movie.mkv"}]})
assert live.progress == 0.5 and live.eta == 5.0 and live.peers == 7, live
assert _follow({"status": "complete", "followedBy": ["z"]}) == "z"
assert _follow({"status": "active", "followedBy": ["z"]}) is None
# add_torrent_file: base64 blob -> aria2.addTorrent, tracked like any root
import tempfile as _tf3
_tp = os.path.join(_tf3.mkdtemp(), "x.torrent")
open(_tp, "wb").write(b"d4:infod4:name1:xee")
_tm = Aria2(conf=None)
_tc = []
_tm._call = lambda method, params=None, **k: _tc.append((method, params)) or "gid1"  # type: ignore[method-assign]
assert _tm.add_torrent_file(_tp) == "gid1" and _tm.roots == ["gid1"]
assert _tc[0][0] == "aria2.addTorrent" and base64.b64decode(_tc[0][1][0]) == b"d4:infod4:name1:xee", _tc
assert _tc[0][1][1:] == [[], {}], "no web seeds, no options"
# torrent_files: aria2's own file numbering, padding hidden, junk rejected
def _ben(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, (bytes, str)):
        x = x.encode() if isinstance(x, str) else x
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(map(_ben, x)) + b"e"
    return b"d" + b"".join(_ben(k) + _ben(v) for k, v in sorted(x.items())) + b"e"
_pack = _ben({"info": {"name": "Pack", "piece length": 16384, "pieces": b"x" * 20, "files": [
    {"length": 100, "path": ["S01", "E01.mkv"]},
    {"length": 5, "path": [".pad", "5"], "attr": "p"},
    {"length": 200, "path": ["S01", "E02 é.mkv"]}]}})
assert torrent_files(_pack) == [
    {"index": 1, "path": "Pack/S01/E01.mkv", "length": 100, "selected": True},
    {"index": 3, "path": "Pack/S01/E02 é.mkv", "length": 200, "selected": True}], torrent_files(_pack)
assert torrent_files(_ben({"info": {"name": "one.iso", "length": 42}})) == [
    {"index": 1, "path": "one.iso", "length": 42, "selected": True}]
for junk in (b"", b"garbage", _pack[:-5], b"d4:infoi1ee", _ben({"info": {"name": "x"}}),
             b"l" * 50000, b"99999999999:x", _ben({"info": {"name": "x", "files": [{"length": 1}]}})):
    try:
        torrent_files(junk)
        raise AssertionError(f"accepted junk: {junk[:20]!r}")
    except ValueError:
        pass
# poll batches via system.multicall: one steady call, root+child on handoff
mock = Aria2(conf=None)
mock.roots = ["root"]
mock._resolved = {"root": "root"}
calls = []
steady = [{"gid": "root", "status": "active", "totalLength": "1",
           "completedLength": "0", "files": []}]
mock._call = lambda method, params=None, **k: calls.append((method, params)) or steady  # type: ignore[method-assign]
rows = mock.poll()
assert len(calls) == 1 and calls[0][0] == "system.multicall", calls
assert len(calls[0][1][0]) == 1, "one batched tellStatus per root"
assert calls[0][1][0][0]["methodName"] == "aria2.tellStatus"
assert rows[0].gid == "root" and rows[0].root == "root"
calls.clear()
def _batched_gid(params):
    return params[0][0]["params"][1]  # token, gid, fields
def batched(method, params=None, statuses=None, **k):
    calls.append((method, params))
    return [statuses[_batched_gid(params)]]
mock._call = lambda method, params=None, statuses={"root": {"gid": "root", "status": "complete",
        "followedBy": ["child"], "files": []}, "child": {"gid": "child", "status": "active",
        "totalLength": "10", "completedLength": "2", "files": [{"path": "/x/movie"}]}}, **k: \
    batched(method, params, statuses, **k)  # type: ignore[method-assign]
rows = mock.poll()
assert [_batched_gid(c[1]) for c in calls] == ["root", "child"], calls
assert rows[0].gid == "child" and rows[0].root == "root" and mock._resolved["root"] == "child", rows[0]
# A vanished/erroring metadata child is skipped, while file APIs follow a
# successful metadata handoff before asking for the child file list.
missing = Aria2(conf=None)
missing.roots = ["root"]
missing._resolved = {"root": "root"}
def missing_call(method, params=None, **k):
    gid = _batched_gid(params)
    if gid == "root":
        return [{"gid": "root", "status": "complete",
                 "followedBy": ["gone"], "files": []}]
    raise Aria2Error("missing child")
missing._call = missing_call  # type: ignore[method-assign]
assert missing.poll() == [], "missing metadata child should not escape poll"
assert missing._resolved["root"] == "root", "failed child must preserve last valid gid"

broken_root = Aria2(conf=None)
broken_root.roots = ["root"]
broken_root._resolved = {"root": "root"}
broken_root._call = lambda *a, **k: (_ for _ in ()).throw(Aria2Error("gone"))  # type: ignore[method-assign]
assert broken_root.poll() == [] and broken_root._resolved["root"] == "root"

failed = Aria2(conf=None)
failed.roots = ["root"]
failed._resolved = {"root": "root"}
def failed_call(method, params=None, **k):
    gid = _batched_gid(params)
    st = {"gid": "child", "status": "error", "errorMessage": "disk full", "files": []} \
        if gid == "child" else {"gid": "root", "status": "complete",
                                "followedBy": ["child"], "files": []}
    return [st]
failed._call = failed_call  # type: ignore[method-assign]
failed_row = failed.poll()[0]
assert failed_row.gid == "child" and failed_row.root == "root"
assert failed_row.status == "error" and failed_row.error == "disk full"

handed = Aria2(conf=None)
handed._resolved = {"root": "root"}
hand_calls = []
child_files = [{"index": "1", "path": "/x/movie.mkv", "length": "12", "selected": "true"}]
def hand_call(method, params=None):
    hand_calls.append(params)
    if params[0] == "root":
        return {"gid": "root", "status": "complete", "followedBy": ["child"]}
    return {"gid": "child", "status": "active", "files": child_files}
handed._call = hand_call  # type: ignore[method-assign]
assert handed.files("root") == [{"index": 1, "path": "/x/movie.mkv", "length": 12,
                                  "selected": True}]
assert handed._resolved["root"] == "child" and hand_calls[-1][0] == "child", hand_calls
assert handed.file_paths("root") == ["/x/movie.mkv"] and hand_calls[-1][0] == "child"
# *.aria2 control-file infohash parse (synthetic, documented big-endian format)
import tempfile as _tf
ih = "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
blob = (1).to_bytes(2, "big") + (1).to_bytes(4, "big") + (20).to_bytes(4, "big") + bytes.fromhex(ih) + b"\x00" * 8
p = _tf.mktemp(suffix=".aria2")
with open(p, "wb") as _f:
    _f.write(blob)
assert control_infohash(p) == ih, control_infohash(p)
with open(p, "wb") as _f:  # HTTP control file (no BT bit) -> skipped
    _f.write((1).to_bytes(2, "big") + (0).to_bytes(4, "big") + b"\x00" * 24)
assert control_infohash(p) is None
os.remove(p)
print("pure mapping ok")

# live plumbing — temp state, temp dir, no user conf, removed before any bytes
import tempfile
tmp = Path(tempfile.mkdtemp(prefix="trawl-selftest-"))
eng = Aria2(conf=None, state_dir=tmp)
with eng:
    ver = eng._call("aria2.getVersion")
    print(f"aria2 {ver['version']} up on :{eng.port}")
    assert "numActive" in eng.global_stat()
    # Big Buck Bunny — a real, valid infohash; removed immediately, no download.
    magnet = ("magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
              "&dn=trawl-selftest")
    root = eng.add(magnet, {"dir": str(tmp)})
    assert root, "addUri returned no gid"
    rows = eng.poll()
    assert any(d.gid for d in rows), f"added magnet not listed: {rows}"
    print(f"added + polled ok ({len(rows)} row, status={rows[0].status})")
    # retry: re-adds from the remembered uri under a new gid
    new = eng.retry(root)
    assert new and new != root and eng._uris[new][0] == magnet, (new, root)
    assert root not in eng.roots and new in eng.roots
    print("retry ok")
    eng.remove(new)
    assert eng.poll() == [], "remove left a row behind"
    print("remove ok")
assert eng.proc.poll() is not None, "aria2c did not shut down"
print("shutdown ok")

# session resume: a paused download saved on shutdown is adopted on restart
eng2 = Aria2(conf=None, state_dir=tmp)
with eng2:
    magnet2 = ("magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
               "&dn=trawl-resume-test")
    r2 = eng2.add(magnet2, {"dir": str(tmp), "pause": "true"})
    eng2._call("aria2.saveSession")
eng3 = Aria2(conf=None, state_dir=tmp)
with eng3:
    assert eng3.roots, "restart did not adopt the saved session download"
    assert eng3.files(eng3.roots[0]) is not None  # files() tolerates metadata state
    for r in list(eng3.roots):
        eng3.remove(r)
print("session resume ok")
print("\nPhase 1 selftest passed.")



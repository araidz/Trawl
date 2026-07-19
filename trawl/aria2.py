"""aria2 engine: spawn a private aria2c and drive it over JSON-RPC.

Forces only the RPC keys plus a trawl-private session file; everything else
(download dir, splits, leech-only seed-time=0, resume) is inherited from the
user's ~/.aria2/aria2.conf. Transliterated from Riptide's Aria2Client.swift.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from shutil import which

USER_CONF = Path.home() / ".aria2" / "aria2.conf"
STATE_DIR = Path.home() / "Library" / "Application Support" / "Trawl"

_METADATA = "[METADATA]"


class Aria2Error(Exception):
    pass


@dataclass
class Download:
    """One tracked download, mapped from aria2's tellStatus for the UI."""

    gid: str
    name: str
    status: str  # active | waiting | paused | complete | error | metadata
    total: int
    completed: int
    speed: int
    peers: int
    eta: float | None  # seconds remaining, None when unknown
    error: str = ""
    root: str = ""  # the gid we added (poll sets it); remove() takes this
    path: str = ""  # on-disk path of the first file (for reveal-in-Finder)

    @property
    def progress(self) -> float:
        return self.completed / self.total if self.total else 0.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _name(st: dict) -> str:
    info = (st.get("bittorrent") or {}).get("info") or {}
    if info.get("name"):
        return info["name"]
    files = st.get("files") or []
    if files and files[0].get("path"):
        return os.path.basename(files[0]["path"])
    return st.get("gid", "?")


def _follow(st: dict) -> str | None:
    """The gid a finished metadata task hands off to, else None. Pure (testable)."""
    fb = st.get("followedBy") or []
    return fb[0] if st.get("status") == "complete" and fb else None


def to_download(st: dict) -> Download:
    """Map an aria2 tellStatus dict to a Download. Pure (testable)."""
    total = int(st.get("totalLength") or 0)
    completed = int(st.get("completedLength") or 0)
    speed = int(st.get("downloadSpeed") or 0)
    status = st.get("status") or ""
    name = _name(st)
    if name.startswith(_METADATA):  # magnet still resolving its .torrent
        name = name[len(_METADATA):] or "fetching metadata"
        status = "metadata"
    eta = (total - completed) / speed if speed > 0 and total > completed else None
    return Download(
        gid=st.get("gid", "?"),
        name=name,
        status=status,
        total=total,
        completed=completed,
        speed=speed,
        peers=int(st.get("connections") or 0),
        eta=eta,
        error=st.get("errorMessage", ""),
        path=(st.get("files") or [{}])[0].get("path", ""),
    )


def control_infohash(path: str) -> str | None:
    """Read the BitTorrent infohash (40-hex) from an aria2 *.aria2 control file.

    aria2's DefaultBtProgressInfoFile format, big-endian (network byte order):
      [0:2] version  [2:6] extension (bit0 => BT)  [6:10] infoHashLen (=20)  [10:30] infoHash
    Returns None for non-BT / unrecognised files. ponytail: parses the documented
    BT prefix only; validate against a real .aria2 if aria2's format ever changes.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(30)
    except OSError:
        return None
    if len(head) < 30:
        return None
    version = int.from_bytes(head[0:2], "big")
    ext = int.from_bytes(head[2:6], "big")
    ih_len = int.from_bytes(head[6:10], "big")
    if version not in (0, 1) or not (ext & 1) or ih_len != 20:
        return None
    return head[10:30].hex()


class Aria2:
    TIMEOUT = 5  # seconds per RPC call

    def __init__(self, conf: Path | None = USER_CONF, state_dir: Path = STATE_DIR):
        self.port = _free_port()
        self.secret = secrets.token_hex(8)
        self.endpoint = f"http://127.0.0.1:{self.port}/jsonrpc"
        self.conf = Path(conf) if conf else None
        self.state_dir = Path(state_dir)
        self.session = self.state_dir / "aria2-session.txt"
        self.proc: subprocess.Popen | None = None
        self.roots: list[str] = []  # gids we added, in add order
        self._resolved: dict[str, str] = {}  # root gid -> current effective gid
        self._uris: dict[str, tuple[str, dict]] = {}  # root gid -> (uri, options) for retry

    # -- lifecycle -----------------------------------------------------------

    def start(self, timeout: float = 10.0) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        args = [
            _binary(),
            "--enable-rpc",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={self.secret}",
            "--rpc-listen-all=false",
            f"--save-session={self.session}",  # private: never touch the user's session
            "--save-session-interval=30",  # survive crashes, not just clean quits
        ]
        if self.session.is_file():  # auto-resume last session's unfinished downloads
            args.append(f"--input-file={self.session}")
        if self.conf and self.conf.is_file():
            args.append(f"--conf-path={self.conf}")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._call("aria2.getVersion")
                self._adopt()
                return
            except Aria2Error:
                if self.proc.poll() is not None:
                    raise Aria2Error("aria2c exited during startup")
                time.sleep(0.1)
        raise Aria2Error("aria2c RPC did not come up")

    def stop(self) -> None:
        try:
            self._call("aria2.shutdown")
        except Aria2Error:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                self.proc.kill()

    def __enter__(self) -> "Aria2":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- commands ------------------------------------------------------------

    def _adopt(self) -> None:
        """Track downloads aria2 restored from the session file at startup."""
        for method, params in (("aria2.tellActive", [["gid"]]),
                               ("aria2.tellWaiting", [0, 1000, ["gid"]])):
            try:
                for t in self._call(method, params) or []:
                    gid = t.get("gid")
                    if gid and gid not in self.roots:
                        self.roots.append(gid)
                        self._resolved[gid] = gid
            except Aria2Error:
                pass

    def add(self, magnet: str, options: dict | None = None) -> str:
        gid = self._call("aria2.addUri", [[magnet], options or {}])
        self.roots.append(gid)
        self._resolved[gid] = gid
        self._uris[gid] = (magnet, options or {})
        return gid

    def remove(self, root: str) -> None:
        gid = self._resolved.get(root, root)
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                self._call(method, [gid])
            except Aria2Error:
                pass
        if root in self.roots:
            self.roots.remove(root)
        self._resolved.pop(root, None)
        self._uris.pop(root, None)

    def retry(self, root: str) -> str | None:
        """Remove a failed download and re-add it from its original uri. Adopted
        session downloads fall back to a bare magnet from the infohash (DHT).
        Returns the new root gid, or None if there's nothing to re-add from."""
        uri, opts = self._uris.get(root, ("", {}))
        if not uri:
            try:
                st = self._call("aria2.tellStatus",
                                [self._resolved.get(root, root), ["infoHash", "bittorrent"]])
            except Aria2Error:
                st = {}
            ih = st.get("infoHash") or ""
            if not ih:
                return None
            name = ((st.get("bittorrent") or {}).get("info") or {}).get("name", ih)
            uri = f"magnet:?xt=urn:btih:{ih}&dn={urllib.parse.quote(name)}"
        self.remove(root)
        try:
            return self.add(uri, opts)
        except Aria2Error:
            return None

    def pause(self, root: str) -> None:
        # forcePause stops a BT download immediately (no tracker round-trip)
        try:
            self._call("aria2.forcePause", [self._resolved.get(root, root)])
        except Aria2Error:
            pass

    def resume(self, root: str) -> None:
        try:
            self._call("aria2.unpause", [self._resolved.get(root, root)])
        except Aria2Error:
            pass

    def global_stat(self) -> dict:
        return self._call("aria2.getGlobalStat")

    def download_dir(self) -> str | None:
        try:
            return self._call("aria2.getGlobalOption").get("dir")
        except Aria2Error:
            return None

    def set_dir(self, path: str) -> None:
        try:
            self._call("aria2.changeGlobalOption", [{"dir": path}])
        except Aria2Error:
            pass

    def active_infohashes(self) -> set[str]:
        """infohashes aria2 already has in flight (so a scan never double-adds)."""
        have: set[str] = set()
        for method, params in (("aria2.tellActive", [["infoHash"]]),
                               ("aria2.tellWaiting", [0, 1000, ["infoHash"]])):
            try:
                for t in self._call(method, params) or []:
                    ih = (t.get("infoHash") or "").lower()
                    if ih:
                        have.add(ih)
            except Aria2Error:
                pass
        return have

    def poll(self) -> list[Download]:
        """Current state of every tracked download, metadata->real gid resolved."""
        out: list[Download] = []
        for root in list(self.roots):
            try:
                st = self._call("aria2.tellStatus", [self._resolve(root)])
            except Aria2Error:
                continue
            d = to_download(st)
            d.root = root
            out.append(d)
        return out

    def files(self, root: str) -> list[dict]:
        """A download's file list for the picker: index, path, length, selected."""
        try:
            st = self._call("aria2.tellStatus", [self._resolve(root), ["files"]])
        except Aria2Error:
            return []
        return [{"index": int(f.get("index") or 0), "path": f.get("path", ""),
                 "length": int(f.get("length") or 0), "selected": f.get("selected") == "true"}
                for f in (st.get("files") or [])]

    def select_files(self, root: str, indices: list[int]) -> bool:
        """Restrict a BT download to the given 1-based file indices."""
        try:
            self._call("aria2.changeOption",
                       [self._resolved.get(root, root),
                        {"select-file": ",".join(str(i) for i in sorted(indices))}])
            return True
        except Aria2Error:
            return False

    def file_paths(self, root: str) -> list[str]:
        """On-disk paths of a download's files (for delete-on-cancel)."""
        try:
            st = self._call("aria2.tellStatus", [self._resolve(root), ["files"]])
        except Aria2Error:
            return []
        return [f.get("path", "") for f in (st.get("files") or []) if f.get("path")]

    # -- internals -----------------------------------------------------------

    def _resolve(self, root: str) -> str:
        gid = self._resolved.get(root, root)
        for _ in range(8):  # ponytail: cap the walk; a magnet is one hop
            st = self._call("aria2.tellStatus", [gid, ["status", "followedBy"]])
            nxt = _follow(st)
            if nxt is None:
                break
            gid = nxt
        self._resolved[root] = gid
        return gid

    def _call(self, method: str, params: list | None = None):
        payload = {
            "jsonrpc": "2.0",
            "id": "trawl",
            "method": method,
            "params": [f"token:{self.secret}", *(params or [])],
        }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Aria2Error(f"{method}: {e}") from e
        if isinstance(body, dict) and body.get("error"):
            raise Aria2Error(f"{method}: {body['error'].get('message', '?')}")
        return body.get("result")


def _binary() -> str:
    path = which("aria2c")
    if not path:
        raise Aria2Error("aria2c not found on PATH (brew install aria2)")
    return path


# -- self-check --------------------------------------------------------------


def selftest() -> None:
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


if __name__ == "__main__":
    selftest()

"""aria2 engine: spawn a private aria2c and drive it over JSON-RPC.

Forces only the RPC keys plus a trawl-private session file; everything else
(download dir, splits, leech-only seed-time=0, resume) is inherited from the
user's ~/.aria2/aria2.conf. Transliterated from Riptide's Aria2Client.swift.
"""

from __future__ import annotations

import base64
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

    def add_torrent_file(self, path: str, options: dict | None = None) -> str:
        """Add a local .torrent (addUri can't read files)."""
        with open(path, "rb") as f:
            blob = base64.b64encode(f.read()).decode()
        gid = self._call("aria2.addTorrent", [blob, [], options or {}])
        self.roots.append(gid)
        self._resolved[gid] = gid
        return gid

    def save_metadata(self, magnet: str, dir_path: str) -> str:
        """Fetch a magnet's metadata only, writing <infohash>.torrent into
        dir_path with no content download. Returns the root gid."""
        opts = {"bt-metadata-only": "true", "bt-save-metadata": "true", "dir": dir_path}
        return self.add(magnet, opts)

    def status(self, root: str) -> str:
        """Raw aria2 status of a tracked download ('' if it can't be queried)."""
        try:
            st = self._call("aria2.tellStatus", [self._resolve(root), ["status"]])
            return st.get("status", "")
        except Aria2Error:
            return ""

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

    def set_limit(self, value: str) -> None:
        """Overall download speed cap per aria2 (e.g. "2M"); "0" = unlimited."""
        try:
            self._call("aria2.changeGlobalOption", [{"max-overall-download-limit": value}])
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

    def active_uris(self) -> set[str]:
        """URIs aria2 is already fetching (http(s) adds; magnets have none)."""
        have: set[str] = set()
        for method, params in (("aria2.tellActive", [["files"]]),
                               ("aria2.tellWaiting", [0, 1000, ["files"]])):
            try:
                for t in self._call(method, params) or []:
                    for f in t.get("files") or []:
                        for u in f.get("uris") or []:
                            if u.get("uri"):
                                have.add(u["uri"])
            except Aria2Error:
                pass
        return have

    def poll(self) -> list[Download]:
        """Current state of every tracked download, batched: one
        system.multicall per metadata-handoff round instead of one RPC per
        download. Per-call errors drop that row for the tick, like before."""
        roots = list(self.roots)
        if not roots:
            return []
        fields = ["gid", "status", "totalLength", "completedLength", "downloadSpeed",
                  "connections", "errorMessage", "files", "bittorrent", "followedBy"]
        statuses: dict[str, dict] = {}
        proposed: dict[str, str] = {}  # advances committed only on a terminal status
        pending = roots
        for _ in range(8):  # ponytail: cap the walk; a magnet is one hop
            try:
                results = self._call("system.multicall", [[
                    {"methodName": "aria2.tellStatus",
                     "params": [f"token:{self.secret}",
                                proposed.get(r, self._resolved.get(r, r)), fields]}
                    for r in pending]], token=False) or []
            except Aria2Error:
                break  # no terminal status -> no commits, last valid gids stand
            next_pending = []
            for root, res in zip(pending, results):
                if isinstance(res, list) and len(res) == 1:
                    res = res[0]  # aria2 wraps each success in a one-element list
                st = res if isinstance(res, dict) and "code" not in res else None
                if st is None:
                    continue  # per-call error: row dropped this tick
                nxt = _follow(st)
                if nxt is not None:
                    proposed[root] = nxt
                    next_pending.append(root)
                else:
                    statuses[root] = st
            pending = next_pending
            if not pending:
                break
        for root, gid in proposed.items():
            if root in statuses:
                self._resolved[root] = gid
        out = []
        for root in roots:
            st = statuses.get(root)
            if st is None:
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
        return self._resolved_status(root, ["status", "followedBy"])[1]

    def _resolved_status(self, root: str, fields: list[str] | None = None) -> tuple[dict, str]:
        gid = self._resolved.get(root, root)
        for _ in range(8):  # ponytail: cap the walk; a magnet is one hop
            params = [gid, fields] if fields else [gid]
            st = self._call("aria2.tellStatus", params)
            nxt = _follow(st)
            if nxt is None:
                break
            gid = nxt
        else:
            params = [gid, fields] if fields else [gid]
            st = self._call("aria2.tellStatus", params)
        self._resolved[root] = gid
        return st, gid

    def _call(self, method: str, params: list | None = None, *, token: bool = True):
        # system.multicall carries no outer token: each inner struct embeds its own.
        payload = {
            "jsonrpc": "2.0",
            "id": "trawl",
            "method": method,
            "params": ([f"token:{self.secret}"] if token else []) + (params or []),
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

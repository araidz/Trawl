# Trawl — Project Plan

> This is the **original design plan**. Some details evolved during build (e.g.
> resume is now triggered by `s` rather than on launch, and several "deferred"
> items shipped). See [README](README.md) for the current feature set.

A torrent finder that lives in the terminal: one search trawls a curated list of
sources at once, you pick a result, and **aria2** downloads it. A native-feeling
single command (`trawl`) — no Node, no `npx`, no install step. Looks and acts like
[torlink](https://github.com/baairon/torlink); driven by aria2 like its sibling
[Riptide](../Archive/Riptide).

- **Command:** `trawl`
- **Language:** Python 3, **stdlib only — zero third-party packages** (hard rule).
- **Engine:** the system `aria2c` (already installed), driven over JSON-RPC.
- **Target:** macOS (Python 3.14 + aria2 already present).
- **Lineage:** torlink (the finder/TUI to emulate) + Riptide (the proven aria2
  integration to transliterate from Swift).

---

## North star

1. **Look like torlink** — violet truecolor theme, logo header, source-tagged
   results, the animated cosine-bell "sheen" progress bar, keyboard-driven panes.
2. **Act like torlink** — type to search, results stream in from every source,
   `d` to download, a Downloads pane with live progress/speed/ETA.
3. **Feel like omp** — type `trawl`, the app starts in the terminal. No runtime to
   install; ships as one executable on `PATH`.
4. **Minimum codebase, YAGNI** — stdlib only, fewest files, no speculative
   abstractions, delete anything not pulling its weight.

---

## Decisions (settled)

| Topic | Decision | Why |
|---|---|---|
| Language | Python 3, stdlib only | Already installed; `curses`/`urllib`/`json`/`re` cover everything; zero installs |
| Engine | spawn private `aria2c` + JSON-RPC | aria2 already installed & used; Riptide already proved the integration |
| Renderer | **raw ANSI full-redraw** (not curses) | Truecolor violet + sheen need 24-bit color, which curses fights |
| Distribution | `python -m zipapp` → single `trawl` executable on `PATH` | Multi-file source, one-file shippable, stdlib, "type the command" |
| Download dir | inherit from `~/.aria2/aria2.conf` (`~/Downloads`) | Conf already sets it; no separate config needed for v1 |
| aria2 conf | load user's `~/.aria2/aria2.conf` as base | Resume, splits, leech-only already configured there |
| Seeding | **none** | User's conf is `seed-time=0` (leech-only); no seeding UI to build |
| Sources | all 10 from torlink | Yes-to-all |

---

## Architecture

Small package, shipped as one file:

```
Trawl/
  trawl/
    __main__.py   # entry: magnet arg, raw-mode setup/restore, the run loop, key dispatch
    aria2.py      # spawn aria2c + JSON-RPC client + add/poll/remove + status mapping
    sources.py    # 10 scrapers + buildMagnet/parseMagnet + concurrent search/merge
    tui.py        # frame render (panes, lists, search field, sheen bar) + key parsing
    theme.py      # COLOR, glyphs, logo, sheen math  <- the single "look" knob
  plan.md
  .gitignore
```

Split `sources.py` only if 1337x bloats it; collapse a file if it stays tiny.
Build: `sh build.sh` → `dist/trawl` (single 63K stdlib zipapp). Install with the
symlink it prints: `ln -sf "$PWD/dist/trawl" /usr/local/bin/trawl`. (`build/`,
`dist/` are gitignored. The script nests the package under an absolute-import
launcher so relative imports resolve inside the archive.)

### Data flow

```
keys ─▶ __main__ run loop ─▶ tui.render(state)
                  │
   search query ──┼─▶ sources.search() ──(ThreadPoolExecutor)──▶ queue.Queue ─▶ drained each tick
                  │
   d / magnet  ───┴─▶ aria2.add() ─▶ aria2c (JSON-RPC) ─▶ aria2.poll() every 500ms ─▶ state
```

Single-threaded loop; the only background work is the search thread pool pushing
results onto a `queue.Queue` the loop drains. aria2 does all the actual downloading
in its own process.

---

## The aria2 seam (`aria2.py`)

### Spawn (force only what we must; inherit the rest)

```
aria2c --enable-rpc \
       --rpc-listen-port=<free port> \
       --rpc-secret=<random> \
       --rpc-listen-all=false \
       --conf-path=<~/.aria2/aria2.conf if present> \
       --save-session=<TRAWL_STATE/aria2-session.txt> \
       --input-file=<TRAWL_STATE/aria2-session.txt if it exists>
```

- **Forced:** the RPC keys (so we can connect) + a **trawl-private session file**
  (so it never corrupts the user's own `~/.aria2/aria2-session.txt` if they run
  aria2 elsewhere).
- **Inherited from conf:** `dir`, `continue`/`always-resume` (free resume),
  splits/connections, timeouts, `seed-time=0` (leech-only), quiet logging.
- Pick a free RPC port (`socket` bind to `:0`), random secret (`secrets.token_hex`).
- `TRAWL_STATE = ~/Library/Application Support/Trawl/` (created on first run).
- On quit: `aria2.shutdown` RPC, then terminate the process; restore the terminal.

### JSON-RPC client

Transliterate Riptide's `Aria2Client.swift` (~120 lines) to `urllib.request` POST +
`json` (~80 lines). Methods needed for v1:

`addUri`, `tellStatus`, `tellActive`, `tellStopped`, `forceRemove`, `getGlobalStat`,
`shutdown`. (Pause/seeding methods omitted — YAGNI.)

### Status mapping (`tellStatus` → display)

| aria2 field | display |
|---|---|
| `completedLength` / `totalLength` | progress fraction (sheen bar) |
| `downloadSpeed` | speed |
| `(total − completed) / downloadSpeed` | ETA |
| `connections`, `numSeeders` | peers |
| `status` | active / waiting / complete / error |
| `bittorrent.info.name` or `files[0].path` | name |

### The metadata gotcha (designed for up front — it bit Riptide)

Adding a magnet first creates a `[METADATA]` task (gid **A**). When metadata
resolves, aria2 spawns the real download under a **new gid B** and links it via
`tellStatus(A).followedBy`. So:

1. On add, show a **"fetching metadata…"** row immediately (don't wait, don't blank).
2. Follow `followedBy` from A to B; track B from then on.

Root-cause-correct; avoids torlink/Riptide's "nothing appears for ~10s" symptom.

---

## Sources (`sources.py`)

All 10 from torlink, merged and sorted by seeders, streaming in as each answers.
Grounded in the actual torlink scrapers:

| Source | Group | Transport | Endpoint / shape | Python |
|---|---|---|---|---|
| YTS | Movies | JSON | `yts.mx/api/v2/list_movies.json` (host failover) | `urllib`+`json` |
| The Pirate Bay (×2) | Movies/TV | JSON | `apibay.org/q.php` + precompiled top100; category filter | `json` |
| EZTV | TV | JSON | `eztvx.to/api/get-torrents` (browse-only) | `json` |
| SolidTorrents | TV | JSON | `solidtorrents.net/api/v1/search` | `json` |
| SubsPlease | Anime | JSON | `subsplease.org/api/?f=search\|latest` (object-keyed) | `json` |
| FitGirl | Games | RSS | `fitgirl-repacks.site/?s=q&feed=rss2` | `re` + `html.unescape` |
| Nyaa | Anime | RSS | `nyaa.si/?page=rss` (`nyaa:` namespaced tags) | `re` + `html.unescape` |
| 1337x (×2) | Movies/TV | **HTML, two-step** | search table → per-row magnet page (4-host failover, cap 8 detail fetches) | `re`/`html.parser` |

**6 JSON, 2 RSS, 2 HTML.** Only 1337x needs real scraping.

### Shared helpers to port (tiny)

- `build_magnet(info_hash, name)` — tracker list + `magnet:?xt=urn:btih:…&dn=…&tr=…`.
- `parse_magnet(s)` + base32→hex `normalize_info_hash` — for pasted magnets + SubsPlease.
- `parse_size("1.5 GB")` → bytes.
- `fetch(url)` — `urllib` wrapper: timeout, 1 retry, `User-Agent`, gzip.
- `html.unescape` (stdlib) replaces torlink's hand-rolled `unescapeEntities`.
- `Result` — a `dataclass`: `info_hash, name, size, seeders, leechers, source, magnet, added`.

### Concurrency

`ThreadPoolExecutor(max_workers=10)` submits all source `search()` calls; each pushes
`(source, results | error)` onto a `queue.Queue`; the run loop drains it per tick so
results appear as sources answer. A failed source is tagged offline, search continues.

---

## TUI (`tui.py` + `theme.py`)

### Run loop (`__main__.py`)

```
raw mode (termios/tty) + alt-screen + hide cursor + SIGWINCH handler
loop:
  select.select([stdin], [], [], frame_interval)   # ~40ms when animating, else idle
  handle any key bytes (arrows = ESC[A/B/C/D; printable; enter; backspace; ctrl-c)
  drain search-results queue into state
  if 500ms elapsed: aria2.poll() into state
  render frame (full redraw, truecolor ANSI)
on exit: show cursor, leave alt-screen, restore termios, aria2.shutdown
```

### Layout (mirror torlink)

- **Header:** logo (`theme.LOGO`) + title.
- **Left rail:** Search / Downloads pane switch + source list (display-only in v1).
- **Main pane:**
  - *Search:* input field (with block cursor) + merged results list (name · size ·
    seeders · source tag), arrow/`j`/`k` to move.
  - *Downloads:* active items with sheen progress bar, speed, ETA, peers; completed below.
- **Footer:** context key hints.
- **Overlay:** `?` help.

### Keymap (v1)

| Key | Action |
|---|---|
| type + `Enter` | run search (empty = browse latest; a magnet = add it directly) |
| `↑`/`↓` or `k`/`j` | move selection |
| `d` | download selected result |
| `Tab` | switch Search ↔ Downloads |
| `?` | help overlay |
| `q` / `Ctrl-C` | quit (restore terminal) |

### The look (`theme.py` — one file changes everything)

- `COLOR` map (torlink base: accent `#a78bfa`, text `#e9e4f5`, good/warn/bad, etc.).
- Glyphs (pointer `❯`, done `✓`, bar `▌`, `↓`/`↑`/`•`).
- `LOGO` block + source tag styles.
- **Sheen math** — the cosine-bell sweep from torlink's `sheen.ts`, pure arithmetic,
  transliterated 1:1 so the progress bar is identical.
- **omp × torlink blend:** sample omp's actual palette + logo treatment and blend
  here when building — do not invent colors before then.

---

## Phases

> Build only on "go". Commit after every change.

### Phase 1 — Engine (`aria2.py`)
- Spawn `aria2c` (free port, random secret, conf-path, private session).
- JSON-RPC client (urllib+json): `addUri`, `tellStatus`, `tellActive`, `tellStopped`,
  `forceRemove`, `getGlobalStat`, `shutdown`.
- `add(magnet)` returns our id; follow `followedBy` to the real gid.
- `poll()` → mapped status list.
- **Check:** `selftest()` — spawn aria2, add a small magnet, poll until progress > 0,
  remove, shutdown. `assert`-based, no UI.

### Phase 2 — Sources (`sources.py`)
- 10 scrapers + shared helpers (`build_magnet`, `parse_magnet`, `parse_size`, `fetch`).
- `search(query)` → `ThreadPoolExecutor` fan-out → merged, seeder-sorted results.
- **Check:** `selftest()` — run a known query, assert ≥1 result with a valid magnet
  from ≥1 source; assert a pasted magnet parses.

### Phase 3 — TUI (`tui.py`, `theme.py`, `__main__.py`)
- Raw-mode terminal control + run loop + key parsing.
- Render header/rail/main/footer + help overlay; sheen progress bar.
- Wire search → results → `d` → Downloads pane (live aria2 poll).
- **Check:** launch, search, download a small magnet, watch progress, quit clean —
  terminal fully restored.

### Phase 4 — Ship + look
- `zipapp` → `trawl`; symlink to `/usr/local/bin/trawl`; confirm `trawl` launches cold.
- Blend the omp × torlink theme in `theme.py`.
- **Check:** `trawl` runs from `PATH` with no args and opens to the search bar.

---

## Deferred (YAGNI — add when actually wanted)

- Animated splash screen (static logo header instead).
- Seeding tab / pause-resume (user is leech-only).
- Separate persisted history store (use aria2 session + `tellStopped`).
- Interactive source filtering / per-category browse (search hits all).
- `config.json` / in-app download-dir change (inherit from aria2.conf).
- Per-file selection within a torrent; mouse/wheel support.
- Attach to an already-running aria2 daemon (v1 spawns its own).

---

## Conventions

- **git:** initialized; **commit after every change** with a clear message.
- **Dependencies:** stdlib only. Adding any third-party package needs an explicit
  decision — default is *no*.
- **Checks:** every module with non-trivial logic ships one runnable `assert`-based
  `selftest()` (no frameworks, no fixtures). Trivial one-liners need none.
- **Comments:** mark deliberate shortcuts with `# ponytail:` naming the ceiling and
  the upgrade path.
- **Platform:** macOS; Python 3.14; aria2 from Homebrew.

---

## Verification (end to end, before calling it done)

1. `trawl` from `PATH` opens to the search bar (no Node, no install).
2. A real query streams in merged, source-tagged, seeder-sorted results.
3. `d` downloads via aria2; the Downloads pane shows live progress/speed/ETA.
4. A pasted magnet downloads directly.
5. Interrupted download resumes on relaunch (aria2 session).
6. An offline source is skipped; search still completes.
7. Quit (and `Ctrl-C`) restores the terminal cleanly.

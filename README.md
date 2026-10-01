# Trawl

```
▀█▀ █▀▄ ▄▀▄ █ ▄ █ █     ╱╲╱╲╱╲
 █  █▀▄ █▀█ ▀▄▀▄▀ █▄▄   ╲╱╲╱╲╱
```

![macOS](https://img.shields.io/badge/macOS-000?logo=apple&logoColor=white)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-success)
![release](https://img.shields.io/github/v/release/araidz/Trawl?color=a78bfa)
![license](https://img.shields.io/badge/license-MIT-blue)

A curated, terminal-native torrent & book finder. One search trawls a short
list of reputable sources — games, movies, TV, anime, books — at once; pick a
result and **aria2** downloads it (magnets, direct http links, and ebooks).
Just type `trawl` — no Node, no `npx`, no install step.

Trawl is a from-scratch Python TUI inspired by
[torlink](https://github.com/baairon/torlink) (the look and feel) and driven by
[aria2](https://aria2.github.io/) like its sibling
[Riptide](https://github.com/araidz/Riptide) — **zero third-party packages, stdlib only.**

## Preview

```
                      ▀█▀ █▀▄ ▄▀▄ █ ▄ █ █     ╱╲╱╲╱╲
                       █  █▀▄ █▀█ ▀▄▀▄▀ █▄▄   ╲╱╲╱╲╱

           A curated, terminal-native torrent & book finder.
               games  ·  movies  ·  tv  ·  anime  ·  books

    ╭─ Search ─────────────────────────────────────────────────────╮
    │ ❯ Search, or paste a magnet or link…                         │
    ╰──────────────────────────────────────────────────────────────╯

                   type to search   ↵ browse   q quit
```

```
  ▀█▀ █▀▄ ▄▀▄ █ ▄ █ █     ╱╲╱╲╱╲
   █  █▀▄ █▀█ ▀▄▀▄▀ █▄▄   ╲╱╲╱╲╱
  ────────────────────────────────────────────────────────────────────
                    ╭─ Search ────────────────────────────────────────╮
  ▌ All             │ ❯ oppenheimer                                   │
    Games           ╰─────────────────────────────────────────────────╯
    Movies
    TV              ╭─ Results · seeders ─────────────────────── (3) ─╮
    Anime           │ 3 results                                       │
    Books           │    Name                     Size       S:L   Src │
    Downloads       │ ❯  Oppenheimer (2023)…   1.83 GB   1240:88   YTS │
                    │    Oppenheimer 2023 2…  14.90 GB    910:41   KNB │
                    │    Oppenheimer.2023.P…   1.96 GB    540:30   TPB │
                    ╰─────────────────────────────────────────────────╯

  ↑↓ move  ·  enter details  ·  d grab  ·  o page  ·  y copy  ·  q quit
```

## Features

- **Multi-source search** — built-in sources plus user-configured Torznab feeds are
  queried concurrently; results stream in, merge, and sort by seeders (toggle to
  size / newest). Failed sources can be retried without losing good results.
- **aria2 engine** — spawns a private `aria2c` over JSON-RPC; honors your
  `~/.aria2/aria2.conf`. Magnet metadata → real download handoff handled; direct
  http(s) links download too.
- **Downloads pane** — live progress (animated bar), speed, ETA, peers;
  pause / resume / cancel / retry; pick individual files from multi-file
  torrents; reveal in Finder; a persistent *Recently downloaded* list.
- **Release badges** — each row shows what the name says it is (`2160p WEB HDR`,
  `1080p BD`, `720p HDTV`); CAM/telesync rips are flagged red. The badge column
  appears on wide terminals; details always show the full format line.
- **Look inside first** — `f` in a magnet's details view fetches just its
  metadata (a few seconds, nothing written to your downloads) and lists the files
  with sizes. Tick the ones you want and `Enter` downloads only those; a season
  pack no longer has to arrive whole.
- **Inspect before grabbing** — a details view, open the torrent's page in your
  browser, copy its selected URI/link, or save its `.torrent` file (`e`, metadata
  only). Duplicate results retain source variants; use `←` / `→` in details to
  cycle them.
- **Instant repeat searches** — a search where every source answered is kept for
  ten minutes; searching it again (or coming back to it) replays from memory. A
  search with a failed source is never cached, and `R` always re-runs.
- **Honest errors** — a Cloudflare browser check is called what it is instead of
  "HTTP 403", DNS failures, resets, TLS and timeouts read as plain sentences, and
  `E` lists every source with its response time, slowest first. A source that fails
  three searches in a row is paused for the session so searches stay fast (`R`
  gives it another chance); a search where *everything* failed counts as being
  offline and pauses nothing.
- **Themes and colour safety** — seven palettes (violet, light, Catppuccin, Nord,
  Gruvbox, Dracula, Tokyo Night), cycled from settings (`g`, "Theme"). Trawl
  detects the terminal: truecolor where it's advertised (`COLORTERM`, iTerm2,
  WezTerm, kitty, Ghostty…), a 256-colour fallback otherwise, and plain text
  under `NO_COLOR`. `TRAWL_COLOR=truecolor|256|none` overrides the detection.
- **Batch grab** — `Space` marks results (a ✓ appears and the status line totals
  the size), `d` grabs them all behind a single disk-space check, `D` sends the
  whole batch to one folder. Marks follow the result through re-sorting.
- **Folder download** — `D` grabs to a one-off folder (created on the fly,
  remembered for next time) without changing the default download dir.
- **Dead-torrent filter** — `z` hides zero-seeder results from sources that
  report swarm counts; library/RSS sources with no swarm data (FitGirl,
  SubsPlease, TokyoTosho, LibGen, Anna's, AudiobookBay) always show their
  health as `—`.
- **Resume** — unfinished downloads resume automatically on the next launch;
  `s` additionally scans the download folder for stray partial `*.aria2` files
  (torrents and remembered direct-http grabs).
- **Safety nets** — before a grab, Trawl compares the torrent's size with the
  free space on the target volume (keeping 1 GiB spare) and warns instead of
  filling your disk; press the same key again to grab anyway. `q` quits at once
  when nothing is downloading and only asks while something is in flight.
- **Quality of life** — persistent search history, completion notifications,
  clipboard magnet auto-detect (new magnets/links are offered as they appear),
  mouse-wheel scrolling, a settings overlay (toggle sources, set the download
  dir and an optional speed cap), and confirm-on-quit.
- **Single file** — ships as one stdlib zipapp executable on your `PATH`.

## Requirements

- **macOS** (uses `open`, `pbcopy`/`pbpaste`, `osascript`)
- **[aria2](https://aria2.github.io/)** (Homebrew installs it for you)
- **Python 3.10+**

## Install

### Homebrew

```sh
brew tap araidz/trawl https://github.com/araidz/Trawl
brew install trawl
```

`brew` pulls in `aria2` and Python automatically. Update later with `brew upgrade trawl`.

### From source

```sh
git clone https://github.com/araidz/Trawl.git && cd Trawl
sh build.sh                                       # -> dist/trawl (one self-contained file)
ln -sf "$PWD/dist/trawl" /opt/homebrew/bin/trawl  # or anywhere on your PATH
```

Needs `aria2` (`brew install aria2`). Or run without building: `python3 -m trawl`.

## Usage

Trawl opens to a search bar. Type and press Enter to search, press Enter on an
empty box to browse the latest, or paste a magnet or direct http(s) link to grab it.
You can also start straight into a search: `trawl oppenheimer`.

**Search**

| Key | Action |
| --- | --- |
| type · `Enter` | search (paste a magnet, bare infohash, or link to grab; drag a `.torrent` file onto the window to add it) |
| `↑ ↓` | recall past searches · `PgUp/PgDn` page · `Home/End` jump |
| `Ctrl-A/E` caret · `Ctrl-U/W` kill · `Esc` exit the box | edit the query |
| `Enter` | result details |
| `Space` | mark / unmark the selected result and step down (build a batch: a whole season, a run of episodes) |
| `a` · `Esc` | mark all visible / none · clear marks |
| `d` | download the marked results, or the selected one if nothing is marked |
| `D` | the same, into a chosen folder (created on the fly, remembered for next time) |
| `e` | save the selected torrent's `.torrent` file (metadata only, no download) |
| `z` | hide/show dead torrents (zero-seeder rows from health-reporting sources) |
| `o` | open the torrent's page in your browser |
| `y` copy selected URI/link · `v` | grab a magnet or link from the clipboard |
| `f` | filter the current results as you type (same operators as the search box: `res:>=1080`, `-cam`, …); `Enter` keeps it, `Esc` clears it |
| `R` | search again from scratch, skipping the cache |
| `S` | cycle sort (seeders / size / newest) |
| `r` | retry failed sources, preserving successful results |
| `E` | source health: how fast each source answered, which failed and why, which are paused |
| `← →` filter category · `c` | clear results |
| `s` | resume partial downloads found on disk (torrents and direct links) |
| `g` settings · `?` keys · `q` | quit |

**Details** (`Enter` on a result)

| Key | Action |
| --- | --- |
| `d` · `D` · `e` | download · download to a folder · save the `.torrent` |
| `f` | look inside the torrent: `↑↓` move, `Space` tick, `a` all/none, `Enter` download the ticked files |
| `← →` | cycle duplicate source variants |
| `o` · `y` · `p` | open the page · copy the magnet · open the poster |

**Downloads** (`Tab` to switch)

| Key | Action |
| --- | --- |
| `↑ ↓` | move / scroll · `PgUp/PgDn` page · `Home/End` jump |
| `Enter` | open a finished download (a season pack opens its folder) |
| `p` | pause / resume · `x` cancel (asks: delete files or keep) |
| `r` | retry a failed download |
| `f` | choose which files to download (season packs) |
| `o` | reveal the file in Finder |
| `s` resume · `g` settings · `q` | quit |

## What it searches

| Category | Sources |
| --- | --- |
| Games | FitGirl |
| Movies | YTS · The Pirate Bay · 1337x · LimeTorrents |
| TV | EZTV · SolidTorrents · The Pirate Bay · 1337x · LimeTorrents |
| Anime | Nyaa · SubsPlease · AnimeTosho · TokyoTosho |
| Books | The Pirate Bay · Nyaa (literature) · Library Genesis · Anna's Archive · AudiobookBay |
| All | Knaben (meta-aggregator) · TorrentGalaxy · Torrents-CSV |

Toggle any source on or off in the settings overlay (`g`). If a source is down,
the search carries on without it.

### Torznab feeds

In settings (`g`), press `a` and paste a Torznab endpoint URL. You may also
enter a separate API key with `K`; a separate key overrides an `apikey` already
present in the endpoint URL. User feeds are search-only: Trawl does not request
capabilities and currently provides neither browse-latest nor pagination for
them. Feed availability and contents depend on the endpoint.

### Local query operators

Trawl sends only the remaining search text to sources, then applies these
filters locally: `seeders:`, `size:`, `source:`, `group:`, `age:`, `files:`, `res:`, and `codec:`.
`res:` reads the resolution from the release name (`res:>=1080`, `res:4k`; a bare
value is an exact match) and `codec:` folds aliases (`hevc`/`h265` = `x265`,
`avc`/`h264` = `x264`). Use `-term` to exclude a word and quotes for an exact
phrase, for example `"special edition" -cam seeders:>10 size:<4GiB group:movies`. Unknown operators
are treated as literal search text.

## How it works

Trawl launches its own `aria2c` with JSON-RPC on loopback and drives it over
HTTP. aria2 does all the downloading; Trawl is a thin native-feeling client plus
a raw-ANSI TUI. Your `~/.aria2/aria2.conf` is loaded as the base config (download
dir, connection/seed settings, resume); Trawl forces only the RPC transport and
uses a private session file so it never touches your own aria2 state.

State lives in `~/Library/Application Support/Trawl/`:
`history.txt` (searches), `downloads.jsonl` (completed), `config.json` (settings),
`update.json` (last update check),
`pending.jsonl` (direct-link grabs, for resuming), `aria2-session.txt` (private session).

Torznab endpoint keys are masked in the UI and errors, as are metadata keys.
They are stored as plaintext in `config.json`, so protect that file like any
other local credential store.

## Privacy

Your files stay on your disk; nothing routes through a central server. Trawl
talks to the sources you search, the torrent network via aria2, and GitHub: a
weekly tracker-list refresh and, at most once a day, the public releases API to
tell you when a newer Trawl exists. The update check sends nothing but the
request itself and can be switched off in settings (`g`, "Update check").

## Credits

- [aria2](https://aria2.github.io/) — the download engine
- [torlink](https://github.com/baairon/torlink) — look-and-feel inspiration
- [Riptide](https://github.com/araidz/Riptide) — the aria2 integration this grew from

No third-party code is used; Trawl is an independent stdlib-only implementation.

## License

[MIT](LICENSE)

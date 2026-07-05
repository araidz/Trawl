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

- **Multi-source search** — 18 sources (incl. the Knaben meta-aggregator) queried
  concurrently, results streamed in and merged, sorted by seeders (toggle to size / newest).
- **aria2 engine** — spawns a private `aria2c` over JSON-RPC; honors your
  `~/.aria2/aria2.conf`. Magnet metadata → real download handoff handled; direct
  http(s) links download too.
- **Downloads pane** — live progress (animated bar), speed, ETA, peers;
  pause / resume / cancel; reveal in Finder; a persistent *Recently downloaded* list.
- **Inspect before grabbing** — a details view, open the torrent's page in your
  browser, or copy its magnet.
- **Resume** — `s` scans the download folder for partial `*.aria2` files and resumes them.
- **Quality of life** — persistent search history, completion notifications,
  clipboard magnet auto-detect, mouse-wheel scrolling, a settings overlay
  (toggle sources, set the download dir), and confirm-on-quit.
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

**Search**

| Key | Action |
| --- | --- |
| type · `Enter` | search (paste a magnet or link to grab) |
| `↑ ↓` | recall past searches / scroll results |
| `Enter` | result details |
| `d` | download selected |
| `o` | open the torrent's page in your browser |
| `y` copy magnet · `v` | grab a magnet or link from the clipboard |
| `S` | cycle sort (seeders / size / newest) |
| `← →` filter category · `c` | clear results |
| `s` | resume partial downloads found on disk |
| `g` settings · `?` keys · `q` | quit |

**Downloads** (`Tab` to switch)

| Key | Action |
| --- | --- |
| `↑ ↓` | move / scroll |
| `p` | pause / resume · `x` cancel (asks: delete files or keep) |
| `o` | reveal the file in Finder |
| `s` resume · `g` settings · `q` | quit |

## What it searches

| Category | Sources |
| --- | --- |
| Games | FitGirl · DODI |
| Movies | YTS · The Pirate Bay · 1337x |
| TV | EZTV · SolidTorrents · The Pirate Bay · 1337x |
| Anime | Nyaa · SubsPlease · AnimeTosho |
| Books | The Pirate Bay · Nyaa (literature) · Library Genesis · Anna's Archive |
| All | Knaben (meta-aggregator) · TorrentGalaxy |

Toggle any source on or off in the settings overlay (`g`). If a source is down,
the search carries on without it.

## How it works

Trawl launches its own `aria2c` with JSON-RPC on loopback and drives it over
HTTP. aria2 does all the downloading; Trawl is a thin native-feeling client plus
a raw-ANSI TUI. Your `~/.aria2/aria2.conf` is loaded as the base config (download
dir, connection/seed settings, resume); Trawl forces only the RPC transport and
uses a private session file so it never touches your own aria2 state.

State lives in `~/Library/Application Support/Trawl/`:
`history.txt` (searches), `downloads.jsonl` (completed), `config.json` (settings),
`aria2-session.txt` (private session).

## Privacy

Your files stay on your disk; nothing routes through a central server. Trawl only
talks to the sources you search and the torrent network via aria2.

## Credits

- [aria2](https://aria2.github.io/) — the download engine
- [torlink](https://github.com/baairon/torlink) — look-and-feel inspiration
- [Riptide](https://github.com/araidz/Riptide) — the aria2 integration this grew from

No third-party code is used; Trawl is an independent stdlib-only implementation.

## License

[MIT](LICENSE)

# Trawl

```
▀█▀ █▀▄ ▄▀▄ █ ▄ █ █     ╱╲╱╲
 █  █▀▄ █▀█ ▀▄▀▄▀ █▄▄   ╲╱╲╱
```

A curated, terminal-native torrent finder. One search trawls a short list of
reputable sources at once; pick a result and **aria2** downloads it. Just type
`trawl` — no Node, no `npx`, no install step.

Trawl is a from-scratch Python TUI inspired by
[torlink](https://github.com/baairon/torlink) (the look and feel) and driven by
[aria2](https://aria2.github.io/) like its sibling
[Riptide](https://github.com/araidz/Riptide) — **zero third-party packages, stdlib only.**

## Features

- **Multi-source search** — 10 curated sources queried concurrently, results
  streamed in and merged, sorted by seeders (toggle to size / newest).
- **aria2 engine** — spawns a private `aria2c` over JSON-RPC; honors your
  `~/.aria2/aria2.conf`. Magnet metadata → real download handoff handled.
- **Downloads pane** — live progress (animated bar), speed, ETA, peers;
  pause / resume / cancel; reveal in Finder; a persistent *Recently downloaded* list.
- **Inspect before grabbing** — a details view, open the torrent's page in your
  browser, or copy its magnet.
- **Resume** — `s` scans the download folder for partial `*.aria2` files and resumes them.
- **Quality of life** — persistent search history, completion notifications,
  clipboard magnet auto-detect, mouse-wheel scrolling, a settings overlay
  (toggle sources, set the download dir), and a confirm-on-quit.
- **Single file** — ships as one stdlib zipapp executable on your `PATH`.

## Requirements

- **macOS** (uses `open`, `pbcopy`/`pbpaste`, `osascript`)
- **Python 3** (developed on 3.14)
- **[aria2](https://aria2.github.io/)** — `brew install aria2`

## Install

```sh
git clone <your-repo-url> Trawl && cd Trawl
sh build.sh                                  # -> dist/trawl (single ~70 KB file)
ln -sf "$PWD/dist/trawl" /opt/homebrew/bin/trawl   # or anywhere on your PATH
trawl
```

Or run from source without building: `python3 -m trawl`.

## Usage

Trawl opens to a search bar. Type and press Enter to search, press Enter on an
empty box to browse the latest, or paste a magnet link to grab it directly.

**Search**

| Key | Action |
| --- | --- |
| type · `Enter` | search (paste a magnet to grab) |
| `↑ ↓` | recall past searches / scroll results |
| `Enter` | result details |
| `d` | download selected |
| `o` | open the torrent's page in your browser |
| `y` | copy magnet · `v` grab a magnet from the clipboard |
| `S` | cycle sort (seeders / size / newest) |
| `← →` | filter by category · `c` clear results |
| `s` | resume partial downloads found on disk |
| `g` | settings · `?` keys · `q` quit |

**Downloads** (`Tab` to switch)

| Key | Action |
| --- | --- |
| `↑ ↓` | move / scroll |
| `p` | pause / resume · `x` cancel |
| `o` | reveal the file in Finder |
| `s` | resume partials · `g` settings · `q` quit |

## What it searches

| Category | Sources |
| --- | --- |
| Games | FitGirl |
| Movies | YTS · The Pirate Bay · 1337x |
| TV | EZTV · SolidTorrents · The Pirate Bay · 1337x |
| Anime | Nyaa · SubsPlease |

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

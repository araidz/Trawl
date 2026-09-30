"""The single look knob: palettes, glyphs, logo, and the sheen/ramp math.

Ported from torlink's theme.ts + sheen.ts (violet base). Two palettes ship:
"violet" (default) and "light" (for light terminals). Colors resolve lazily
through the module __getattr__ (PEP 562), so every `T.<COLOR>` reads the
currently active palette; `set_theme()` flips it at runtime.
"""

from __future__ import annotations

import math
import os

# -- palettes -----------------------------------------------------------------
# violet: torlink's original. light: tuned for light terminal backgrounds.
# Source abbreviations are shared; only the colors are per-palette.
_V_GOOD, _V_WARN = "#86d6a2", "#f0c560"
_SOURCE_ABBR = {
    "fitgirl": "FG", "yts": "YTS", "eztv": "EZTV", "nyaa": "NYAA",
    "subsplease": "SUB", "solid": "SLD", "tpb-movies": "TPB", "tpb-tv": "TPB",
    "tpb-books": "TPB", "x1337-movies": "1337", "x1337-tv": "1337",
    "dodi": "DODI", "animetosho": "ATSH", "knaben": "KNB",
    "torrentgalaxy": "TGx", "nyaa-books": "NYAA", "libgen": "LGEN",
    "annas": "ANNA",
}
_PALETTES: dict[str, dict[str, str | dict[str, str]]] = {
    "violet": {
        "ACCENT": "#a78bfa", "TEXT": "#e9e4f5", "ALT": "#b9a7e6",
        "GOOD": _V_GOOD, "WARN": _V_WARN, "BAD": "#ee7d92",
        "BRIGHT": "#d8b4fe", "RULE": "#6b6577", "PAUSED": "#7c7785",
        "DEEP": "#7c5cd6", "SHEEN_PEAK": "#f4efff", "WHITE": "#ffffff",
        "SHADE": "#4c3a8a", "NET_COLOR": "#5fd0c5",
        "SOURCE_COLOR": {
            "fitgirl": "#a78bfa", "yts": _V_GOOD, "eztv": _V_WARN,
            "nyaa": "#d8b4fe", "subsplease": "#b9a7e6", "solid": "#60a5fa",
            "tpb-movies": "#5fd0c5", "tpb-tv": "#5fd0c5", "tpb-books": "#5fd0c5",
            "x1337-movies": "#f6a55c", "x1337-tv": "#f6a55c", "dodi": "#e0af68",
            "animetosho": "#bb9af7", "knaben": "#7dcfff",
            "torrentgalaxy": "#9ece6a", "nyaa-books": "#d8b4fe",
            "libgen": "#8fd694", "annas": "#f7768e",
        },
    },
    "light": {
        "ACCENT": "#6d4fc9", "TEXT": "#2a2336", "ALT": "#6b5b8f",
        "GOOD": "#2f9e63", "WARN": "#a06a00", "BAD": "#c73a55",
        "BRIGHT": "#7c3aed", "RULE": "#a49ac2", "PAUSED": "#8b8790",
        "DEEP": "#5b3fb8", "SHEEN_PEAK": "#ffffff", "WHITE": "#3b2b66",
        "SHADE": "#c9bff0", "NET_COLOR": "#159a8c",
        "SOURCE_COLOR": {
            "fitgirl": "#6d4fc9", "yts": "#2f9e63", "eztv": "#a06a00",
            "nyaa": "#7c3aed", "subsplease": "#6b5b8f", "solid": "#1d6fd6",
            "tpb-movies": "#159a8c", "tpb-tv": "#159a8c", "tpb-books": "#159a8c",
            "x1337-movies": "#b45309", "x1337-tv": "#b45309", "dodi": "#a05a1f",
            "animetosho": "#6b46c1", "knaben": "#2b7fd0",
            "torrentgalaxy": "#4d7c0f", "nyaa-books": "#7c3aed",
            "libgen": "#3f8f4d", "annas": "#c0265d",
        },
    },
}

# Compact dark palettes: (ACCENT, TEXT, ALT, GOOD, WARN, BAD, BRIGHT, RULE, PAUSED, DEEP, SHADE,
# NET, BLUE, ORANGE, PINK, PEAK). Source colors are derived from these roles.
_ROLES = {
    "fitgirl": "ACCENT", "yts": "GOOD", "eztv": "WARN", "nyaa": "BRIGHT", "subsplease": "ALT",
    "solid": "BLUE", "tpb-movies": "NET_COLOR", "tpb-tv": "NET_COLOR", "tpb-books": "NET_COLOR",
    "x1337-movies": "ORANGE", "x1337-tv": "ORANGE", "dodi": "WARN", "animetosho": "PINK",
    "knaben": "BLUE", "torrentgalaxy": "GOOD", "nyaa-books": "BRIGHT", "libgen": "GOOD", "annas": "BAD",
}
_DARK = {
    "catppuccin": ("#cba6f7", "#cdd6f4", "#b4befe", "#a6e3a1", "#f9e2af", "#f38ba8", "#f5c2e7", "#585b70",
                   "#6c7086", "#8b6fc9", "#45475a", "#94e2d5", "#89b4fa", "#fab387", "#f5c2e7", "#f5e0dc"),
    "nord": ("#88c0d0", "#eceff4", "#81a1c1", "#a3be8c", "#ebcb8b", "#bf616a", "#8fbcbb", "#4c566a",
             "#616e88", "#5e81ac", "#3b4252", "#8fbcbb", "#81a1c1", "#d08770", "#b48ead", "#eceff4"),
    "gruvbox": ("#fabd2f", "#ebdbb2", "#d5c4a1", "#b8bb26", "#fe8019", "#fb4934", "#f9e08c", "#665c54",
                "#7c6f64", "#d79921", "#504945", "#8ec07c", "#83a598", "#fe8019", "#d3869b", "#fbf1c7"),
    "dracula": ("#bd93f9", "#f8f8f2", "#caa9fa", "#50fa7b", "#f1fa8c", "#ff5555", "#d6b8ff", "#6272a4",
                "#6272a4", "#8a5cd6", "#44475a", "#8be9fd", "#8be9fd", "#ffb86c", "#ff79c6", "#f8f8f2"),
    "tokyo-night": ("#7aa2f7", "#c0caf5", "#9aa5ce", "#9ece6a", "#e0af68", "#f7768e", "#bb9af7", "#414868",
                    "#565f89", "#3d59a1", "#292e42", "#7dcfff", "#2ac3de", "#ff9e64", "#bb9af7", "#c0caf5"),
}
for _name, _v in _DARK.items():
    _all = dict(zip(("ACCENT", "TEXT", "ALT", "GOOD", "WARN", "BAD", "BRIGHT", "RULE", "PAUSED", "DEEP",
                     "SHADE", "NET_COLOR", "BLUE", "ORANGE", "PINK", "SHEEN_PEAK"), _v), WHITE="#ffffff")
    _pal = {k: v for k, v in _all.items() if k not in ("BLUE", "ORANGE", "PINK")}  # roles only feed sources
    _pal["SOURCE_COLOR"] = {sid: _all[role] for sid, role in _ROLES.items()}
    _PALETTES[_name] = _pal

THEMES = ("violet", "light", *_DARK)
ACTIVE = "violet"


def set_theme(name: str) -> str:
    global ACTIVE
    if name in _PALETTES:
        ACTIVE = name
    return ACTIVE


# -- terminal color capability -------------------------------------------------
_TRUECOLOR_APPS = {"iTerm.app", "WezTerm", "ghostty", "vscode", "Hyper", "WarpTerminal"}


def detect_color_mode(env) -> str:
    """truecolor | 256 | none. TRAWL_COLOR overrides; NO_COLOR (no-color.org) turns color off;
    truecolor needs a positive signal, so terminals that lack it (older Terminal.app) get 256."""
    force = env.get("TRAWL_COLOR", "").lower()
    if force in ("truecolor", "256", "none"):
        return force
    term = env.get("TERM", "")
    if env.get("NO_COLOR") or term == "dumb":
        return "none"
    if (env.get("COLORTERM", "").lower() in ("truecolor", "24bit")
            or env.get("TERM_PROGRAM") in _TRUECOLOR_APPS
            or any(t in term for t in ("kitty", "ghostty", "alacritty", "wezterm"))):
        return "truecolor"
    return "256"


def rgb_to_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 index: the 6x6x6 cube (16-231) or the grey ramp (232-255)."""
    lvl = lambda v: 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    steps = (0, 95, 135, 175, 215, 255)
    ci = (lvl(r), lvl(g), lvl(b))
    cube = tuple(steps[i] for i in ci)
    gi = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    grey = 8 + 10 * gi
    d = lambda c: sum((x - y) ** 2 for x, y in zip(c, (r, g, b)))
    return 232 + gi if d((grey,) * 3) < d(cube) else 16 + 36 * ci[0] + 6 * ci[1] + ci[2]


COLOR_MODE = detect_color_mode(os.environ)


def __getattr__(name: str):
    pal = _PALETTES[ACTIVE]
    if name in pal:
        return pal[name]
    raise AttributeError(name)

# -- glyphs ------------------------------------------------------------------
PTR = "❯"
DONE = "✓"
ERR = "✗"
DOWN = "↓"
PEER = "•"
BAR = "▌"
PAUSE = "⏸"
DOT = "·"
BLOCK = "█"
TRACK = "░"

# trawl wordmark + a trawling-net mesh (gradient on the word, aqua on the net)
LOGO_LINES: list[str] = [
    "▀█▀ █▀▄ ▄▀▄ █ ▄ █ █     ╱╲╱╲╱╲",
    " █  █▀▄ █▀█ ▀▄▀▄▀ █▄▄   ╲╱╲╱╲╱",
]
NET_GLYPHS = set("╱╲╳▞▚◇")


def source_style(source_id: str) -> tuple[str, str]:
    colors = _PALETTES[ACTIVE]["SOURCE_COLOR"]
    return (_SOURCE_ABBR.get(source_id, source_id[:4].upper()),
            colors.get(source_id, _PALETTES[ACTIVE]["ALT"]))


# -- color math --------------------------------------------------------------


def _rgb(h: str) -> tuple[int, int, int]:
    n = int(h[1:], 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def lerp_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    t = max(0.0, min(1.0, t))
    c = lambda x, y: round(x + (y - x) * t)
    return f"#{c(ar, br):02x}{c(ag, bg):02x}{c(ab, bb):02x}"


def progress_ramp(t: float, deep: str, mid: str, bright: str) -> str:
    return lerp_hex(deep, mid, t / 0.5) if t <= 0.5 else lerp_hex(mid, bright, (t - 0.5) / 0.5)


def _c(name: str) -> str:
    return _PALETTES[ACTIVE][name]  # type: ignore[return-value]


def logo_color(t: float) -> str:
    if t < 0.15:
        return lerp_hex(_c("WHITE"), _c("BRIGHT"), t / 0.15)
    if t < 0.4:
        return lerp_hex(_c("BRIGHT"), _c("ACCENT"), (t - 0.15) / 0.25)
    if t < 0.7:
        return lerp_hex(_c("ACCENT"), _c("DEEP"), (t - 0.4) / 0.3)
    return lerp_hex(_c("DEEP"), _c("SHADE"), (t - 0.7) / 0.3)


# -- sheen (torlink sheen.ts, verbatim math) ---------------------------------
SHEEN_RADIUS = 4.5
SHEEN_GAP = 8
SHEEN_SPEED = 0.45
SHEEN_MAX = 0.9
SHEEN_TICK_MS = 40


def sheen_period(width: int) -> int:
    return math.ceil(width + SHEEN_RADIUS * 2) + SHEEN_GAP


def sheen_center(tick: float, period: int) -> float:
    return (tick * SHEEN_SPEED) % period - SHEEN_RADIUS


def sheen_intensity(i: int, center: float) -> float:
    d = abs(i - center)
    if d >= SHEEN_RADIUS:
        return 0.0
    return 0.5 * (1 + math.cos(math.pi * d / SHEEN_RADIUS)) * SHEEN_MAX

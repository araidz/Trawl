"""The single look knob: palettes, glyphs, logo, and the sheen/ramp math.

Ported from torlink's theme.ts + sheen.ts (violet base). Two palettes ship:
"violet" (default) and "light" (for light terminals). Colors resolve lazily
through the module __getattr__ (PEP 562), so every `T.<COLOR>` reads the
currently active palette; `set_theme()` flips it at runtime.
"""

from __future__ import annotations

import math

# -- palettes -----------------------------------------------------------------
# violet: torlink's original. light: tuned for light terminal backgrounds.
_V_GOOD, _V_WARN = "#86d6a2", "#f0c560"
_PALETTES: dict[str, dict[str, str | dict[str, tuple[str, str]]]] = {
    "violet": {
        "ACCENT": "#a78bfa", "TEXT": "#e9e4f5", "ALT": "#b9a7e6",
        "GOOD": _V_GOOD, "WARN": _V_WARN, "BAD": "#ee7d92",
        "BRIGHT": "#d8b4fe", "RULE": "#6b6577", "PAUSED": "#7c7785",
        "DEEP": "#7c5cd6", "SHEEN_PEAK": "#f4efff", "WHITE": "#ffffff",
        "SHADE": "#4c3a8a", "NET_COLOR": "#5fd0c5",
        "SOURCE_STYLE": {
            "fitgirl": ("FG", "#a78bfa"), "yts": ("YTS", _V_GOOD),
            "eztv": ("EZTV", _V_WARN), "nyaa": ("NYAA", "#d8b4fe"),
            "subsplease": ("SUB", "#b9a7e6"), "solid": ("SLD", "#60a5fa"),
            "tpb-movies": ("TPB", "#5fd0c5"), "tpb-tv": ("TPB", "#5fd0c5"),
            "tpb-books": ("TPB", "#5fd0c5"), "x1337-movies": ("1337", "#f6a55c"),
            "x1337-tv": ("1337", "#f6a55c"), "dodi": ("DODI", "#e0af68"),
            "animetosho": ("ATSH", "#bb9af7"), "knaben": ("KNB", "#7dcfff"),
            "torrentgalaxy": ("TGx", "#9ece6a"), "nyaa-books": ("NYAA", "#d8b4fe"),
            "libgen": ("LGEN", "#8fd694"), "annas": ("ANNA", "#f7768e"),
        },
    },
    "light": {
        "ACCENT": "#6d4fc9", "TEXT": "#2a2336", "ALT": "#6b5b8f",
        "GOOD": "#2f9e63", "WARN": "#a06a00", "BAD": "#c73a55",
        "BRIGHT": "#7c3aed", "RULE": "#a49ac2", "PAUSED": "#8b8790",
        "DEEP": "#5b3fb8", "SHEEN_PEAK": "#ffffff", "WHITE": "#3b2b66",
        "SHADE": "#c9bff0", "NET_COLOR": "#159a8c",
        "SOURCE_STYLE": {
            "fitgirl": ("FG", "#6d4fc9"), "yts": ("YTS", "#2f9e63"),
            "eztv": ("EZTV", "#a06a00"), "nyaa": ("NYAA", "#7c3aed"),
            "subsplease": ("SUB", "#6b5b8f"), "solid": ("SLD", "#1d6fd6"),
            "tpb-movies": ("TPB", "#159a8c"), "tpb-tv": ("TPB", "#159a8c"),
            "tpb-books": ("TPB", "#159a8c"), "x1337-movies": ("1337", "#b45309"),
            "x1337-tv": ("1337", "#b45309"), "dodi": ("DODI", "#a05a1f"),
            "animetosho": ("ATSH", "#6b46c1"), "knaben": ("KNB", "#2b7fd0"),
            "torrentgalaxy": ("TGx", "#4d7c0f"), "nyaa-books": ("NYAA", "#7c3aed"),
            "libgen": ("LGEN", "#3f8f4d"), "annas": ("ANNA", "#c0265d"),
        },
    },
}

THEMES = ("violet", "light")
ACTIVE = "violet"


def set_theme(name: str) -> str:
    global ACTIVE
    if name in _PALETTES:
        ACTIVE = name
    return ACTIVE


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
    style_map = _PALETTES[ACTIVE]["SOURCE_STYLE"]
    return style_map.get(source_id, (source_id[:4].upper(), _PALETTES[ACTIVE]["ALT"]))  # type: ignore[return-value]


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

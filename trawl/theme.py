"""The single look knob: palette, glyphs, logo, and the sheen/ramp math.

Ported from torlink's theme.ts + sheen.ts (violet base). To reskin for omp,
sample its palette and change the constants here — nothing else touches color.
"""

from __future__ import annotations

import math

# -- palette (torlink violet) ------------------------------------------------
ACCENT = "#a78bfa"
TEXT = "#e9e4f5"
ALT = "#b9a7e6"
GOOD = "#86d6a2"
WARN = "#f0c560"
BAD = "#ee7d92"
BRIGHT = "#d8b4fe"
RULE = "#6b6577"
PAUSED = "#7c7785"
DEEP = "#7c5cd6"
SHEEN_PEAK = "#f4efff"
WHITE = "#ffffff"
SHADE = "#4c3a8a"

# -- glyphs ------------------------------------------------------------------
PTR = "❯"
DONE = "✓"
ERR = "✗"
DOWN = "↓"
UP = "↑"
PEER = "•"
BAR = "▌"
PAUSE = "⏸"
DOT = "·"
WARN_I = "⚠"
BLOCK = "█"
TRACK = "░"

# trawl wordmark + a trawling-net mesh (gradient on the word, aqua on the net)
LOGO_LINES: list[str] = [
    "▀█▀ █▀▄ ▄▀▄ █ ▄ █ █     ╱╲╱╲╱╲",
    " █  █▀▄ █▀█ ▀▄▀▄▀ █▄▄   ╲╱╲╱╲╱",
]
NET_GLYPHS = set("╱╲╳▞▚◇")
NET_COLOR = "#5fd0c5"  # aqua — reads as net-in-water against the violet

# -- per-source tag + color (torlink SOURCE_STYLE) ---------------------------
SOURCE_STYLE: dict[str, tuple[str, str]] = {
    "fitgirl": ("FG", ACCENT),
    "yts": ("YTS", GOOD),
    "eztv": ("EZTV", WARN),
    "nyaa": ("NYAA", BRIGHT),
    "subsplease": ("SUB", "#b9a7e6"),
    "solid": ("SLD", "#60a5fa"),
    "tpb-movies": ("TPB", "#5fd0c5"),
    "tpb-tv": ("TPB", "#5fd0c5"),
    "tpb-books": ("TPB", "#5fd0c5"),
    "x1337-movies": ("1337", "#f6a55c"),
    "x1337-tv": ("1337", "#f6a55c"),
    "dodi": ("DODI", "#e0af68"),
    "animetosho": ("ATSH", "#bb9af7"),
    "knaben": ("KNB", "#7dcfff"),
    "torrentgalaxy": ("TGx", "#9ece6a"),
    "nyaa-books": ("NYAA", BRIGHT),
    "libgen": ("LGEN", "#8fd694"),
    "annas": ("ANNA", "#f7768e"),
}


def source_style(source_id: str) -> tuple[str, str]:
    return SOURCE_STYLE.get(source_id, (source_id[:4].upper(), ALT))


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


def logo_color(t: float) -> str:
    if t < 0.15:
        return lerp_hex(WHITE, BRIGHT, t / 0.15)
    if t < 0.4:
        return lerp_hex(BRIGHT, ACCENT, (t - 0.15) / 0.25)
    if t < 0.7:
        return lerp_hex(ACCENT, DEEP, (t - 0.4) / 0.3)
    return lerp_hex(DEEP, SHADE, (t - 0.7) / 0.3)


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

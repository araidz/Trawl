"""Optional metadata for a movie/series result: rating, genres, cast, overview,
poster. Two interchangeable providers, chosen by the user:

- tmdb: TMDB rating (0-10), rich cast/genres. Free v3 key.
- omdb: IMDb rating + votes. Free key.

Read via the same stdlib HTTP path as the scrapers; no new deps. No key for the
chosen provider -> the info panel simply stays off.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .sources import fetch_json

_TMDB = "https://api.themoviedb.org/3"
_OMDB = "https://www.omdbapi.com/"
_KIND = {"Movies": "movie", "TV": "tv"}


def kind_for(group: str | None) -> str | None:
    """Search kind for a result's category, or None (books/games/anime)."""
    return _KIND.get(group or "")


@dataclass
class Meta:
    title: str
    year: str
    rating: float  # 0-10; TMDB vote_average or IMDb rating per provider
    votes: int
    genres: list[str]
    cast: list[str]
    overview: str
    poster: str = ""  # image URL, or "" if none


# Recover a searchable title + year from a release name.
# ponytail: regex heuristic, not a real release parser; it handles the common
# scene/p2p shapes. Upgrade path is a library like `guessit` if match rate matters.
_SEP = re.compile(r"[._]+")
_EPISODE = re.compile(r"\b(s\d{1,2}(e\d{1,3})?|season\s*\d+|complete)\b.*", re.I)
_YEAR = re.compile(r"(?:^|[\s(\[])((?:19|20)\d{2})(?:[\s)\]]|$)")
_TAGS = re.compile(
    r"\b(1080p|2160p|4k|720p|480p|x264|x265|h\.?264|h\.?265|hevc|blu-?ray|web-?dl|"
    r"web-?rip|hd-?rip|bd-?rip|dvd-?rip|remux|proper|repack|extended|unrated|imax|"
    r"hdr|10bit|aac|dts|dd[p]?5\.?1|atmos|multi|dual|subbed|dubbed|hdtv|amzn|nf)\b.*",
    re.I)


def clean_title(name: str) -> tuple[str, str | None]:
    s = _SEP.sub(" ", name)
    s = _EPISODE.sub("", s)            # drop SxxExx / season / complete + trailing junk
    m = _YEAR.search(s)
    year = None
    if m:
        year = m.group(1)
        s = s[:m.start()]             # title is everything before the year
    else:
        s = _TAGS.sub("", s)          # no year: cut at the first quality tag
    s = re.sub(r"[\[\](){}]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s, year


def lookup(name: str, kind: str, provider: str, key: str) -> Meta | None:
    """Best first match for a release name, or None. May raise SourceError."""
    title, year = clean_title(name)
    if not title:
        return None
    return _omdb(title, year, kind, key) if provider == "omdb" else _tmdb(title, year, kind, key)


# -- TMDB --------------------------------------------------------------------


def _tmdb(title: str, year: str | None, kind: str, key: str) -> Meta | None:
    def search(with_year: bool) -> list:
        params = {"api_key": key, "query": title}
        if with_year and year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        return fetch_json(f"{_TMDB}/search/{kind}?{urllib.parse.urlencode(params)}").get("results") or []

    hits = search(True) or (search(False) if year else [])
    tid = hits[0].get("id") if hits else None
    if not tid:
        return None
    det = fetch_json(f"{_TMDB}/{kind}/{tid}?api_key={key}&append_to_response=credits")
    date = det.get("release_date") or det.get("first_air_date") or ""
    genres = [g["name"] for g in det.get("genres") or [] if g.get("name")]
    cast = [c["name"] for c in (det.get("credits") or {}).get("cast") or [] if c.get("name")][:5]
    pp = det.get("poster_path")
    return Meta(det.get("title") or det.get("name") or "", date[:4],
                float(det.get("vote_average") or 0), int(det.get("vote_count") or 0),
                genres, cast, (det.get("overview") or "").strip(),
                f"https://image.tmdb.org/t/p/w500{pp}" if pp else "")


# -- OMDb --------------------------------------------------------------------


def _clean(s: str | None) -> str:
    s = (s or "").strip()
    return "" if s == "N/A" else s


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in _clean(s).split(",") if x.strip()]


def _omdb(title: str, year: str | None, kind: str, key: str) -> Meta | None:
    def fetch(with_year: bool) -> dict:
        params = {"apikey": key, "t": title, "type": "series" if kind == "tv" else "movie"}
        if with_year and year:
            params["y"] = year
        return fetch_json(f"{_OMDB}?{urllib.parse.urlencode(params)}")

    d = fetch(True)
    if d.get("Response") != "True" and year:
        d = fetch(False)
    if d.get("Response") != "True":
        return None

    def num(s: str | None) -> float:
        try:
            return float(_clean(s).replace(",", ""))
        except ValueError:
            return 0.0

    return Meta(_clean(d.get("Title")), _clean(d.get("Year"))[:4],
                num(d.get("imdbRating")), int(num(d.get("imdbVotes"))),
                _split(d.get("Genre")), _split(d.get("Actors"))[:5],
                _clean(d.get("Plot")), _clean(d.get("Poster")))

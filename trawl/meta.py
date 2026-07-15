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


# -- self-check --------------------------------------------------------------


def selftest() -> None:
    assert clean_title("The.Matrix.1999.1080p.BluRay.x264") == ("The Matrix", "1999")
    assert clean_title("Dune.Part.Two.2024.2160p.WEB-DL") == ("Dune Part Two", "2024")
    assert clean_title("Oppenheimer (2023) [1080p]") == ("Oppenheimer", "2023")
    assert clean_title("Breaking.Bad.S01E01.720p.HDTV.x264") == ("Breaking Bad", None)
    assert clean_title("Severance.S02.COMPLETE.1080p") == ("Severance", None)
    assert kind_for("Movies") == "movie" and kind_for("TV") == "tv" and kind_for("Books") is None

    g = globals()
    orig = g["fetch_json"]
    # TMDB: search -> details+credits -> Meta, offline
    g["fetch_json"] = lambda url, **k: (
        {"results": [{"id": 603}]} if "/search/" in url else
        {"title": "The Matrix", "release_date": "1999-03-30", "poster_path": "/abc.jpg",
         "vote_average": 8.2, "vote_count": 25000,
         "genres": [{"name": "Action"}, {"name": "Science Fiction"}],
         "overview": "A computer hacker learns the true nature of reality.",
         "credits": {"cast": [{"name": "Keanu Reeves"}, {"name": "Laurence Fishburne"}]}})
    m = lookup("The.Matrix.1999.1080p", "movie", "tmdb", "KEY")
    assert m and m.title == "The Matrix" and m.year == "1999" and m.rating == 8.2, m
    assert m.genres == ["Action", "Science Fiction"] and m.cast == ["Keanu Reeves", "Laurence Fishburne"], m
    assert m.poster == "https://image.tmdb.org/t/p/w500/abc.jpg", m.poster
    # OMDb: single call -> Meta (IMDb rating), offline
    g["fetch_json"] = lambda url, **k: {
        "Response": "True", "Title": "The Matrix", "Year": "1999", "imdbRating": "8.7",
        "imdbVotes": "1,999,001", "Genre": "Action, Sci-Fi", "Actors": "Keanu Reeves, Carrie-Anne Moss",
        "Plot": "A hacker discovers reality is a simulation.", "Poster": "http://img/omdb.jpg"}
    m = lookup("The.Matrix.1999.1080p", "movie", "omdb", "KEY")
    assert m and m.rating == 8.7 and m.votes == 1999001, m
    assert m.genres == ["Action", "Sci-Fi"] and m.cast == ["Keanu Reeves", "Carrie-Anne Moss"], m
    assert m.poster == "http://img/omdb.jpg" and m.overview.startswith("A hacker"), m
    # OMDb "N/A" fields degrade to empty, no match -> None
    g["fetch_json"] = lambda url, **k: {"Response": "True", "Title": "X", "Year": "2000",
                                        "imdbRating": "N/A", "imdbVotes": "N/A", "Genre": "N/A",
                                        "Actors": "N/A", "Plot": "N/A", "Poster": "N/A"}
    m = lookup("X 2000", "movie", "omdb", "KEY")
    assert m.rating == 0 and m.genres == [] and m.cast == [] and m.poster == "", m
    g["fetch_json"] = lambda url, **k: {"Response": "False", "Error": "not found"}
    assert lookup("Nope 2099", "movie", "omdb", "KEY") is None, "omdb no match -> None"
    g["fetch_json"] = lambda url, **k: {"results": []}
    assert lookup("Nope 2099", "movie", "tmdb", "KEY") is None, "tmdb no match -> None"
    g["fetch_json"] = orig
    print("meta ok")


if __name__ == "__main__":
    selftest()

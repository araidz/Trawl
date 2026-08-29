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



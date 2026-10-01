# pure logic — deterministic, offline
assert parse_size("1.5 GB") == 1_500_000_000
assert parse_size("700 MiB") == 700 * 1024 ** 2
assert parse_size("") == 0
assert parse_size("-1 GB") == 0 and parse_size("9" * 10000 + " GB") == 0
h40 = "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
pm = parse_magnet(build_magnet(h40, "Some Movie 2024"))
assert pm and pm.info_hash == h40 and pm.name == "Some Movie 2024", pm
b32 = base64.b32encode(bytes.fromhex(h40)).decode()
assert normalize_info_hash(b32) == h40, "base32->hex"
assert parse_magnet("not a magnet") is None
# parse_source: magnet passes through; http(s) becomes a grabbable link with
# a filename label; anything else (a search query) is not grabbable.
assert parse_source(build_magnet(h40, "x")).kind == "magnet"
lk = parse_source("https://example.com/files/My%20Book.epub")
assert lk and lk.info_hash == "" and lk.name == "My Book.epub" and lk.kind == "link", lk
assert parse_source("https://example.com").name == "example.com"
assert parse_source("https://x.org/a/b.torrent").kind == "torrent"
assert parse_source("oppenheimer 2023") is None
# bare infohash -> magnet; a 39/41-char or non-hex string is just a query
bare = parse_source(h40.upper())
assert bare and bare.info_hash == h40 and bare.kind == "magnet" and bare.magnet.startswith("magnet:?xt=urn:btih:" + h40), bare
assert parse_source(h40[:-1]) is None and parse_source(h40 + "0") is None and parse_source("z" * 40) is None
# a local .torrent path (as terminals paste dropped files) -> kind "file"
import tempfile as _tf2
_td2 = os.path.join(_tf2.mkdtemp(), "My Show [1080p].torrent")
open(_td2, "wb").write(b"d4:infod4:name1:xee")
for pasted in (_td2, _td2.replace(" ", "\\ ") + " ", f"'{_td2}'", f'"{_td2}"'):
    pf = parse_source(pasted)
    assert pf and pf.kind == "file" and pf.magnet == _td2 and pf.name == "My Show [1080p].torrent", (pasted, pf)
assert parse_source(_td2 + ".missing.torrent") is None and parse_source("a.torrent b.torrent") is None
assert parse_source("it's a query.torrent") is None, "unbalanced quote is not a path"
merged = dedupe([
    Result(h40, "lo", 1, 5, 0, "a", "m"),
    Result(h40, "hi", 1, 50, 0, "b", "m"),
])
assert len(merged) == 1 and merged[0].seeders == 50, "dedupe keeps higher seeders"
assert [v.source for v in merged[0].variants] == ["b", "a"]
assert dedupe(merged) == merged, "dedupe is idempotent"
direct = dedupe([
    Result("a" * 32, "low", 1, 2, 3, "one", "HTTPS://Example.COM/get.php?x=1#frag"),
    Result("a" * 32, "high", 2, 9, 4, "two", "https://other/get.php"),
    Result("", "x", 1, 1, 0, "x", "https://example.com/a"),
    Result("", "y", 1, 1, 0, "y", "https://example.com/b"),
])
assert len(direct) == 3 and direct[0].seeders == 9, direct
assert direct[0].seeders == 9 and direct[0].leechers == 4, "swarm counts are not summed"
upper_magnet = "MAGNET:?xt=urn:btih:" + h40.upper()
assert result_identity(Result("", "x", 0, 0, 0, "x", upper_magnet)) == "btih:" + h40
tied = [
    Result(h40, "z-name", 9, 5, 4, "same", "same-uri", 2, 3, "page", "TV"),
    Result(h40, "a-name", 1, 5, 4, "same", "same-uri", 1, 2, "page", "Movies"),
]
assert dedupe(tied) == dedupe(list(reversed(tied))), "metadata tie rule must be deterministic"
assert dedupe(tied)[0].name == "a-name", "lexicographically smallest copied metadata wins"
# Torznab request construction, XML namespaces, relative enclosures and errors.
ep = "https://indexer.example/api?x=1&Q=old&q=twice&T=caps&t=twice&apikey=endpoint#f"
req = torznab_request_url(ep, "matrix", "separate")
qp = urllib.parse.parse_qsl(urllib.parse.urlsplit(req).query)
assert qp.count(("Q", "matrix")) == 1 and qp.count(("T", "search")) == 1, qp
assert ("x", "1") in qp and ("apikey", "separate") in qp and "limit" not in dict(qp)
aliases = "apikey=first&APIKEY=second&api_key=third&access-token=fourth&secret=fifth"
kept = urllib.parse.parse_qsl(urllib.parse.urlsplit(
    torznab_request_url("https://x.test/api?" + aliases, "q")).query)
assert kept.count(("apikey", "first")) == 1 and ("APIKEY", "second") not in kept
assert ("api_key", "third") not in kept and ("access-token", "fourth") in kept
replaced = urllib.parse.parse_qsl(urllib.parse.urlsplit(
    torznab_request_url("https://x.test/api?" + aliases, "q", "one")).query)
assert ("apikey", "one") in replaced and ("api_key", "third") not in replaced
assert ("access-token", "fourth") in replaced and ("secret", "fifth") in replaced
tx = '''<rss xmlns:t="http://torznab.com/schemas/2015/feed"
  xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel><item>
  <title>Fixture</title><link>https://indexer.example/details/1</link>
  <enclosure url="downloads/1.torrent" type="application/x-bittorrent" length="999"/>
  <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
  <t:attr name="infohash" value="dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"/>
  <n:attr name="seeders" value="8"/><n:attr name="peers" value="11"/>
  <n:attr name="size" value="123"/><n:attr name="files" value="bad"/>
  <n:attr name="category" value="5070"/></item></channel></rss>'''
tr = parse_torznab(tx, "https://indexer.example/api", "fixture")
assert len(tr) == 1 and tr[0].magnet == "https://indexer.example/downloads/1.torrent", tr
assert (tr[0].size, tr[0].seeders, tr[0].leechers, tr[0].num_files, tr[0].group) == (123, 8, 3, None, "Anime")
magxml = f'''<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel><item>
  <title>Magnet</title><n:attr name="magneturl" value="{html.escape(build_magnet(h40, 'm'))}"/>
  <n:attr name="seeders" value="-1"/><n:attr name="category" value="2000"/>
  </item></channel></rss>'''
assert parse_torznab(magxml, "https://x.test/api")[0].group == "Movies"
policy_xml = f'''<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
  <item><title>HTML enclosure</title><enclosure url="https://x.test/details/1" type="text/html"/>
  <link>https://x.test/details/1</link><guid>https://x.test/details/1</guid>
  <n:attr name="magneturl" value="{html.escape(build_magnet(h40, 'm'))}"/></item>
  <item><title>Opaque download</title><link>https://x.test/f/opaque-id</link>
  <guid>https://x.test/details/2</guid></item>
  <item><title>GUID only</title><guid>https://x.test/details/3</guid></item>
  <item><title>Magnet with page</title><n:attr name="magneturl" value="{html.escape(build_magnet('a' * 40, 'a'))}"/>
  <comments>https://x.test/details/4</comments></item>
  </channel></rss>'''
policy = parse_torznab(policy_xml, "https://x.test/api")
assert [r.name for r in policy] == ["HTML enclosure", "Opaque download", "Magnet with page"]
assert policy[0].magnet.lower().startswith("magnet:?") and policy[1].magnet.endswith("/f/opaque-id")
assert policy[2].page == "https://x.test/details/4"
bad_candidates = f'''<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
  <item><title>Fallback</title><enclosure url="https://[bad/download.torrent" type="application/x-bittorrent"/>
  <n:attr name="magneturl" value="{html.escape(build_magnet(h40, 'fallback'))}"/></item>
  <item><title>Bad port</title><link>https://x.test:99999/download</link></item>
  </channel></rss>'''
candidates = parse_torznab(bad_candidates, "https://x.test/api")
assert [r.name for r in candidates] == ["Fallback"], "bad candidate must not poison valid fallback"
for bad in ('<rss><error code="100" description="nope"/></rss>', '<rss><bad>'):
    try:
        parse_torznab(bad, "https://x.test/api?apikey=secret")
        assert False, "Torznab errors must fail"
    except SourceError as e:
        assert "secret" not in str(e) and "pass" not in str(e)
try:
    validate_torznab_url("https://user:pass@x.test/api")
    assert False, "userinfo must be rejected"
except SourceError:
    pass
for bad_url in ("https://x .test/api", "https://x.test:0/api", "https://x.test:no/api",
                "https://x.test/api\n?x=1", "https://[bad/api"):
    try:
        validate_torznab_url(bad_url)
        assert False, "invalid host or port must be rejected"
    except SourceError:
        pass
assert validate_torznab_url("HTTPS://[2001:DB8::1]:443/api") == "https://[2001:db8::1]:443/api"
secret = "a+b/c"
red = redact("https://u:p@x.test/api?apikey=a%2Bb%2Fc\n" + secret, (secret,))
assert secret not in red and "a%2Bb%2Fc" not in red and "u:p" not in red and "\n" not in red
fallback = redact_url("https://user:pass@[bad/api?access-token=hidden&safe=ok")
assert "user:pass" not in fallback and "hidden" not in fallback and "safe=ok" in fallback
spellings = ("apikey", "api_key", "api-key", "api", "key", "token", "access_token",
              "access-token", "auth_token", "refresh_token", "passkey", "pass", "password",
              "pwd", "secret", "client_secret", "auth", "authorization", "customtoken",
              "customsecret", "token_value", "api_token_v2", "secret_key", "my_secret_id",
              "secretValue")
assert all(_is_sensitive_name(name) for name in spellings)
for i, name in enumerate(spellings):
    value = f"s{i} +/value"
    src = make_torznab_source(TorznabFeed(f"secret-{i}",
                              f"https://x.test/api?{name}={urllib.parse.quote_plus(value)}"))
    assert src.secrets == (value,), "secret collection failed"
    reflected = " ".join((value, urllib.parse.quote(value, safe=""),
                          urllib.parse.quote_plus(value)))
    search_secret = Search("", [Source(src.id, "Secret", "Other",
                           lambda q, text=reflected: (_ for _ in ()).throw(SourceError(text)),
                           secrets=src.secrets)])
    secret_update = search_secret.updates.get(timeout=3)
    assert secret_update.results is None, "secret source did not fail"
    assert all(form not in secret_update.error for form in
               (value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value))), \
        "secret redaction failed"
original_urlopen = urllib.request.urlopen
def invalid_urlopen(*args, **kwargs):
    raise http.client.InvalidURL("bad refresh_token=must-not-leak")
urllib.request.urlopen = invalid_urlopen
try:
    try:
        fetch("https://x.test/api?refresh_token=must-not-leak", retries=0)
        assert False, "invalid URL did not fail"
    except SourceError as e:
        assert str(e) == "invalid URL", "invalid URL leaked details"
    direct_source = make_torznab_source(TorznabFeed(
        "direct", "https://x.test/api?refresh_token=must-not-leak"))
    try:
        direct_source.fn("query")
        assert False, "direct source invalid URL did not fail"
    except SourceError as e:
        assert str(e) == "invalid URL", "direct source leaked URL details"
finally:
    urllib.request.urlopen = original_urlopen
assert _rfc822_unix("Mon, 01 Jan 2024 00:00:00") == 1704067200
# Query operators are local; malformed syntax remains in the remote query.
fixed = 2_000_000_000
before = time.time()
direct_query = LocalQuery("")
assert before <= direct_query.now <= time.time(), "direct LocalQuery timestamp"
qr = parse_query('matrix "special edition" -cam seeders:>=5 size:1GiB age:<2d files:>1 source:yts group:movies', fixed)
assert qr.remote == "matrix special edition" and qr.terms == ("matrix", "special edition") and not qr.malformed, qr
rr = Result(h40, "Matrix Special Edition", 2 * 1024 ** 3, 5, 1, "yts", "m",
            fixed - 3600, 2, group="Movies")
assert matches_query(rr, qr)
assert not matches_query(Result(h40, "Matrix Special Edition CAM", rr.size, 5, 1, "yts", "m",
                                rr.added, 2, group="Movies"), qr)
empty_phrase = parse_query('ok "" -""')
assert empty_phrase.malformed == ('""', '-""') and not empty_phrase.exclusions
threshold = parse_query("age:>=1h", fixed)
assert matches_query(Result(h40, "x", 0, 0, 0, "yts", "m", fixed - 3600), threshold)
assert not matches_query(Result(h40, "x", 0, 0, 0, "yts", "m", fixed - 3599), threshold)
malformed = parse_query('x bogus:1 size:nope "unfinished')
assert malformed.malformed == ("bogus:1", "size:nope", '"unfinished') and "bogus:1" in malformed.remote
assert malformed.terms == ("x",), "malformed tokens are not mandatory local terms"
unfinished = parse_query('"special edition" "unfinished')
assert unfinished.terms == ("special edition",) and unfinished.remote == 'special edition "unfinished'
assert unfinished.malformed == ('"unfinished',), unfinished
absurd = parse_query(f"size:{'9' * 10000}GB age:{'9' * 10000}y seeders:{'9' * 10000}")
assert len(absurd.malformed) == 3 and not absurd.filters and not absurd.terms
assert not matches_query(Result(h40, "x", 1, 1, 0, "yts", "m"), parse_query("age:1d", fixed))
# Retry accounting, in-flight suppression and secret-safe failures.
gate = threading.Event()
blocker = Source("block", "Block", "Other", lambda q: (gate.wait(2), [rr])[1])
search = Search("", [blocker])
assert search.total == 1 and search.retry(["block", "missing"]) == ()
gate.set()
assert search.updates.get(timeout=3).results == [rr]
assert search.retry(["block", "missing"]) == ("block",) and search.total == 2
assert search.updates.get(timeout=3).results == [rr]
failing = Search("", [Source("bad", "Bad", "Other",
                            lambda q: (_ for _ in ()).throw(SourceError("token=s3cr3t")),
                            secrets=("s3cr3t",))])
fu = failing.updates.get(timeout=3)
assert fu.results is None and "s3cr3t" not in fu.error and "***" in fu.error
starts: list[str] = []
original_start = threading.Thread.start
def broken_start(thread) -> None:
    starts.append(thread.name)
    raise RuntimeError("startup leaked-start-secret")
threading.Thread.start = broken_start
duplicate = Source("dup", "Dup", "Other", lambda q: [])
try:
    try:
        Search("", [duplicate, duplicate])
        assert False, "duplicate source ids must fail"
    except ValueError:
        pass
    assert not starts, "duplicate ids must fail before thread startup"
    start_failure = Search("", [Source("start", "Start", "Other", lambda q: [],
                                           secrets=("leaked-start-secret",))])
finally:
    threading.Thread.start = original_start
failed_start = start_failure.updates.get(timeout=1)
assert start_failure.total == 1 and not start_failure.in_flight and failed_start.results is None
assert "leaked-start-secret" not in failed_start.error and "***" in failed_start.error
try:
    merged[0].seeders = 0
    assert False, "results must be immutable"
except FrozenInstanceError:
    pass
# browse flag: only search-only sources are excluded from empty-query Latest
assert {s.id for s in SOURCES if not s.browse} == \
    {"libgen", "annas", "knaben", "torrentgalaxy", "torrents-csv", "audiobookbay"}, \
    [s.id for s in SOURCES if not s.browse]
# tracker-list parse: keeps only announce urls, junk lines dropped
tl = _parse_trackers("udp://a:1/announce\n\n# comment\nhttps://b/announce\nnot a url\n")
assert tl == ["udp://a:1/announce", "https://b/announce"], tl
x_page = (
    '<table class="table-list"><tbody><tr>'
    '<td class="coll-1 name"><a href="/sort-here/">x</a>'
    '<a href="/torrent/42/The-Matrix-1999/">The Matrix 1999</a></td>'
    '<td class="coll-2 seeds">1234</td>'
    '<td class="coll-3 leeches">56</td>'
    '<td class="coll-4 size">1.5 GB<span>x</span></td></tr></tbody></table>'
)
xr = _x_rows(x_page)
assert len(xr) == 1 and xr[0]["name"] == "The Matrix 1999", xr
assert xr[0]["seeders"] == 1234 and xr[0]["leechers"] == 56, xr
assert xr[0]["size"] == 1_500_000_000 and xr[0]["path"] == "/torrent/42/The-Matrix-1999/", xr
x_urls: list[str] = []
original_fetch = globals()["fetch"]
def x_fetch(url: str, **kw) -> str:
    x_urls.append(url)
    return x_page if "/category-search/" in url else f'<a href="{build_magnet(h40, "The Matrix")}">m</a>'
globals()["fetch"] = x_fetch
try:
    quoted_x = _x1337(parse_query('"The Matrix"').remote, "Movies", "x1337-movies")
finally:
    globals()["fetch"] = original_fetch
assert len(quoted_x) == 1 and all("%22" not in url for url in x_urls), \
    "1337x must receive clean words from quoted phrases"
# knaben category -> group mapping
assert _knaben_group("Movies / HD") == "Movies"
assert _knaben_group("PC / Games") == "Games"
assert _knaben_group("Anime / Subbed") == "Anime"
assert _knaben_group("XXX / Video") == "SKIP"
assert _knaben_group("Books / EBooks") == "Books"
# TGx + FitGirl parsers (synthetic pages; fetch monkeypatched — real sites unverified here)
_g = globals()
_of = _g["fetch"]
try:
    _g["fetch"] = lambda *a, **k: (
        'x<div class="tgxtablerow">'
        '<a href="/torrent/55/The-Matrix/">The Matrix</a>'
        '<a href="magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c'
        '&dn=The+Matrix+1999">m</a>'
        "<font color='green'><b>1234</b></font>"
        "<font color='#ff0000'><b>56</b></font>"
        '<span class="badge">1.5 GB</span></div>')
    tg = _tgx("matrix")
    assert len(tg) == 1 and tg[0].name == "The Matrix 1999", tg
    assert tg[0].seeders == 1234 and tg[0].leechers == 56 and tg[0].size == 1_500_000_000, tg
    assert tg[0].page.endswith("/torrent/55/The-Matrix/"), tg[0].page
    _g["fetch"] = lambda *a, **k: (
        '<rss><channel><item><title>Cyberpunk 2077</title>'
        '<link>https://fitgirl-repacks.site/cyberpunk-2077/</link>'
        '<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>'
        '<description>x <a href="magnet:?xt=urn:btih:'
        'dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=cp">here</a></description>'
        '</item></channel></rss>')
    dd = _fitgirl("cyberpunk")
    assert len(dd) == 1 and dd[0].source == "fitgirl" and dd[0].name == "Cyberpunk 2077", dd
    assert dd[0].page == "https://fitgirl-repacks.site/cyberpunk-2077/", dd[0].page
    _g["fetch"] = lambda *a, **k: (
        '<table><tr>'
        '<td><a href="edition.php?id=1">x</a>'
        '<font size=1 color="gray"><br>The Hobbit <br></font></td>'
        '<td>J.R.R. Tolkien</td><td>Allen</td><td>1937</td><td>English</td><td>300</td>'
        '<td><a href="/file.php?id=1">2 MB</a></td><td>epub</td>'
        '<td><a href="/get.php?md5=aabbccddeeff00112233445566778899">Libgen</a></td>'
        '</tr></table>')
    lg = _libgen("hobbit")
    assert len(lg) == 1 and lg[0].source == "libgen" and lg[0].group == "Books", lg
    assert lg[0].info_hash == "aabbccddeeff00112233445566778899", lg[0].info_hash
    assert lg[0].name == "[EPUB] The Hobbit — J.R.R. Tolkien", lg[0].name
    assert lg[0].size == 2_000_000, lg[0].size
    assert lg[0].magnet == "https://libgen.li/get.php?md5=aabbccddeeff00112233445566778899", lg[0].magnet
    assert _libgen("") == [], "libgen browse is search-only"
    _g["fetch"] = lambda *a, **k: (
        '<a href="/md5/aabbccddeeff00112233445566778899" class="custom-a font-semibold text-lg leading-[1.2]">Sapiens: A Brief History</a>'
        '<a href="/search?q=x" class="custom-a text-sm"><span class="icon-[mdi--user-edit] text-base"></span> Yuval Noah Harari</a>'
        '<div class="text-gray-800 dark:text-slate-400 font-semibold text-sm leading-[1.2] mt-2">✅ English [en] · EPUB · 3.3MB · 2015 · 📘 Book (non-fiction)</div>')
    an = _annas("harari")
    assert len(an) == 1 and an[0].source == "annas" and an[0].group == "Books", an
    assert an[0].info_hash == "aabbccddeeff00112233445566778899", an[0].info_hash
    assert an[0].name == "[EPUB] Sapiens: A Brief History — Yuval Noah Harari", an[0].name
    assert an[0].size == 3_300_000, an[0].size
    assert an[0].magnet == "https://libgen.li/get.php?md5=aabbccddeeff00112233445566778899", an[0].magnet
    assert an[0].page == "https://annas-archive.org/md5/aabbccddeeff00112233445566778899", an[0].page
    assert _annas("") == [], "annas search-only"
    _g["fetch"] = lambda *a, **k: json.dumps({"torrents": [
        {"infohash": "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c", "name": "The Matrix 1999",
         "size_bytes": 1_500_000_000, "seeders": 1234, "leechers": 56, "created_unix": 1700000000},
        {"infohash": "tooshort", "name": "bad row", "size_bytes": 0, "seeders": 0, "leechers": 0},
    ]})
    tc = _torrentscsv("matrix")
    assert len(tc) == 1 and tc[0].source == "torrents-csv" and tc[0].name == "The Matrix 1999", tc
    assert tc[0].info_hash == "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c", tc[0].info_hash
    assert tc[0].seeders == 1234 and tc[0].leechers == 56 and tc[0].size == 1_500_000_000, tc
    assert tc[0].added == 1700000000 and tc[0].magnet.startswith("magnet:?"), tc
    assert _torrentscsv("") == [], "torrents-csv search-only"
    # LimeTorrents: RSS with byte sizes, swarm in description, hash in enclosure
    _g["fetch"] = lambda *a, **k: (
        '<rss><channel><item><title>Oppenheimer 2023 1080p</title>'
        '<pubDate>14 Aug 2026 11:15:12 +0200</pubDate>'
        '<link>https://www.limetorrents.fun/Oppenheimer-torrent-19886478.html</link>'
        '<category>Movies</category><size>2510928092</size>'
        '<description>Seeds: 24 , Leechers 6</description>'
        '<enclosure url="https://itorrents.net/torrent/'
        + h40.upper() + '.torrent?title=x" /></item>'
        '<item><title>Some Show S01E01</title><category>TV shows</category>'
        '<size>100</size><description>Seeds: 1 , Leechers 0</description>'
        '<enclosure url="https://itorrents.net/torrent/' + h40.upper() + '.torrent" />'
        '</item></channel></rss>')
    lm = _lime("oppenheimer", "Movies", "lime-movies")
    assert len(lm) == 1 and lm[0].source == "lime-movies", lm
    assert lm[0].name == "Oppenheimer 2023 1080p" and lm[0].info_hash == h40, lm
    assert lm[0].size == 2510928092 and lm[0].seeders == 24 and lm[0].leechers == 6, lm
    assert lm[0].page.endswith("-19886478.html") and lm[0].added, lm
    lt = _lime("some show", "TV", "lime-tv")
    assert len(lt) == 1 and lt[0].name == "Some Show S01E01" and lt[0].source == "lime-tv", lt
    # TokyoTosho: base32 magnet, size and details page inside the description
    _g["fetch"] = lambda *a, **k: (
        '<rss><channel><item><category>Anime</category>'
        '<title>[Grp] Show - 01 [1080p].mkv</title>'
        '<description><![CDATA[<a href="https://nyaa.si/download/1.torrent">Torrent Link</a><br />'
        '<a href="magnet:?xt=urn:btih:' + b32 + '&tr=http://t/announce">Magnet Link</a><br />'
        '<a href="https://www.tokyotosho.info/details.php?id=2106821">Tokyo Tosho</a><br />'
        'Size: 1.26GB<br />]]></description>'
        '<pubDate>Mon, 31 Aug 2026 08:17:10 GMT</pubDate></item></channel></rss>')
    tt = _tokyotosho("show")
    assert len(tt) == 1 and tt[0].source == "tokyotosho" and tt[0].info_hash == h40, tt
    assert tt[0].name == "[Grp] Show - 01 [1080p].mkv" and tt[0].size == 1_260_000_000, tt
    assert tt[0].page == "https://www.tokyotosho.info/details.php?id=2106821", tt[0].page
    assert tt[0].added and tt[0].magnet.startswith("magnet:?xt=urn:btih:"), tt
    # AudiobookBay: two-step — search page lists posts, hash + size on the post
    _g["fetch"] = lambda url, *a, **k: (
        '<div class="postTitle"><h2><a href="/abss/dune-frank-herbert/" '
        'rel="bookmark">Dune - Frank Herbert</a></h2></div>'
        if "?s=" in url else
        "<tr><td>Info Hash:</td>\n<td>" + h40.upper() + "</td></tr>"
        "<tr><td>File Size:</td>\n<td><span style='color:#00f;'>1.12</span> GBs</td></tr>")
    ab = _audiobookbay("Dune")
    assert len(ab) == 1 and ab[0].source == "audiobookbay" and ab[0].info_hash == h40, ab
    assert ab[0].name == "Dune - Frank Herbert" and ab[0].size == 1_120_000_000, ab
    assert ab[0].page == "https://audiobookbay.lu/abss/dune-frank-herbert/", ab[0].page
    assert ab[0].magnet.startswith("magnet:?xt=urn:btih:"), ab
finally:
    _g["fetch"] = _of
# honest errors: Cloudflare interstitials, readable network failures, per-source timing
_cf = cloudflare_challenge
assert _cf(403, {"cf-mitigated": "challenge"}, "anything")
assert _cf(503, {}, "<title>Just a moment...</title><script>window._cf_chl_opt={}</script>")
assert _cf(403, {}, '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>')
assert not _cf(403, {}, "<h1>Forbidden</h1>") and not _cf(404, {}, "_cf_chl_ everywhere")
assert _cf(200, {}, "<title>Just a moment...</title>_cf_chl_opt"), "small 200 challenge page"
assert not _cf(200, {}, "A torrent called Just a moment... 1080p"), "no markers: real content"
assert not _cf(200, {}, "x" * 200_000 + "_cf_chl_ Just a moment"), "huge pages are never scanned"
import ssl as _ssl
assert "DNS lookup failed" in _why(socket.gaierror(8, "nodename nor servname"))
assert "refused or reset" in _why(ConnectionResetError()) and "refused or reset" in _why(ConnectionRefusedError())
assert _why(urllib.error.URLError(socket.gaierror(8, "x"))).startswith("DNS lookup failed"), "URLError is unwrapped"
assert _why(TimeoutError("timed out")) == "timed out" and _why(OSError("The read operation timed out")) == "timed out"
assert "TLS error" in _why(_ssl.SSLError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
assert _why(OSError("odd")) == "odd"
import http.server as _hs
_hits = []
class _CfH(_hs.BaseHTTPRequestHandler):
    def do_GET(self):
        _hits.append(self.path)
        code, hdr, body = {
            "/hdr": (403, {"cf-mitigated": "challenge"}, b"blocked"),
            "/503": (503, {}, b"<title>Just a moment...</title>_cf_chl_opt"),
            "/200": (200, {}, b"<title>Just a moment...</title>_cf_chl_opt"),
            "/403": (403, {}, b"<h1>Forbidden</h1>"),
            "/ok": (200, {}, b"real page"),
        }[self.path]
        self.send_response(code)
        for k, v in hdr.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
_srv = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _CfH)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_base = f"http://127.0.0.1:{_srv.server_address[1]}"
try:
    for path in ("/hdr", "/503", "/200"):
        _hits.clear()
        try:
            fetch(_base + path, retries=2)
            raise AssertionError(f"{path}: challenge accepted as content")
        except SourceError as e:
            assert str(e) == CLOUDFLARE_MSG, (path, e)
        assert len(_hits) == 1, f"{path}: a challenge must not be retried ({len(_hits)} requests)"
    _hits.clear()
    try:
        fetch(_base + "/403", retries=2); raise AssertionError("403 accepted")
    except SourceError as e:
        assert str(e) == "HTTP 403", e
    assert fetch(_base + "/ok") == "real page"
finally:
    _srv.shutdown()
    _srv.server_close()
try:
    fetch(_base + "/ok", retries=0, timeout=2)
    raise AssertionError("server is down; fetch should fail")
except SourceError as e:
    assert "refused or reset" in str(e), e
_slow = Search("", [Source("slow", "Slow", "Other", lambda q: (time.sleep(0.15), [])[1]),
                    Source("boom", "Boom", "Other", lambda q: (_ for _ in ()).throw(SourceError("x")))])
_got = {}
while len(_got) < 2:
    u = _slow.updates.get(timeout=3); _got[u.source] = u
assert _got["slow"].elapsed >= 0.14 and _got["boom"].elapsed < 0.14, {k: v.elapsed for k, v in _got.items()}
assert SourceUpdate("a", []).elapsed == 0.0, "elapsed is optional"

# release parser + res:/codec: operators
_pr = parse_release
assert _pr("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-GRP") == \
    Release(2160, "WEB", "x265", "DV", ("Atmos", "DD+")), _pr("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-GRP")
assert _pr("Oppenheimer.2023.1080p.BluRay.x264-YIFY").badge() == "1080p BD"
assert _pr("Movie.2023.2160p.UHD.BluRay.REMUX.HDR.HEVC").badge() == "2160p REMUX HDR"
assert _pr("Show.S01E01.720p.HDTV.x264").badge() == "720p HDTV"
assert _pr("New.Movie.2024.HDCAM.x264").bad and _pr("New.Movie.2024.HDTS").kind == "TS"
assert _pr("Cam.2018.1080p.WEB-DL").kind == "WEB" and not _pr("Cam.2018.1080p.WEB-DL").bad, "title 'Cam' is not a CAM rip"
assert _pr("Some Book.epub") == Release() and _pr("Some Book.epub").badge() == ""
assert _pr("Movie 4K HDR10+ x265").res == 2160 and _pr("Movie 4K HDR10+ x265").hdr == "HDR"
assert _pr("Movie.1080p.AVC").codec == "x264" and _pr("Movie.Extended.Cut.1080p").extras == ("Extended",)
_rq = [Result("", n, 1, 5, 0, "s", "m") for n in (
    "A.2160p.WEB-DL.x265", "B.1080p.BluRay.x264", "C.720p.HDTV.x264", "D.no.tags")]
def _names(q): return [r.name[0] for r in _rq if matches_query(r, q)]
assert _names("res:>=1080") == ["A", "B"] and _names("res:1080") == ["B"] and _names("res:4k") == ["A"]
assert _names("res:<=720p") == ["C"] and _names("res:<1080") == ["C"], "unknown res never matches"
assert _names("codec:hevc") == ["A"] and _names("codec:h264") == ["B", "C"] and _names("codec:x264 res:720") == ["C"]
assert parse_query("codec:nope").malformed == ("codec:nope",) and parse_query("res:big").malformed == ("res:big",)
assert parse_query("codec:>x265").malformed and parse_query("res:1080 dune").remote == "dune"
print("pure logic ok")

# live — best-effort; proves the pipeline + real parsing without requiring
# every site to be up. Asserts every returned row is well-formed.
s = Search("the matrix")
counts: dict[str, int] = {}
errors: dict[str, str] = {}
pages: dict[str, int] = {}
got = 0
end = time.monotonic() + 25
while got < s.total and time.monotonic() < end:
    try:
        u = s.updates.get(timeout=max(0.1, end - time.monotonic()))
    except queue.Empty:
        break
    got += 1
    if u.results is None:
        errors[u.source] = u.error
    else:
        counts[u.source] = len(u.results)
        for r in u.results:
            # torrent rows: btih + magnet. direct-download rows (libgen): a
            # 32-hex md5 + an http link. Both must be a grabbable uri aria2 takes.
            if r.magnet.lower().startswith("magnet:?"):
                assert re.fullmatch(r"[a-f0-9]{40}", r.info_hash), f"bad hash from {u.source}: {r.info_hash!r}"
            else:
                assert r.magnet.lower().startswith("http"), f"bad uri from {u.source}: {r.magnet!r}"
                assert re.fullmatch(r"[a-f0-9]{32}", r.info_hash), f"bad md5 from {u.source}: {r.info_hash!r}"
            if r.page:
                assert r.page.startswith("http"), f"bad page from {u.source}: {r.page!r}"
                pages[u.source] = pages.get(u.source, 0) + 1
print(f"sources answered: {got}/{s.total}")
for sid, n in sorted(counts.items()):
    print(f"  {sid:14} {n} results  ({pages.get(sid, 0)} with pages)")
for sid, err in sorted(errors.items()):
    print(f"  {sid:14} ERROR: {err[:60]}")
total = sum(counts.values())
if total == 0:
    print("\n[warn] no live results — sources blocked/offline or no network.")
else:
    print(f"\nPhase 2 selftest passed — {total} results, all well-formed.")



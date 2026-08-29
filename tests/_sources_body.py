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
    {"libgen", "annas", "knaben", "torrentgalaxy", "torrents-csv"}, \
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
finally:
    _g["fetch"] = _of
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



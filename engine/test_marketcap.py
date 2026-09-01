"""
test_marketcap.py — the size floor.

A filing says nothing about how big the business behind it is. A shell company
reporting 100% profit growth on Rs 1 crore of revenue clears rule 1 exactly as
a real business does, so the floor is what separates them.

The one behaviour worth being careful about is what happens when the market cap
CANNOT be determined. Blocking there would silently drop real alerts whenever
BSE is unreachable or a company is NSE-only, so unknown means allowed — and
recorded.

Network and database are stubbed; `test_marketcap_live.py` covers the real
lookup. Run: pytest test_marketcap.py  or  python test_marketcap.py
"""
import config
import marketcap


class FakeStore:
    """Stands in for the market_caps table."""

    def __init__(self, cached=None):
        self.cached = cached or {}
        self.saved = {}

    def fetch(self, symbol, ttl_days):
        return self.cached.get(symbol.upper())

    def save(self, symbol, value, scrip=None):
        self.saved[symbol.upper()] = value


def with_stubs(cached=None, live=None, fn=None):
    store = FakeStore(cached)
    orig_fetch, orig_save, orig_live = (
        marketcap.db.fetch_market_cap, marketcap.db.save_market_cap, marketcap._fetch)
    marketcap.db.fetch_market_cap = store.fetch
    marketcap.db.save_market_cap = store.save
    # _fetch now also takes the company name, used to resolve a scrip that
    # the frozen master does not carry.
    marketcap._fetch = lambda sym, name=None: (
        (live or {}).get(sym.upper()), "999")
    try:
        return fn(), store
    finally:
        (marketcap.db.fetch_market_cap, marketcap.db.save_market_cap,
         marketcap._fetch) = orig_fetch, orig_save, orig_live


# -----------------------------------------------------------------------------

def test_below_the_floor_is_blocked():
    (ok, cap, why), _ = with_stubs(
        cached={"TINY": 22.0}, fn=lambda: marketcap.passes_floor("TINY"))
    assert ok is False
    assert cap == 22.0
    assert "below" in why


def test_at_the_floor_exactly_is_allowed():
    """'equal to or greater than' — the boundary belongs to the allowed side."""
    (ok, cap, _), _ = with_stubs(
        cached={"EDGE": float(config.MIN_MARKET_CAP_CR)},
        fn=lambda: marketcap.passes_floor("EDGE"))
    assert ok is True
    assert cap == config.MIN_MARKET_CAP_CR


def test_a_rupee_below_the_floor_is_blocked():
    (ok, _, _), _ = with_stubs(
        cached={"EDGE": float(config.MIN_MARKET_CAP_CR) - 0.01},
        fn=lambda: marketcap.passes_floor("EDGE"))
    assert ok is False


def test_above_the_floor_is_allowed():
    (ok, cap, _), _ = with_stubs(
        cached={"BIG": 6244.0}, fn=lambda: marketcap.passes_floor("BIG"))
    assert ok is True and cap == 6244.0


# -----------------------------------------------------------------------------
#  The failure mode that matters
# -----------------------------------------------------------------------------

def test_unknown_market_cap_is_allowed_not_blocked():
    """
    BSE unreachable, or an NSE-only listing with no scrip code. Blocking here
    would silently drop real alerts whenever a data source has a bad day.
    """
    (ok, cap, why), _ = with_stubs(cached={}, live={},
                                   fn=lambda: marketcap.passes_floor("NOSUCH"))
    assert ok is True
    assert cap is None
    assert "unknown" in why


def test_the_floor_can_be_switched_off():
    old = config.MIN_MARKET_CAP_CR
    config.MIN_MARKET_CAP_CR = 0
    try:
        (ok, _, why), store = with_stubs(
            cached={"TINY": 1.0}, fn=lambda: marketcap.passes_floor("TINY"))
        assert ok is True
        assert "disabled" in why
    finally:
        config.MIN_MARKET_CAP_CR = old


# -----------------------------------------------------------------------------
#  Caching
# -----------------------------------------------------------------------------

def test_a_cached_value_is_not_refetched():
    calls = {"n": 0}

    def counting(sym):
        calls["n"] += 1
        return 500.0, "1"

    orig = marketcap._fetch
    marketcap._fetch = counting
    try:
        (_, _, _), store = with_stubs(cached={"CACHED": 500.0},
                                      fn=lambda: marketcap.passes_floor("CACHED"))
    finally:
        marketcap._fetch = orig
    assert calls["n"] == 0, "hit the network despite a cached value"


def test_a_fresh_lookup_is_stored():
    (ok, cap, _), store = with_stubs(cached={}, live={"NEW": 750.0},
                                     fn=lambda: marketcap.passes_floor("NEW"))
    assert ok is True and cap == 750.0
    assert store.saved.get("NEW") == 750.0, "fetched value was not cached"


def test_symbols_are_matched_case_insensitively():
    (ok, cap, _), _ = with_stubs(cached={"TINY": 22.0},
                                 fn=lambda: marketcap.passes_floor("tiny"))
    assert ok is False and cap == 22.0


# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
#  Resolving a company BSE's frozen scrip master has never heard of
#
#  bse_scrips.json is a snapshot. A symbol missing from it had no scrip, so no
#  market cap, so the size floor was never applied to it AT ALL — 13% of alerts
#  in one week, and two of those were microcaps the floor exists to block:
#  GLOBAL at Rs 26 Cr and JNPR at Rs 32 Cr, both waved through.
# -----------------------------------------------------------------------------

SEARCH_HTML = (
    "<li class='quotemenu quotemenuselect' onclick=\"liclick('505703',"
    "'Satani Bearings Ltd')\"><a><strong>SATANI</strong> BEARINGS LTD<br />"
    "<span><strong>SATANI</strong>BRG&nbsp;&nbsp;INE498D01012&nbsp;&nbsp;505703"
    "</span></a></li>")


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "text/plain"}

    def json(self):
        return self.text


def _search(html):
    """Run search_scrip against a canned BSE response."""
    saved = marketcap.requests.get
    marketcap.requests.get = lambda *a, **k: FakeResponse(html)
    marketcap._SEARCHED.clear()
    try:
        return marketcap.search_scrip("Satani Bearings Ltd", "SATANIBRG")
    finally:
        marketcap.requests.get = saved
        marketcap._SEARCHED.clear()


def test_a_scrip_is_recovered_from_bses_own_search():
    assert _search(SEARCH_HTML) == "505703"


def test_a_result_showing_our_ticker_wins_over_the_others():
    """Two candidates: the one carrying OUR ticker is the answer, not the first."""
    other = SEARCH_HTML.replace("505703", "999999").replace("SATANI</strong>BRG",
                                                            "OTHER</strong>CO")
    assert _search(other + SEARCH_HTML) == "505703"


def test_an_ambiguous_search_resolves_to_nothing():
    """
    Two results and no ticker match is a guess, and a wrong scrip means a wrong
    market cap — worse than no market cap, because it would be believed.
    """
    a = SEARCH_HTML.replace("505703", "111111").replace("SATANI</strong>BRG", "AA</strong>A")
    b = SEARCH_HTML.replace("505703", "222222").replace("SATANI</strong>BRG", "BB</strong>B")
    assert _search(a + b) is None


def test_an_empty_or_broken_response_is_survivable():
    for html in ("", "<html>nothing here</html>", "<li>no liclick</li>"):
        assert _search(html) is None, html


def test_the_search_is_asked_once_per_company():
    """Negatives are memoised too, or a symbol BSE cannot place is asked about
    on every filing it ever makes."""
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return FakeResponse("")

    saved = marketcap.requests.get
    marketcap.requests.get = counting
    marketcap._SEARCHED.clear()
    try:
        for _ in range(4):
            marketcap.search_scrip("Nowhere Ltd", "NOWHERE")
    finally:
        marketcap.requests.get = saved
        marketcap._SEARCHED.clear()
    assert calls["n"] == 1, "asked BSE {} times".format(calls["n"])


def test_the_frozen_master_still_wins_where_it_has_an_entry():
    """It maps to the NSE symbol everything else is keyed on; search is a fallback."""
    saved = marketcap._SYMBOL_TO_SCRIP
    marketcap._SYMBOL_TO_SCRIP = {"RAILTEL": "543265"}
    called = {"n": 0}
    saved_get = marketcap.requests.get
    marketcap.requests.get = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    try:
        assert marketcap.symbol_to_scrip("RAILTEL", "RailTel Corporation") == "543265"
        assert called["n"] == 0, "searched despite knowing the answer"
    finally:
        marketcap._SYMBOL_TO_SCRIP = saved
        marketcap.requests.get = saved_get


def test_without_a_company_name_there_is_nothing_to_search_on():
    saved = marketcap._SYMBOL_TO_SCRIP
    marketcap._SYMBOL_TO_SCRIP = {}
    try:
        assert marketcap.symbol_to_scrip("UNKNOWN") is None
        assert marketcap.symbol_to_scrip("UNKNOWN", "") is None
    finally:
        marketcap._SYMBOL_TO_SCRIP = saved


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  PASS  {}".format(name))
            passed += 1
        except AssertionError as e:
            print("  FAIL  {}  {}".format(name, e))
            failed += 1
        except Exception as e:
            print("  ERROR {}  {}: {}".format(name, type(e).__name__, e))
            failed += 1
    print("\n{} passed, {} failed".format(passed, failed))
    raise SystemExit(1 if failed else 0)

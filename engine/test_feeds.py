"""
test_feeds.py — how much of the day the feed can actually see.

The bug these exist for: NSE accepts `pageNo` and ignores it. Pages 1 to 4 all
return the same newest 20 rows, so paging could never reach past the 20 most
recent filings — while a date-range query returned 374 for the same period.
On a quiet Sunday 20 is the whole day and nothing looks wrong; on a results day
it is a few percent of it, and the rest was lost permanently.

No network: the two NSE queries are stubbed.

Run: pytest test_feeds.py   or   python test_feeds.py
"""
import config
import feeds


def item(n, day="30-Aug-2026"):
    return {"symbol": "SYM{}".format(n), "sm_name": "Company {}".format(n),
            "desc": "Announcement {}".format(n),
            "attchmntFile": "https://x.invalid/{}.pdf".format(n),
            "sort_date": "{} 10:00:00".format(day), "an_dt": "{} 10:00:00".format(day)}


class Feed:
    """Stands in for NSE: a small 'latest' window over a much larger day."""

    def __init__(self, latest=20, whole_day=374):
        self.latest_n = latest
        self.day_n = whole_day
        self.page_calls = 0
        self.day_calls = 0

    def page(self, page_no):
        self.page_calls += 1
        # The real API ignores page_no. Returning the same rows regardless is
        # the behaviour under test, not a simplification.
        return [item(i) for i in range(self.latest_n)]

    def day(self, frm, to):
        self.day_calls += 1
        return [item(i) for i in range(self.day_n)]


def _install(feed):
    saved = (feeds._nse_page, feeds._nse_day, feeds._last_sweep)
    feeds._nse_page = feed.page
    feeds._nse_day = feed.day
    feeds._last_sweep = 0.0
    return saved


def _restore(saved):
    feeds._nse_page, feeds._nse_day, feeds._last_sweep = saved


def test_the_sweep_sees_the_whole_day_not_just_the_latest():
    f = Feed(latest=20, whole_day=374)
    saved = _install(f)
    try:
        rows = feeds.fetch_nse()
    finally:
        _restore(saved)
    assert len(rows) == 374, \
        "only {} filings - the plain feed's 20 is not the day".format(len(rows))
    assert f.day_calls == 1


def test_a_cold_start_sweeps_immediately():
    """
    A restart must catch up on what it missed while down. If the sweep waited
    for its interval, a deploy during market hours would lose that window.
    """
    f = Feed()
    saved = _install(f)
    try:
        feeds.fetch_nse()
    finally:
        _restore(saved)
    assert f.day_calls == 1, "cold start did not sweep"


def test_the_sweep_does_not_run_every_cycle():
    """
    It pulls several hundred rows. Every 20s that is a lot of bandwidth for
    filings already stored — the plain feed is what keeps latency low between
    sweeps.
    """
    f = Feed()
    saved = _install(f)
    try:
        feeds.fetch_nse()          # cold: sweeps
        feeds.fetch_nse()          # within the interval: must not
        feeds.fetch_nse()
    finally:
        _restore(saved)
    assert f.day_calls == 1, "swept {} times in three cycles".format(f.day_calls)
    assert f.page_calls == 3, "the plain feed must still run every cycle"


def test_a_failed_sweep_does_not_lose_the_plain_feed():
    """NSE challenges and rate-limits. A failed sweep must degrade, not break."""
    f = Feed()
    f.day = lambda frm, to: []
    saved = _install(f)
    try:
        rows = feeds.fetch_nse()
    finally:
        _restore(saved)
    assert len(rows) == 20, "a failed sweep took the plain feed down with it"


def test_a_failed_sweep_is_retried_next_cycle():
    """It must not consume the interval, or a blip costs five minutes."""
    f = Feed()
    calls = {"n": 0}

    def failing_then_ok(frm, to):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [item(i) for i in range(374)]

    f.day = failing_then_ok
    saved = _install(f)
    try:
        feeds.fetch_nse()
        rows = feeds.fetch_nse()
    finally:
        _restore(saved)
    assert calls["n"] == 2, "did not retry the failed sweep"
    assert len(rows) == 374


def test_the_two_queries_are_deduped():
    """They overlap almost entirely; the same filing must appear once."""
    f = Feed(latest=20, whole_day=374)
    saved = _install(f)
    try:
        rows = feeds.fetch_nse()
    finally:
        _restore(saved)
    urls = [r["pdf_url"] for r in rows]
    assert len(urls) == len(set(urls)), "duplicate pdf_urls across the two queries"


def test_rows_carry_what_the_scraper_stores():
    f = Feed(latest=1, whole_day=1)
    saved = _install(f)
    try:
        rows = feeds.fetch_nse()
    finally:
        _restore(saved)
    r = rows[0]
    for k in ("company_symbol", "company_name", "title", "pdf_url",
              "announced_at", "exchange"):
        assert k in r, k
    assert r["exchange"] == "NSE"
    assert r["company_symbol"] == r["company_symbol"].upper()


def test_rows_without_a_pdf_are_dropped():
    """A filing with no attachment cannot be analysed, so it is not stored."""
    f = Feed(latest=0, whole_day=0)
    f.day = lambda frm, to: [
        item(1),
        {"symbol": "NOPDF", "sm_name": "X", "desc": "d", "attchmntFile": "",
         "sort_date": "30-Aug-2026 10:00:00"},
        {"symbol": "", "sm_name": "Y", "desc": "d",
         "attchmntFile": "https://x.invalid/y.pdf", "sort_date": "30-Aug-2026 10:00:00"},
    ]
    saved = _install(f)
    try:
        rows = feeds.fetch_nse()
    finally:
        _restore(saved)
    assert len(rows) == 1, [r["company_symbol"] for r in rows]


def test_the_sweep_interval_is_not_longer_than_the_alert_window():
    """
    A filing missed by the plain feed waits for the next sweep. That wait must
    stay well inside the window in which an alert is still worth sending.
    """
    assert config.NSE_SWEEP_INTERVAL_SEC <= config.WHATSAPP_MAX_AGE_MIN * 60 / 2, (
        "sweep every {}s against a {}min alert window".format(
            config.NSE_SWEEP_INTERVAL_SEC, config.WHATSAPP_MAX_AGE_MIN))


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

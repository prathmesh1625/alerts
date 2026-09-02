"""
test_pricehistory.py — the six-month price context, and the order-size buckets.

Both feed the dashboard, and both are pure enough to pin down exactly. No
network and no database.

Run: pytest test_pricehistory.py   or   python test_pricehistory.py
"""
import datetime

import api
import config
import db
import pricehistory


# -----------------------------------------------------------------------------
#  Which past session to compare against
# -----------------------------------------------------------------------------

def test_the_reference_date_is_about_six_months_back():
    today = datetime.date(2026, 9, 2)
    ref = pricehistory.reference_date(today=today, months=6)
    gap = (today - ref).days
    assert 175 <= gap <= 190, gap


def test_the_reference_date_is_never_a_weekend():
    """A Bhavcopy does not exist for a Saturday, so asking for one is wasted."""
    for d in range(0, 14):
        today = datetime.date(2026, 9, 2) + datetime.timedelta(days=d)
        assert pricehistory.reference_date(today=today).weekday() < 5


def test_the_window_is_configurable():
    today = datetime.date(2026, 9, 2)
    three = pricehistory.reference_date(today=today, months=3)
    twelve = pricehistory.reference_date(today=today, months=12)
    assert (today - three).days < (today - twelve).days


# -----------------------------------------------------------------------------
#  The change itself
# -----------------------------------------------------------------------------

class Stub:
    """Stands in for the database and the Bhavcopy download."""

    def __init__(self, then=None, now=None, have=True):
        self.then, self.now, self.have = then, now, have
        self.fetched = 0

    def install(self):
        self.saved = (db.have_session, db.close_on, db.latest_close,
                      pricehistory.bhavcopy.fetch, db.save_daily_volumes)
        db.have_session = lambda d: self.have
        db.close_on = lambda sym, d: self.then
        db.latest_close = lambda sym: self.now
        db.save_daily_volumes = lambda rows: len(rows)

        def fetch(d):
            self.fetched += 1
            return [{"symbol": "X", "session_date": d, "close": self.then}]

        pricehistory.bhavcopy.fetch = fetch
        pricehistory._LOADED.clear()

    def restore(self):
        (db.have_session, db.close_on, db.latest_close,
         pricehistory.bhavcopy.fetch, db.save_daily_volumes) = self.saved
        pricehistory._LOADED.clear()


def test_a_rise_is_reported_as_a_percentage():
    """100 -> 600 is +500%, which is the shape asked for."""
    st = Stub(then=100.0, now=600.0)
    st.install()
    try:
        then, now, pct = pricehistory.change("RAILTEL")
    finally:
        st.restore()
    assert (then, now) == (100.0, 600.0)
    assert abs(pct - 500.0) < 0.01


def test_a_fall_is_negative():
    st = Stub(then=400.0, now=300.0)
    st.install()
    try:
        _, _, pct = pricehistory.change("X")
    finally:
        st.restore()
    assert abs(pct - (-25.0)) < 0.01


def test_a_symbol_absent_from_bhavcopy_is_unknown_not_zero():
    """
    A BSE-only listing has no row in an NSE Bhavcopy. Reporting 0% would be a
    claim we cannot support; None says we do not know.
    """
    st = Stub(then=None, now=250.0)
    st.install()
    try:
        then, now, pct = pricehistory.change("SATANIBRG")
    finally:
        st.restore()
    assert then is None and pct is None


def test_a_zero_baseline_does_not_divide_by_zero():
    st = Stub(then=0.0, now=100.0)
    st.install()
    try:
        assert pricehistory.change("X")[2] is None
    finally:
        st.restore()


def test_a_blank_symbol_is_handled():
    assert pricehistory.change("") == (None, None, None)
    assert pricehistory.change(None) == (None, None, None)


def test_the_baseline_is_downloaded_once_for_all_symbols():
    """
    One Bhavcopy covers every symbol on that date. Downloading per symbol would
    turn a day of alerts into a day of downloads.
    """
    st = Stub(then=100.0, now=110.0, have=False)
    st.install()
    try:
        for sym in ("A", "B", "C", "D"):
            pricehistory.change(sym)
    finally:
        st.restore()
    assert st.fetched == 1, "downloaded {} times".format(st.fetched)


# -----------------------------------------------------------------------------
#  Order-size buckets
# -----------------------------------------------------------------------------

def test_the_buckets_cover_the_boundaries_exactly():
    assert api._order_bucket(0.5) == "SMALL"
    assert api._order_bucket(49.99) == "SMALL"
    assert api._order_bucket(50.0) == "MID"          # 50 belongs to 50-100
    assert api._order_bucket(99.99) == "MID"
    assert api._order_bucket(100.0) == "LARGE"       # 100 belongs to 100+
    assert api._order_bucket(250000.0) == "LARGE"


def test_no_order_means_no_bucket():
    assert api._order_bucket(None) is None


def test_every_bucket_is_reachable_and_they_do_not_overlap():
    seen = set()
    for v in (1, 25, 49, 50, 75, 99, 100, 500, 5000):
        b = api._order_bucket(v)
        assert b is not None, v
        seen.add(b)
    assert seen == {"SMALL", "MID", "LARGE"}


def test_the_dashboard_reads_the_boundaries_from_the_api():
    """
    Hard-coding them in the browser is how "50-100 Cr" ends up meaning two
    different things. /api/config publishes them.
    """
    keys = [b["key"] for b in api.ORDER_BUCKETS]
    assert keys == ["SMALL", "MID", "LARGE"]
    for b in api.ORDER_BUCKETS:
        assert b["label"] and "min" in b and "max" in b


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

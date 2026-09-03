"""
test_outcomes.py — the gap/capturable split, and the momentum before an alert.

The measurement this file protects came out of a real question: TEJASNET,
ASHOKA and WELCORP moved 7-15% after an alert while TEXRAIL, with a LARGER
order relative to its size, did not. Splitting each move into its overnight gap
and its intraday part answered it — and dissolved it:

    TEJASNET  filed 18:20, market shut   gap +5.25   capturable +2.26
    TEXRAIL   filed 13:11, market OPEN   gap -0.60   capturable +4.40

The apparent winner offered half the reachable move of the apparent failure.
Keeping the two numbers apart is the entire point, so they are pinned here
against the real sessions.

No network, no database. Run: pytest test_outcomes.py  or  python test_outcomes.py
"""
import datetime

import db
import outcomes

IST = outcomes.IST


# -----------------------------------------------------------------------------
#  The split, against real Bhavcopy figures
# -----------------------------------------------------------------------------

def test_tejasnet_was_mostly_gap():
    """511.15 -> open 538.00 -> close 550.15 on 28 Aug 2026."""
    gap = outcomes._pct(511.15, 538.00)
    capturable = outcomes._pct(538.00, 550.15)
    total = outcomes._pct(511.15, 550.15)
    assert abs(gap - 5.25) < 0.01
    assert abs(capturable - 2.26) < 0.01
    assert abs(total - 7.63) < 0.01
    assert gap > capturable * 2, "the headline move was mostly unreachable"


def test_texrail_was_almost_all_capturable():
    """108.95 -> open 108.30 -> close 113.07 on 1 Sep 2026."""
    gap = outcomes._pct(108.95, 108.30)
    capturable = outcomes._pct(108.30, 113.07)
    assert gap < 0, "it opened DOWN"
    assert abs(capturable - 4.40) < 0.01
    # The point of the whole exercise.
    assert capturable > outcomes._pct(538.00, 550.15), (
        "TEXRAIL offered more reachable move than TEJASNET")


def test_the_three_add_up():
    prev, opn, cls = 100.0, 105.0, 110.0
    gap = outcomes._pct(prev, opn)
    cap = outcomes._pct(opn, cls)
    total = outcomes._pct(prev, cls)
    # _pct rounds to two decimals, so they compound to within rounding rather
    # than exactly. Asserting exact equality here failed on that alone.
    assert abs((1 + gap / 100) * (1 + cap / 100) - (1 + total / 100)) < 1e-3


def test_bad_inputs_do_not_produce_a_number():
    for a, b in ((None, 100), (100, None), (0, 100), (-5, 100), ("x", 1)):
        assert outcomes._pct(a, b) is None, (a, b)


# -----------------------------------------------------------------------------
#  Which session the reaction lands in
# -----------------------------------------------------------------------------

class Sessions:
    """Stands in for daily_volume."""

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def first_on_or_after(self, symbol, date):
        self.asked.append(date)
        for r in self.rows:
            if r["session_date"] >= date:
                return r
        return None


def _with_sessions(rows):
    s = Sessions(rows)
    saved = db.first_session_on_or_after
    db.first_session_on_or_after = s.first_on_or_after
    return s, saved


def test_an_after_hours_filing_is_measured_on_the_NEXT_session():
    """
    Filed at 18:20, so the market never saw it that day. Measuring the same
    day's bar would score the alert on a session that closed before it existed.
    """
    rows = [{"session_date": datetime.date(2026, 8, 28), "open": 538.0,
             "close": 550.15, "prev_close": 511.15}]
    s, saved = _with_sessions(rows)
    try:
        sess, gap, cap, total = outcomes.outcome(
            "TEJASNET", datetime.datetime(2026, 8, 27, 18, 20))
    finally:
        db.first_session_on_or_after = saved
    assert s.asked[0] == datetime.date(2026, 8, 28), "looked at the wrong day"
    assert sess == datetime.date(2026, 8, 28)
    assert abs(gap - 5.25) < 0.01 and abs(cap - 2.26) < 0.01


def test_a_filing_during_the_session_is_measured_on_that_session():
    rows = [{"session_date": datetime.date(2026, 9, 1), "open": 108.30,
             "close": 113.07, "prev_close": 108.95}]
    s, saved = _with_sessions(rows)
    try:
        sess, gap, cap, _ = outcomes.outcome(
            "TEXRAIL", datetime.datetime(2026, 9, 1, 13, 11))
    finally:
        db.first_session_on_or_after = saved
    assert s.asked[0] == datetime.date(2026, 9, 1)
    assert abs(cap - 4.40) < 0.01


def test_a_weekend_filing_lands_on_the_next_trading_day():
    """
    ASHOKA filed Saturday 29 Aug; the next session was Monday 31 Aug. The
    lookup asks for "on or after", so a missing Sunday costs nothing.
    """
    rows = [{"session_date": datetime.date(2026, 8, 31), "open": 117.70,
             "close": 120.29, "prev_close": 112.65}]
    s, saved = _with_sessions(rows)
    try:
        sess, gap, cap, total = outcomes.outcome(
            "ASHOKA", datetime.datetime(2026, 8, 29, 10, 5))
    finally:
        db.first_session_on_or_after = saved
    assert sess == datetime.date(2026, 8, 31)
    assert abs(gap - 4.48) < 0.01
    assert abs(total - 6.78) < 0.01


def test_no_session_yet_means_no_answer():
    s, saved = _with_sessions([])
    try:
        assert outcomes.outcome("X", datetime.datetime(2026, 9, 3, 10, 0)) == (
            None, None, None, None)
    finally:
        db.first_session_on_or_after = saved


def test_a_missing_timestamp_is_survivable():
    assert outcomes.outcome("X", None) == (None, None, None, None)


# -----------------------------------------------------------------------------
#  Momentum before the alert
# -----------------------------------------------------------------------------

def _with_history(closes):
    rows = [{"session_date": datetime.date(2026, 8, 1) + datetime.timedelta(days=i),
             "close": c} for i, c in enumerate(closes)]
    saved = db.sessions_for
    db.sessions_for = lambda sym, before, limit=21: rows
    return saved


def test_a_falling_stock_reads_negative():
    """
    INOXWIND is why this exists: 74.00 down to 69.50 over three weeks, and the
    only one of five names that had been falling into its alert.
    """
    saved = _with_history([74.0, 74.1, 73.7, 78.1, 75.0, 73.7, 73.9, 73.6,
                           73.8, 73.7, 73.2, 72.0, 71.8, 70.4, 69.5])
    try:
        m20, m5, pos = outcomes.momentum("INOXWIND", datetime.date(2026, 9, 3))
    finally:
        db.sessions_for = saved
    assert m20 is not None and m20 < 0, m20
    assert m5 < 0
    assert pos is not None and pos < 20, "it closed near the bottom of its range"


def test_a_rising_stock_reads_positive_and_near_its_high():
    saved = _with_history([100 + i for i in range(16)])
    try:
        m20, m5, pos = outcomes.momentum("X", datetime.date(2026, 9, 3))
    finally:
        db.sessions_for = saved
    assert m20 > 0 and m5 > 0
    assert pos == 100.0, "a new high should read as the top of the range"


def test_too_little_history_is_silent_rather_than_wrong():
    saved = _with_history([100, 101, 102])
    try:
        assert outcomes.momentum("X", datetime.date(2026, 9, 3)) == (None, None, None)
    finally:
        db.sessions_for = saved


def test_a_flat_stock_does_not_divide_by_zero():
    saved = _with_history([100.0] * 16)
    try:
        m20, m5, pos = outcomes.momentum("X", datetime.date(2026, 9, 3))
    finally:
        db.sessions_for = saved
    assert m20 == 0.0 and m5 == 0.0
    assert pos is None, "a zero-width range has no position in it"


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

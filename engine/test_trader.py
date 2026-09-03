"""
test_trader.py — the trading gate, and the guarantee that it cannot trade.

This module decides what WOULD be bought. The most important test in the file
is the one asserting there is no code path that can place an order, because
that is the claim the whole shadow stage rests on: not a flag someone could
flip by accident, but no executor to flip it into.

No network, no database.

Run: pytest test_trader.py   or   python test_trader.py
"""
import datetime

import config
import db
import trader


def alert(**kw):
    base = dict(announcement_id=1, company_symbol="TEST", company_name="Test Ltd",
                conviction="STRONG", order_value_cr=200.0, market_cap_cr=1000.0,
                headline="Order win Rs 200 Cr")
    base.update(kw)
    return base


# -----------------------------------------------------------------------------
#  It cannot place an order
# -----------------------------------------------------------------------------

def test_there_is_no_code_that_can_place_an_order():
    """
    The load-bearing test of this stage. Shadow mode is only meaningful if the
    executor does not exist yet — a disabled executor is one edit away from an
    enabled one, and this system was producing about two misclassifications a
    day when it was written.
    """
    src = open(trader.__file__, encoding="utf-8").read().lower()
    for forbidden in ("kiteconnect", "place_order", "kite.trade",
                      "transaction_type", "order_type", "api_secret"):
        assert forbidden not in src, (
            "trader.py contains {!r} - it can no longer be called shadow mode"
            .format(forbidden))


def test_nothing_in_the_engine_imports_a_broker_sdk():
    """
    No broker is connected at all: the session layer that existed briefly was
    removed. This asserts the engine stays that way rather than drifting back.
    """
    import glob
    import os
    here = os.path.dirname(os.path.abspath(trader.__file__))
    for path in glob.glob(os.path.join(here, "*.py")):
        # The test files name the SDK in order to forbid it, which is not the
        # same as importing it — scanning them finds this file's own word list.
        if os.path.basename(path).startswith("test_"):
            continue
        text = open(path, encoding="utf-8").read().lower()
        assert "import kiteconnect" not in text, path
        assert "from kiteconnect" not in text, path


# -----------------------------------------------------------------------------
#  The gate
# -----------------------------------------------------------------------------

def test_a_transformative_order_is_taken():
    """CCME: Rs 133 Cr against a Rs 171 Cr company. That is the whole thesis."""
    would, why, d = trader.evaluate(
        alert(company_symbol="CCME", order_value_cr=133.06, market_cap_cr=171.05))
    assert would, why
    assert d["order_to_mcap_pct"] > 77


def test_a_big_order_against_a_big_company_is_rejected():
    """
    NHPC: Rs 392 Cr is a large order and 0.52% of the company. Without the
    materiality test this passes on size alone, which is the mistake the third
    condition exists to prevent.
    """
    would, why, _ = trader.evaluate(
        alert(company_symbol="NHPC", order_value_cr=392.31, market_cap_cr=76040.91))
    assert not would
    assert "0.52% of market cap" in why


def test_only_strong_conviction_qualifies():
    for c in ("MODERATE", "WATCH", None, ""):
        would, why, _ = trader.evaluate(alert(conviction=c))
        assert not would, c


def test_a_small_order_is_rejected_however_material():
    would, _, _ = trader.evaluate(alert(order_value_cr=50.0, market_cap_cr=100.0))
    assert not would, "50% of the company, but only Rs 50 Cr"


def test_an_unknown_market_cap_is_refused_and_says_so():
    """
    Six of 23 otherwise-qualifying alerts in a month had no market cap. Skipping
    them silently is how a rule stops applying without anyone noticing — and
    here it would mean either a missed trade or, if it defaulted the other way,
    a trade with no idea of the company's size.
    """
    would, why, _ = trader.evaluate(alert(market_cap_cr=None))
    assert not would
    assert "market cap unknown" in why


def test_a_missing_order_value_is_refused():
    for v in (None, 0):
        would, _, _ = trader.evaluate(alert(order_value_cr=v))
        assert not would, v


def test_the_boundary_is_inclusive_on_both_conditions():
    exactly = alert(order_value_cr=config.TRADE_MIN_ORDER_CR,
                    market_cap_cr=config.TRADE_MIN_ORDER_CR
                    / (config.TRADE_MIN_ORDER_TO_MCAP_PCT / 100.0))
    would, why, d = trader.evaluate(exactly)
    assert would, (why, d)


def test_the_detail_records_what_the_decision_turned_on():
    """A recorded trade has to be arguable with later, not taken on trust."""
    _, _, d = trader.evaluate(alert())
    assert d["conviction"] == "STRONG"
    assert d["order_cr"] == 200.0
    assert d["market_cap_cr"] == 1000.0
    assert d["order_to_mcap_pct"] == 20.0


# -----------------------------------------------------------------------------
#  When it could actually trade
# -----------------------------------------------------------------------------

def test_an_alert_after_the_close_is_marked_as_such():
    """
    56% of alerts in a week arrived after 15:30. Those cannot be traded that
    day, and recording them as if they could would flatter every later result.
    """
    evening = datetime.datetime(2026, 9, 2, 17, 20, tzinfo=trader.IST)
    state, note = trader.session_state(evening)
    assert state == "AFTER_CLOSE"
    assert "next open" in note


def test_an_alert_during_the_session_is_tradeable_now():
    midday = datetime.datetime(2026, 9, 2, 12, 30, tzinfo=trader.IST)
    assert trader.session_state(midday)[0] == "OPEN"


def test_the_open_and_close_boundaries():
    for hhmm, expect in (((9, 14), "PRE_OPEN"), ((9, 15), "OPEN"),
                         ((15, 29), "OPEN"), ((15, 30), "AFTER_CLOSE")):
        t = datetime.datetime(2026, 9, 2, hhmm[0], hhmm[1], tzinfo=trader.IST)
        assert trader.session_state(t)[0] == expect, (hhmm, expect)


def test_a_weekend_alert_queues_to_monday():
    sat = datetime.datetime(2026, 9, 5, 11, 0, tzinfo=trader.IST)
    state, note = trader.session_state(sat)
    assert state == "WEEKEND"
    assert "Monday" in note


# -----------------------------------------------------------------------------
#  Not buying the same thing twice
# -----------------------------------------------------------------------------

def test_the_same_symbol_is_not_taken_twice():
    """
    SUGSLLOYD announced the same Rs 214.27 Cr order on two consecutive days and
    alerted both times. A trader acting on both buys the position twice.
    """
    saved = (db.fetch_untraded_alerts, db.traded_recently, db.count_trades_today,
             db.save_paper_trade)
    saved_price = trader.reference_price
    recorded = []
    try:
        db.fetch_untraded_alerts = lambda days, limit: [
            alert(announcement_id=1, company_symbol="SUGSLLOYD",
                  order_value_cr=214.27, market_cap_cr=498.87)]
        db.traded_recently = lambda sym, days: True      # already held
        db.count_trades_today = lambda: 0
        db.save_paper_trade = lambda row: recorded.append(row)
        trader.reference_price = lambda sym, name=None: 100.0

        trader.run_once(verbose=False)
    finally:
        (db.fetch_untraded_alerts, db.traded_recently, db.count_trades_today,
         db.save_paper_trade) = saved
        trader.reference_price = saved_price

    assert len(recorded) == 1
    assert recorded[0]["would_trade"] is False
    assert "already taken" in recorded[0]["reason"]


def test_the_daily_cap_stops_a_runaway():
    """
    The gate admits about two a week. Hitting a daily cap means something
    upstream broke, not that the market got busy — so the cap is the brake.
    """
    saved = (db.fetch_untraded_alerts, db.traded_recently, db.count_trades_today,
             db.save_paper_trade)
    saved_price = trader.reference_price
    recorded = []
    try:
        db.fetch_untraded_alerts = lambda days, limit: [alert()]
        db.traded_recently = lambda sym, days: False
        db.count_trades_today = lambda: config.TRADE_MAX_PER_DAY
        db.save_paper_trade = lambda row: recorded.append(row)
        trader.reference_price = lambda sym, name=None: 100.0
        trader.run_once(verbose=False)
    finally:
        (db.fetch_untraded_alerts, db.traded_recently, db.count_trades_today,
         db.save_paper_trade) = saved
        trader.reference_price = saved_price

    assert recorded[0]["would_trade"] is False
    assert "daily cap" in recorded[0]["reason"]


def test_a_taken_trade_records_a_size():
    saved = (db.fetch_untraded_alerts, db.traded_recently, db.count_trades_today,
             db.save_paper_trade)
    saved_price = trader.reference_price
    recorded = []
    try:
        db.fetch_untraded_alerts = lambda days, limit: [
            alert(company_symbol="CCME", order_value_cr=133.06, market_cap_cr=171.05)]
        db.traded_recently = lambda sym, days: False
        db.count_trades_today = lambda: 0
        db.save_paper_trade = lambda row: recorded.append(row)
        trader.reference_price = lambda sym, name=None: 250.0
        trader.run_once(verbose=False)
    finally:
        (db.fetch_untraded_alerts, db.traded_recently, db.count_trades_today,
         db.save_paper_trade) = saved
        trader.reference_price = saved_price

    r = recorded[0]
    assert r["would_trade"] is True
    assert r["quantity"] == int(config.TRADE_VALUE_INR // 250.0)
    assert abs(r["intended_value_inr"] - r["quantity"] * 250.0) < 0.01


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

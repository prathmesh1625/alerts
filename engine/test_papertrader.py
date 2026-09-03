"""
test_papertrader.py — buying on an alert, with virtual money.

This is the first code in the repo that can place an order, so the guarantee
that made the shadow stage meaningful has to be replaced with a narrower one
rather than dropped: it can only ever reach MegaBull's simulator. The first
test in this file is that, and it matters more than the rest.

No network, no database. Run: pytest test_papertrader.py
"""
import config
import db
import megabull
import papertrader


# -----------------------------------------------------------------------------
#  It can only reach the simulator
# -----------------------------------------------------------------------------

def test_the_endpoint_is_a_constant_not_a_setting():
    """
    The whole safety argument. A configurable base URL is one environment
    variable away from a live broker; a constant is not.
    """
    assert megabull.BASE == "https://api.megabull.in"
    src = open(megabull.__file__, encoding="utf-8").read()
    assert "BASE = \"https://api.megabull.in\"" in src
    assert "getenv(\"MEGABULL_BASE" not in src
    assert "MEGABULL_BASE_URL" not in src
    assert not hasattr(config, "MEGABULL_BASE"), (
        "a base-URL setting exists - the endpoint is no longer fixed")


def test_every_request_goes_to_that_constant():
    """No call builds a URL from anything but BASE."""
    import re
    src = open(megabull.__file__, encoding="utf-8").read()
    # Every urlopen must be on BASE + path, or the instrument CSV URL that
    # MegaBull itself returns.
    for m in re.finditer(r"urlopen\(([^,\)]+)", src):
        arg = m.group(1).strip()
        assert arg in ("req", "url"), arg
    # Whitespace-tolerant: the call wraps across lines.
    assert re.search(r"Request\(\s*BASE \+ path", src), (
        "a request is built from something other than BASE")


def test_no_real_broker_appears_anywhere():
    import glob
    import os
    here = os.path.dirname(os.path.abspath(megabull.__file__))
    for path in glob.glob(os.path.join(here, "*.py")):
        if os.path.basename(path).startswith("test_"):
            continue
        text = open(path, encoding="utf-8").read().lower()
        for broker in ("api.kite.trade", "kiteconnect", "api.upstox",
                       "api.dhan.co", "smartapi.angel"):
            assert broker not in text, (path, broker)


# -----------------------------------------------------------------------------
#  Position sizing
# -----------------------------------------------------------------------------

def test_size_is_the_budget_divided_by_the_price():
    qty = papertrader.quantity_for(500.0, 500000)
    assert qty == int(config.PAPER_TRADE_VALUE_INR // 500.0)


def test_size_is_capped_by_what_is_actually_left():
    """
    MegaBull rejects an order beyond the virtual money, so the balance is read
    rather than assumed - an account nearly deployed must shrink the position,
    not send an order that bounces.
    """
    assert papertrader.quantity_for(550.15, 3000) == 5
    assert papertrader.quantity_for(550.15, 100) == 0


def test_an_unusable_price_buys_nothing():
    for price in (0, None, -5, "x"):
        assert papertrader.quantity_for(price, 500000) == 0, price


def test_size_never_exceeds_the_api_ceiling():
    """MegaBull refuses more than 10,000 units."""
    assert papertrader.quantity_for(0.5, 10_000_000) <= 10000


# -----------------------------------------------------------------------------
#  What the order looks like
# -----------------------------------------------------------------------------

class Sent:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"orderId": "MB-1"}


def _with_request():
    s = Sent()
    saved = megabull._request
    megabull._request = s
    return s, saved


def test_a_buy_is_delivery_and_market_by_default():
    """
    CNC rather than MIS: an order-win thesis plays out over days, and an
    intraday product would be squared off at the close regardless of it.
    """
    s, saved = _with_request()
    try:
        megabull.place_buy("5409537", 45)
    finally:
        megabull._request = saved
    method, path, body = s.calls[0]
    assert (method, path) == ("POST", "/api/order/buysell")
    assert body["type"] == "BUY"
    assert body["duration"] == "CNC"
    assert body["orderType"] == "MKT"
    assert body["instrumentToken"] == "5409537" and body["qty"] == 45
    assert "price" not in body, "a market order must not carry a price"


def test_a_limit_order_requires_a_price():
    s, saved = _with_request()
    try:
        try:
            megabull.place_buy("1", 10, order_type="LIMIT")
            assert False, "should have raised"
        except megabull.MegaBullError as e:
            assert "price" in str(e)
        megabull.place_buy("1", 10, order_type="LIMIT", price=250.0)
        assert s.calls[-1][2]["price"] == 250.0
    finally:
        megabull._request = saved


def test_nonsense_orders_are_refused_before_they_are_sent():
    s, saved = _with_request()
    try:
        for kwargs in ({"instrument_token": "", "qty": 5},
                       {"instrument_token": "1", "qty": 0},
                       {"instrument_token": "1", "qty": -3},
                       {"instrument_token": "1", "qty": 20000},
                       {"instrument_token": "1", "qty": 5, "duration": "XYZ"},
                       {"instrument_token": "1", "qty": 5, "order_type": "XYZ"}):
            try:
                megabull.place_buy(**kwargs)
                assert False, kwargs
            except megabull.MegaBullError:
                pass
        assert s.calls == [], "a bad order reached the API"
    finally:
        megabull._request = saved


# -----------------------------------------------------------------------------
#  The brakes
# -----------------------------------------------------------------------------

def test_it_is_off_by_default():
    assert config.PAPER_TRADING_ENABLED is False


def test_it_refuses_to_start_without_a_key():
    saved_enabled = config.PAPER_TRADING_ENABLED
    saved_key = config.MEGABULL_API_KEY
    try:
        config.PAPER_TRADING_ENABLED = True
        config.MEGABULL_API_KEY = ""
        assert papertrader._preflight() is False
    finally:
        config.PAPER_TRADING_ENABLED = saved_enabled
        config.MEGABULL_API_KEY = saved_key


def test_an_expired_key_says_so_rather_than_reporting_a_bare_401():
    """
    MegaBull keys last one month, so this WILL happen on a date nobody is
    watching for. The message has to carry the instruction.
    """
    import urllib.error
    saved = megabull.urllib.request.urlopen
    saved_key = config.MEGABULL_API_KEY
    try:
        config.MEGABULL_API_KEY = "k"

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        megabull.urllib.request.urlopen = boom
        try:
            megabull.account()
            assert False, "should have raised"
        except megabull.MegaBullError as e:
            assert "one month" in str(e) and "regenerate" in str(e).lower()
    finally:
        megabull.urllib.request.urlopen = saved
        config.MEGABULL_API_KEY = saved_key


def test_the_key_is_never_in_an_error_message():
    import urllib.error
    saved = megabull.urllib.request.urlopen
    saved_key = config.MEGABULL_API_KEY
    try:
        config.MEGABULL_API_KEY = "SECRET-KEY-VALUE"
        megabull.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 500, "boom", {}, None))
        try:
            megabull.account()
        except megabull.MegaBullError as e:
            assert "SECRET-KEY-VALUE" not in str(e)
    finally:
        megabull.urllib.request.urlopen = saved
        config.MEGABULL_API_KEY = saved_key


def test_status_carries_no_key():
    saved_key = config.MEGABULL_API_KEY
    saved_acct = megabull.account
    try:
        config.MEGABULL_API_KEY = "SECRET-KEY-VALUE"
        megabull.account = lambda: {"virtual_money": 500000, "available": 500000,
                                    "blocked": 0}
        assert "SECRET-KEY-VALUE" not in repr(megabull.status())
    finally:
        config.MEGABULL_API_KEY = saved_key
        megabull.account = saved_acct


def test_the_lookback_is_hours_not_days():
    """
    Switching this on against a populated database must not replay a month of
    alerts into the account in one pass.
    """
    assert config.PAPER_LOOKBACK_HOURS <= 24


def test_the_daily_cap_is_small_because_the_gate_is_narrow():
    assert 0 < config.PAPER_MAX_PER_DAY <= 5


def test_it_uses_the_same_gate_as_the_shadow_record():
    """
    One gate, so the paper trades and the shadow record measure the same thing.
    A second copy of the rules would drift from the first.
    """
    import inspect
    import re
    src = inspect.getsource(papertrader)
    assert "trader.evaluate(" in src

    # The thresholds may be PRINTED - _preflight logs the gate it is running
    # under - but never compared against here, which would be a second copy of
    # the rules free to drift from the first.
    for line in src.splitlines():
        if "TRADE_MIN_ORDER" in line and re.search(r"[<>]=?|==", line):
            raise AssertionError(
                "the gate is being re-implemented: {}".format(line.strip()))


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

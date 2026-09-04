"""
test_papertrader.py — buying on an alert, with virtual money.

This is the first code in the repo that can place an order, so the guarantee
that made the shadow stage meaningful has to be replaced with a narrower one
rather than dropped: it can only ever reach MegaBull's simulator. The first
test in this file is that, and it matters more than the rest.

No network, no database. Run: pytest test_papertrader.py
"""
import json

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
        megabull.place_buy("5409537", 45, price=210.5)
    finally:
        megabull._request = saved
    method, path, body = s.calls[0]
    assert (method, path) == ("POST", "/api/order/buysell")
    assert body["type"] == "BUY"
    assert body["duration"] == "CNC"
    assert body["orderType"] == "MKT"
    assert body["instrumentToken"] == "5409537" and body["qty"] == 45
    # A price on a MARKET order looks wrong and is required: MegaBull answers
    # HTTP 400 "Price cannot be Blank" without one. It does not set the fill -
    # the simulator uses its own live quote.
    assert body["price"] == 210.5, "MegaBull rejects an order with no price"


def test_every_order_type_requires_a_price():
    """Not just LIMIT - MegaBull rejects a MKT order without one too."""
    s, saved = _with_request()
    try:
        for kind in ("MKT", "LIMIT", "SL"):
            try:
                megabull.place_buy("1", 10, order_type=kind)
                assert False, "{} was sent with no price".format(kind)
            except megabull.MegaBullError as e:
                assert "price" in str(e).lower(), (kind, str(e))
        megabull.place_buy("1", 10, order_type="LIMIT", price=250.0)
        assert s.calls[-1][2]["price"] == 250.0
    finally:
        megabull._request = saved


def test_a_list_of_validation_errors_is_rendered_not_concatenated():
    """
    The crash. MegaBull returns {"message": ["Price cannot be Blank"]}, and
    formatting a list with `"; " + value` raises a TypeError from inside the
    exception handler - which is not a MegaBullError, so it escaped every
    caller and killed the pass instead of being recorded as one failed order.
    """
    import urllib.error
    saved = megabull.urllib.request.urlopen
    saved_key = config.MEGABULL_API_KEY
    try:
        config.MEGABULL_API_KEY = "k"

        class _Body:
            def read(self):
                return json.dumps({
                    "message": ["Price cannot be Blank", "qty must be > 0"],
                    "error": "MethodArgumentNotValidException",
                }).encode()

        def boom(*a, **k):
            err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
            err.read = _Body().read
            raise err

        megabull.urllib.request.urlopen = boom
        try:
            megabull.account()
            assert False, "should have raised"
        except megabull.MegaBullError as e:
            assert "Price cannot be Blank" in str(e), str(e)
            assert "qty must be > 0" in str(e), str(e)
        except TypeError:
            raise AssertionError(
                "a list message still raises TypeError out of the handler")
    finally:
        megabull.urllib.request.urlopen = saved
        config.MEGABULL_API_KEY = saved_key


def test_readable_handles_every_shape_an_api_might_send():
    assert megabull._readable("plain") == "plain"
    assert megabull._readable(["a", "b"]) == "a; b"
    assert megabull._readable(None) == ""
    assert "x" in megabull._readable({"field": "x"})
    assert megabull._readable(404) == "404"


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


# -----------------------------------------------------------------------------
#  The market has to be open
# -----------------------------------------------------------------------------

def _no_db(*a, **k):
    raise AssertionError("the database was read while the market was shut")


def test_nothing_is_attempted_while_the_market_is_shut():
    """
    MegaBull rejects an order outside 09:15-15:30, and a recorded rejection is
    keyed on announcement_id - so recording one would mark the alert handled
    and it would never be retried at the open. Since 56% of alerts arrive
    after 15:30, that would quietly lose most of them.
    """
    import trader
    saved_state = trader.session_state
    saved_fetch = db.fetch_unpapered_alerts
    saved_save = db.save_paper_order
    try:
        db.fetch_unpapered_alerts = _no_db
        db.save_paper_order = _no_db
        for state in ("WEEKEND", "PRE_OPEN", "AFTER_CLOSE"):
            papertrader._last_session = None
            trader.session_state = lambda s=state: (s, "shut: {}".format(s))
            assert papertrader.run_once(verbose=False) == 0, state
    finally:
        trader.session_state = saved_state
        db.fetch_unpapered_alerts = saved_fetch
        db.save_paper_order = saved_save
        papertrader._last_session = None


def test_an_open_market_does_reach_the_database():
    """The converse, so the guard above cannot pass by blocking everything."""
    import trader
    saved_state = trader.session_state
    saved_fetch = db.fetch_unpapered_alerts
    seen = []
    try:
        papertrader._last_session = None
        trader.session_state = lambda: ("OPEN", "open")
        db.fetch_unpapered_alerts = lambda h, n: seen.append((h, n)) or []
        papertrader.run_once(verbose=False)
        assert seen, "the market was open and no alerts were read"
    finally:
        trader.session_state = saved_state
        db.fetch_unpapered_alerts = saved_fetch
        papertrader._last_session = None


def test_the_lookback_outlives_a_night():
    """
    An alert at 20:00 cannot be acted on until 09:15 the next morning - 13
    hours later. A lookback shorter than that discards the majority of alerts
    before the market ever opens.
    """
    assert config.PAPER_LOOKBACK_HOURS >= 14, (
        "lookback of {}h expires overnight alerts before the open".format(
            config.PAPER_LOOKBACK_HOURS))


class _Parked(Exception):
    """Raised by a stubbed sleep to prove the process parked rather than exited."""


def test_being_disabled_parks_the_process_instead_of_exiting_it():
    """
    The one that took the site down.

    This service is OFF by default, so "disabled" is the path that actually
    runs in production. It used to return from main(), and a container that
    exits cleanly under `restart: always` is restarted immediately, exits
    again, and keeps going - a restart storm against the Docker daemon that
    every other container on the box shares.

    Exiting is only correct for --once. The daemon must idle.
    """
    import sys
    saved_argv = sys.argv
    saved_sleep = papertrader.time.sleep
    saved_enabled = config.PAPER_TRADING_ENABLED
    slept = []
    try:
        config.PAPER_TRADING_ENABLED = False
        sys.argv = ["papertrader.py"]

        def parked(sec):
            slept.append(sec)
            raise _Parked()

        papertrader.time.sleep = parked
        try:
            papertrader.main()
            raise AssertionError(
                "main() returned with trading disabled - the container will "
                "exit and be restarted forever")
        except _Parked:
            pass
        assert slept and slept[0] >= 60, (
            "parked, but on a short sleep: {}".format(slept))
    finally:
        sys.argv = saved_argv
        papertrader.time.sleep = saved_sleep
        config.PAPER_TRADING_ENABLED = saved_enabled


def test_once_still_exits_when_disabled():
    """--once is a command, not a daemon: it must return, not hang."""
    import sys
    saved_argv = sys.argv
    saved_enabled = config.PAPER_TRADING_ENABLED
    saved_sleep = papertrader.time.sleep
    try:
        config.PAPER_TRADING_ENABLED = False
        sys.argv = ["papertrader.py", "--once"]
        papertrader.time.sleep = lambda s: (_ for _ in ()).throw(
            AssertionError("--once slept instead of returning"))
        papertrader.main()
    finally:
        sys.argv = saved_argv
        config.PAPER_TRADING_ENABLED = saved_enabled
        papertrader.time.sleep = saved_sleep


def test_the_compose_service_cannot_restart_on_a_clean_exit():
    """
    Belt and braces for the above: even if something exits cleanly again,
    `restart: always` would loop it. The policy must be bounded.
    """
    import os
    import yaml
    here = os.path.dirname(os.path.abspath(papertrader.__file__))
    path = os.path.join(os.path.dirname(here), "docker-compose.yml")
    if not os.path.exists(path):
        return
    svc = yaml.safe_load(open(path, encoding="utf-8"))["services"]["alert-paper"]
    assert str(svc.get("restart", "")).startswith("on-failure"), (
        "alert-paper restart policy is {!r}; `always` restarts a clean "
        "exit forever".format(svc.get("restart")))


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

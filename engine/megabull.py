"""
megabull.py — MegaBull paper-trading API. Virtual money only.

This is the first module in the repo that can place an order, and the guarantee
that made the earlier shadow stage meaningful has to be replaced with a
narrower one rather than dropped: BASE is a hardcoded constant, not a setting.
There is no environment variable, no argument and no code path that can point
this at a real broker. A test asserts it.

What that buys: every failure mode this engine has produced — a deposit
programme read as a Rs 35,000 Cr order, a terminated contract read as a win, a
USD contract undervalued 80x, the same order alerting three times — costs
virtual money here and nothing else.

The API, from https://megabull.in/paper-trading-api-india.html and the spec at
/api-docs:

    POST /api/order/buysell   {instrumentToken, qty, type, duration, orderType}
        type      BUY | SELL
        duration  MIS (intraday) | CNC (delivery)
        orderType LIMIT (needs price) | MKT | SL (needs triggerPrice)
    GET  /api/user/my              virtual money and what is blocked
    GET  /api/position/my          open positions
    GET  /api/order/my             orders placed
    GET  /api/marketwatch/instruments   -> a CSV download URL

Two constraints worth stating because they shape the caller:

  * the api-key expires after ONE MONTH, so this will start failing on a date
    nobody is watching for. The error is surfaced plainly rather than retried.
  * accounts start with Rs 5,00,000 of virtual money and the API rejects an
    order that exceeds it. Sizing therefore reads the balance rather than
    assuming it.
"""
import csv
import io
import json
import threading
import time
import urllib.error
import urllib.request

import config

# Hardcoded, and deliberately not configurable. This is the paper endpoint;
# there is no live one reachable from this code.
BASE = "https://api.megabull.in"
INSTRUMENTS = "/api/marketwatch/instruments"

_TIMEOUT = 25
_instruments = None
_instruments_at = 0.0
_lock = threading.Lock()


class MegaBullError(Exception):
    """A MegaBull call that did not succeed."""


def _log(msg):
    """NEVER pass the api key through this."""
    print("[megabull] {}".format(msg), flush=True)


def configured() -> bool:
    return bool(config.MEGABULL_API_KEY)


def _request(method, path, body=None):
    if not configured():
        raise MegaBullError("MEGABULL_API_KEY is not set")

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"api-key": config.MEGABULL_API_KEY,
                 "Accept": "application/json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = _readable(
                (json.loads(e.read().decode("utf-8", "replace")) or {}).get(
                    "message", ""))
        except Exception:
            pass
        if e.code in (401, 403):
            # The key lasts a month. Saying so turns a puzzling 401 into an
            # instruction.
            raise MegaBullError(
                "MegaBull rejected the key ({}). Keys expire after one month - "
                "regenerate at trade.megabull.in and update "
                "ALERT_MEGABULL_API_KEY.".format(e.code))
        raise MegaBullError("HTTP {}{}".format(e.code, ": " + detail if detail else ""))
    except Exception as e:
        raise MegaBullError("could not reach MegaBull: {}".format(e))

    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise MegaBullError("MegaBull returned a non-JSON response")


def _readable(value):
    """
    An error detail as a string, whatever shape it arrived in.

    MegaBull returns validation errors as a LIST - {"message": ["Price cannot
    be Blank"]} - and formatting that with `"; " + value` raised a TypeError
    from INSIDE the exception handler. That is not a MegaBullError, so it flew
    past every caller's `except MegaBullError`, killed the whole pass, and the
    failure was never recorded - so it retried, and failed the same way, every
    60 seconds.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return "; ".join(_readable(v) for v in value)
    if isinstance(value, dict):
        try:
            return json.dumps(value)
        except Exception:
            return str(value)
    return str(value)


def _unwrap(payload):
    """Responses are sometimes {"data": ...} and sometimes the value itself."""
    if isinstance(payload, dict) and "data" in payload and len(payload) <= 3:
        return payload["data"]
    return payload


# -----------------------------------------------------------------------------
#  Account
# -----------------------------------------------------------------------------

def account() -> dict:
    """
    Virtual money available. Contains no key and no personal detail worth
    passing on, so only the money fields are returned.
    """
    d = _unwrap(_request("GET", "/api/user/my")) or {}
    return {
        "virtual_money": d.get("virtualMoney"),
        "blocked": d.get("virtualMoneyBlocked"),
        "available": d.get("virtualMoneyLeft"),
    }


def positions():
    return _unwrap(_request("GET", "/api/position/my")) or []


def orders():
    return _unwrap(_request("GET", "/api/order/my")) or []


# -----------------------------------------------------------------------------
#  Symbol -> instrument token
# -----------------------------------------------------------------------------

def instruments(force=False) -> dict:
    """
    {tradingSymbol: instrumentToken}, cached.

    The endpoint returns a URL to a CSV rather than the rows, so this follows
    it. 5,687 instruments keyed on the NSE ticker, which is what the alerts are
    already keyed on - no mapping table needed.
    """
    global _instruments, _instruments_at
    with _lock:
        fresh = time.time() - _instruments_at < config.MEGABULL_INSTRUMENTS_TTL_SEC
        if _instruments is not None and fresh and not force:
            return _instruments

        payload = _unwrap(_request("GET", INSTRUMENTS)) or {}
        url = payload.get("downloadUrl") if isinstance(payload, dict) else None
        if not url:
            raise MegaBullError("no instrument download URL in the response")

        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception as e:
            raise MegaBullError("could not fetch the instrument list: {}".format(e))

        table = {}
        for row in csv.DictReader(io.StringIO(text)):
            sym = (row.get("tradingSymbol") or "").strip().upper()
            tok = (row.get("instrumentToken") or "").strip()
            if sym and tok:
                table[sym] = tok
        if not table:
            raise MegaBullError("the instrument list was empty")

        _instruments, _instruments_at = table, time.time()
        _log("instrument list loaded ({} symbols)".format(len(table)))
        return table


def token_for(symbol):
    """The instrument token for a ticker, or None if MegaBull does not list it."""
    if not symbol:
        return None
    try:
        return instruments().get(symbol.strip().upper())
    except MegaBullError as e:
        _log("instrument lookup failed: {}".format(e))
        return None


# -----------------------------------------------------------------------------
#  Orders
# -----------------------------------------------------------------------------

def place_buy(instrument_token, qty, duration="CNC", order_type="MKT", price=None):
    """
    Place a paper BUY. Virtual money; nothing real is bought.

    CNC by default rather than MIS: the thesis behind an order-win alert plays
    out over days, and an intraday product would be squared off at the close
    regardless of it.
    """
    if not instrument_token:
        raise MegaBullError("no instrument token")
    qty = int(qty or 0)
    if qty <= 0:
        raise MegaBullError("quantity must be greater than 0")
    if qty > 10000:
        # The API's own ceiling; caught here so the reason is legible.
        raise MegaBullError("quantity {} exceeds MegaBull's limit of 10000".format(qty))
    if order_type not in ("MKT", "LIMIT", "SL"):
        raise MegaBullError("orderType must be MKT, LIMIT or SL")
    if duration not in ("CNC", "MIS"):
        raise MegaBullError("duration must be CNC or MIS")

    # MegaBull requires a price on EVERY order type, market ones included -
    # "Price cannot be Blank", HTTP 400. It is not the fill price: the
    # simulator fills a MKT order at its own live quote (1 BRAHMINFRA sent at
    # 164.8 filled at 165.4). It is a required field, so it is always sent.
    if not price or float(price) <= 0:
        raise MegaBullError("a price is required - MegaBull rejects an order without one")

    body = {"instrumentToken": str(instrument_token), "qty": qty,
            "type": "BUY", "duration": duration, "orderType": order_type,
            "price": float(price)}

    return _unwrap(_request("POST", "/api/order/buysell", body))


def status() -> dict:
    """Whether paper trading can run. No key, safe to serve behind the gate."""
    if not configured():
        return {"configured": False, "live": False,
                "reason": "ALERT_MEGABULL_API_KEY is not set"}
    try:
        acct = account()
    except MegaBullError as e:
        return {"configured": True, "live": False, "reason": str(e)}
    return {
        "configured": True,
        "live": True,
        "paper": True,
        "endpoint": BASE,
        "virtual_money": acct.get("virtual_money"),
        "available": acct.get("available"),
        "blocked": acct.get("blocked"),
        "note": "virtual money only - MegaBull is a simulator",
    }

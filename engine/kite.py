"""
kite.py — Zerodha Kite Connect session handling. READ AND AUTH ONLY.

There is deliberately no order placement here. This module can log in, hold a
session, read the profile and read available margin. It cannot buy or sell, and
test_trader.py asserts as much across the whole engine. Execution is a separate
change, to be made once the shadow record in `paper_trades` justifies it.

The login flow, from Kite's docs:

    1. send the user to  https://kite.zerodha.com/connect/login?v=3&api_key=...
    2. Kite redirects back to our registered URL with ?request_token=...
    3. POST that token plus a checksum to /session/token
    4. the access_token that comes back signs every later request

The checksum is SHA-256 of (api_key + request_token + api_secret) with no
separators — a detail worth stating because getting it wrong returns a generic
failure rather than anything that points at the cause.

Two constraints from the docs shape everything here:

  * the access_token expires at 6 AM the next day, by regulation, and a
    master-logout from Kite Web kills it sooner. There is no refresh for
    ordinary accounts. So a session is a daily, human act — anything built on
    top must expect to find no valid token and say so plainly rather than fail
    obscurely.
  * "never expose your api_secret ... do not expose the access_token". The
    secret is read from the environment and never stored, logged or returned;
    the access_token is stored because it must be, and is never returned by
    any endpoint. Nothing here prints either.

Uses `requests` rather than the pykiteconnect SDK: one less dependency, and the
SDK's surface includes order placement, which this stage must not have.
"""
import datetime
import hashlib

import requests

import config
import db

BASE = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Kite wants its version pinned on every call.
_HEADERS = {"X-Kite-Version": "3"}


class KiteError(Exception):
    """A Kite API call that did not succeed."""


def _log(msg):
    """Log helper. NEVER pass a token or the secret through this."""
    print("[kite] {}".format(msg), flush=True)


def configured() -> bool:
    return bool(config.KITE_API_KEY and config.KITE_API_SECRET)


def login_url() -> str:
    """Where a human has to go, once a day, to start a session."""
    return "{}?v=3&api_key={}".format(LOGIN, config.KITE_API_KEY)


def token_expiry(login_time=None):
    """
    When an access_token dies: the next 06:00 IST strictly after login.

    Kite expires tokens at 6 AM the following day as a regulatory requirement,
    so a login at 05:00 is good for an hour and one at 10:00 for twenty.
    """
    now = login_time or datetime.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    six = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now >= six:
        six += datetime.timedelta(days=1)
    return six


def checksum(request_token: str) -> str:
    """SHA-256 of api_key + request_token + api_secret, concatenated."""
    raw = "{}{}{}".format(config.KITE_API_KEY, request_token, config.KITE_API_SECRET)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def exchange(request_token: str) -> dict:
    """
    Trade a request_token for an access_token and store the session.

    Returns a summary WITHOUT the token in it. The request_token is valid for
    only a few minutes and is single-use, so this runs immediately on callback.
    """
    if not configured():
        raise KiteError("KITE_API_KEY / KITE_API_SECRET are not set")
    if not request_token:
        raise KiteError("no request_token in the callback")

    try:
        r = requests.post(
            BASE + "/session/token",
            headers=_HEADERS,
            data={"api_key": config.KITE_API_KEY,
                  "request_token": request_token,
                  "checksum": checksum(request_token)},
            timeout=20,
        )
    except Exception as e:
        raise KiteError("could not reach Kite: {}".format(e))

    try:
        body = r.json() or {}
    except Exception:
        raise KiteError("Kite returned a non-JSON response (HTTP {})".format(
            r.status_code))

    if r.status_code != 200 or body.get("status") != "success":
        # Kite's message, not the token, and not the secret.
        raise KiteError("token exchange failed: {}".format(
            body.get("message") or "HTTP {}".format(r.status_code)))

    data = body.get("data") or {}
    token = data.get("access_token")
    if not token:
        raise KiteError("Kite returned no access_token")

    expires = token_expiry()
    db.save_kite_session({
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "email": data.get("email"),
        "access_token": token,
        "public_token": data.get("public_token"),
        "login_time": data.get("login_time"),
        "expires_at": expires,
    })
    _log("session established for {} until {} IST".format(
        data.get("user_id"), expires.strftime("%Y-%m-%d %H:%M")))

    return {"user_id": data.get("user_id"), "user_name": data.get("user_name"),
            "expires_at": expires.isoformat(),
            "exchanges": data.get("exchanges"), "products": data.get("products")}


def session():
    """The stored session if it is still valid, else None."""
    try:
        row = db.fetch_kite_session()
    except Exception as e:
        _log("cannot read session: {}".format(e))
        return None
    if not row:
        return None
    expires = row.get("expires_at")
    if expires and expires <= datetime.datetime.now(expires.tzinfo or IST):
        return None
    return row


def status() -> dict:
    """
    Whether we can currently talk to Kite. Contains NO token.

    Safe to serve publicly, which matters: the API this is exposed through is
    reachable from the internet.
    """
    if not configured():
        return {"configured": False, "live": False,
                "reason": "KITE_API_KEY / KITE_API_SECRET not set"}
    s = session()
    if not s:
        return {"configured": True, "live": False,
                "reason": "no valid session - a human must log in again",
                "login_url": login_url()}
    return {
        "configured": True,
        "live": True,
        "user_id": s.get("user_id"),
        "expires_at": s.get("expires_at").isoformat() if s.get("expires_at") else None,
        "note": ("the token expires at 06:00 IST and cannot be refreshed; "
                 "a new login is required each day"),
    }


def _auth_header():
    s = session()
    if not s:
        raise KiteError("no valid Kite session - log in again")
    return dict(_HEADERS,
                Authorization="token {}:{}".format(config.KITE_API_KEY,
                                                   s["access_token"]))


def _get(path):
    try:
        r = requests.get(BASE + path, headers=_auth_header(), timeout=20)
    except Exception as e:
        raise KiteError("could not reach Kite: {}".format(e))
    try:
        body = r.json() or {}
    except Exception:
        raise KiteError("Kite returned a non-JSON response")
    if r.status_code != 200 or body.get("status") != "success":
        raise KiteError(body.get("message") or "HTTP {}".format(r.status_code))
    return body.get("data") or {}


def profile() -> dict:
    """The logged-in user's profile. Read-only; returns no tokens."""
    return _get("/user/profile")


def margins(segment: str = "equity") -> dict:
    """
    Available funds. Read-only.

    The number that decides whether an order COULD be afforded, which is worth
    knowing before execution is ever wired up.
    """
    if segment not in ("equity", "commodity"):
        raise KiteError("segment must be equity or commodity")
    return _get("/user/margins/" + segment)


def logout() -> bool:
    """Invalidate the session at Kite and drop it locally."""
    s = session()
    if not s:
        return False
    try:
        requests.delete(BASE + "/session/token", headers=_HEADERS,
                        params={"api_key": config.KITE_API_KEY,
                                "access_token": s["access_token"]},
                        timeout=20)
    except Exception as e:
        _log("remote logout failed, clearing locally anyway: {}".format(e))
    db.clear_kite_session()
    _log("session cleared")
    return True

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
  * "never expose your api_secret ... do not expose the access_token". Neither
    is written anywhere. The secret is read from the environment; the session
    lives in this process's memory and is never persisted, returned by any
    endpoint, or logged.

    Not persisting the token is the point rather than an omission. It is a
    bearer credential for the WHOLE Kite API, order placement included,
    whatever this code can do — so a stored one sits in every database backup
    and volume snapshot as a live trading credential. Keeping it in memory
    costs a re-login after a redeploy, and the token dies at 06:00 daily
    regardless.

Uses `requests` rather than the pykiteconnect SDK: one less dependency, and the
SDK's surface includes order placement, which this stage must not have.
"""
import datetime
import hashlib

import requests

import config

BASE = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Kite wants its version pinned on every call.
_HEADERS = {"X-Kite-Version": "3"}


# The session lives in THIS PROCESS ONLY and is never written anywhere.
#
# An access_token is a bearer credential for the whole Kite API, order
# placement included, regardless of what this code can do. Persisting one puts
# a live trading credential in a database backup, in a volume snapshot, and in
# anything that can read either. It bought nothing: the token dies at 06:00 IST
# daily and the login is already a manual act, so the only cost of keeping it
# in memory is re-logging-in after a redeploy.
#
# If a background worker ever needs a session, this is the decision to revisit
# deliberately - not by quietly adding a table.
_SESSION = {}


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
    _SESSION.clear()
    _SESSION.update({
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "access_token": token,
        "expires_at": expires,
    })
    _log("session established for {} until {} IST".format(
        data.get("user_id"), expires.strftime("%Y-%m-%d %H:%M")))

    return {"user_id": data.get("user_id"), "user_name": data.get("user_name"),
            "expires_at": expires.isoformat(),
            "exchanges": data.get("exchanges"), "products": data.get("products")}


def session():
    """The in-memory session if it is still valid, else None."""
    if not _SESSION:
        return None
    expires = _SESSION.get("expires_at")
    if expires and expires <= datetime.datetime.now(expires.tzinfo or IST):
        _SESSION.clear()
        return None
    return dict(_SESSION)


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
                "reason": "no valid session - a human must log in again "
                          "(sessions are held in memory and do not survive a "
                          "restart, by design)",
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
    _SESSION.clear()
    _log("session cleared")
    return True

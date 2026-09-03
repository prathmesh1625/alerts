"""
test_kite.py — the Kite session layer, and the secrets it must not leak.

Two things this file exists to hold in place. First, that the module can
authenticate and read but cannot trade. Second, that neither the api_secret nor
the access_token escapes — Kite's docs warn about both explicitly, and the API
this is served through is reachable from the internet.

No network. Run: pytest test_kite.py   or   python test_kite.py
"""
import datetime
import hashlib

import config
import db
import kite


class Cfg:
    """Set Kite credentials for one test and put them back."""

    def __init__(self, key="testkey", secret="testsecret"):
        self.key, self.secret = key, secret

    def __enter__(self):
        self.saved = (config.KITE_API_KEY, config.KITE_API_SECRET)
        config.KITE_API_KEY, config.KITE_API_SECRET = self.key, self.secret
        return self

    def __exit__(self, *a):
        config.KITE_API_KEY, config.KITE_API_SECRET = self.saved


# -----------------------------------------------------------------------------
#  It cannot trade
# -----------------------------------------------------------------------------

def test_the_module_cannot_place_an_order():
    """
    The claim the whole shadow stage rests on. A session layer that can also
    trade is not a session layer.
    """
    src = open(kite.__file__, encoding="utf-8").read().lower()
    for forbidden in ("place_order", "modify_order", "cancel_order",
                      "transaction_type", "/orders/regular", "variety"):
        assert forbidden not in src, (
            "kite.py contains {!r} - it can no longer be called auth-only"
            .format(forbidden))


def test_it_does_not_use_the_sdk():
    """
    pykiteconnect's surface includes order placement. Using requests keeps the
    dependency small and keeps that surface out of the process entirely.
    """
    # An IMPORT, not a mention: the module names the SDK in order to explain
    # why it does not use it, and matching prose finds that explanation.
    src = open(kite.__file__, encoding="utf-8").read().lower()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "kiteconnect" not in stripped, stripped
    assert "import requests" in src


# -----------------------------------------------------------------------------
#  The checksum
# -----------------------------------------------------------------------------

def test_the_checksum_is_the_three_values_concatenated():
    """
    SHA-256 of api_key + request_token + api_secret, with no separator. Getting
    this wrong returns a generic failure that points nowhere, so it is pinned.
    """
    with Cfg():
        got = kite.checksum("reqtok")
    assert got == hashlib.sha256(b"testkeyreqtoktestsecret").hexdigest()
    assert len(got) == 64


def test_the_checksum_changes_with_the_request_token():
    with Cfg():
        assert kite.checksum("a") != kite.checksum("b")


# -----------------------------------------------------------------------------
#  Expiry — 6 AM, by regulation, with no refresh
# -----------------------------------------------------------------------------

def test_a_token_expires_at_six_the_next_morning():
    t = datetime.datetime(2026, 9, 3, 10, 0, tzinfo=kite.IST)
    e = kite.token_expiry(t)
    assert (e.day, e.hour) == (4, 6)


def test_a_token_taken_before_six_dies_the_same_morning():
    """A 05:00 login is good for an hour, not a day."""
    t = datetime.datetime(2026, 9, 3, 5, 0, tzinfo=kite.IST)
    e = kite.token_expiry(t)
    assert (e.day, e.hour) == (3, 6)


def test_six_exactly_rolls_to_the_next_day():
    t = datetime.datetime(2026, 9, 3, 6, 0, tzinfo=kite.IST)
    assert kite.token_expiry(t).day == 4


def test_an_expired_session_reads_as_no_session():
    saved = db.fetch_kite_session
    try:
        db.fetch_kite_session = lambda: {
            "access_token": "x",
            "expires_at": datetime.datetime.now(kite.IST) - datetime.timedelta(hours=1)}
        assert kite.session() is None
    finally:
        db.fetch_kite_session = saved


def test_a_live_session_reads_as_live():
    saved = db.fetch_kite_session
    try:
        db.fetch_kite_session = lambda: {
            "access_token": "x", "user_id": "AB1234",
            "expires_at": datetime.datetime.now(kite.IST) + datetime.timedelta(hours=5)}
        assert kite.session() is not None
    finally:
        db.fetch_kite_session = saved


# -----------------------------------------------------------------------------
#  Secrets
# -----------------------------------------------------------------------------

def test_status_never_carries_a_token():
    """
    /api/kite/status is served publicly. Kite's docs are explicit that the
    access_token must not be exposed.
    """
    saved = db.fetch_kite_session
    try:
        db.fetch_kite_session = lambda: {
            "access_token": "SECRET-TOKEN-VALUE", "user_id": "AB1234",
            "expires_at": datetime.datetime.now(kite.IST) + datetime.timedelta(hours=5)}
        with Cfg():
            body = repr(kite.status())
        assert "SECRET-TOKEN-VALUE" not in body, body
        assert "testsecret" not in body, body
    finally:
        db.fetch_kite_session = saved


def test_status_without_credentials_says_so_rather_than_crashing():
    with Cfg(key="", secret=""):
        st = kite.status()
    assert st["configured"] is False and st["live"] is False
    assert "not set" in st["reason"]


def test_the_api_secret_is_never_persisted():
    """It belongs in the environment only; the schema must have no column for it."""
    import inspect
    schema = inspect.getsource(db).lower()
    assert "api_secret" not in schema.split("create table if not exists kite_session")[1][:800]


def test_an_exchange_failure_reports_the_reason_not_the_token():
    class Resp:
        status_code = 400

        @staticmethod
        def json():
            return {"status": "error", "message": "Invalid `checksum`."}

    saved = kite.requests.post
    try:
        kite.requests.post = lambda *a, **k: Resp()
        with Cfg():
            try:
                kite.exchange("SECRET-REQUEST-TOKEN")
                assert False, "should have raised"
            except kite.KiteError as e:
                assert "checksum" in str(e).lower()
                assert "SECRET-REQUEST-TOKEN" not in str(e)
                assert "testsecret" not in str(e)
    finally:
        kite.requests.post = saved


def test_a_missing_request_token_is_refused_clearly():
    with Cfg():
        for bad in (None, ""):
            try:
                kite.exchange(bad)
                assert False, bad
            except kite.KiteError as e:
                assert "request_token" in str(e)


def test_reads_refuse_without_a_session_rather_than_sending_a_blank_token():
    saved = db.fetch_kite_session
    try:
        db.fetch_kite_session = lambda: None
        with Cfg():
            for fn in (kite.profile, lambda: kite.margins("equity")):
                try:
                    fn()
                    assert False, "should have raised"
                except kite.KiteError as e:
                    assert "session" in str(e).lower()
    finally:
        db.fetch_kite_session = saved


def test_an_unknown_margin_segment_is_refused():
    with Cfg():
        try:
            kite.margins("crypto")
            assert False
        except kite.KiteError as e:
            assert "equity or commodity" in str(e)


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

"""
test_api_security.py — what the public internet can and cannot see.

alerts.equityalerts.in serves this API with no login. That is a deliberate
choice for the dashboard's alert data and an unacceptable one for account
funds, the broker session, and the record of what would have been traded.

The bug this file exists for was real and recent: /api/kite/margins returns
cash balance, collateral and available funds, and it was added with no gate at
all. It had not been deployed yet, which is the only reason nothing leaked.

Run: pytest test_api_security.py   or   python test_api_security.py
"""
import config
import kite as kite_api

try:
    from fastapi.testclient import TestClient
    import api
    CLIENT = TestClient(api.app)
except Exception as e:                                    # pragma: no cover
    CLIENT = None
    _WHY = e

SENSITIVE = ("/api/kite/margins", "/api/kite/status", "/api/paper-trades")


class Token:
    """Set (or clear) the admin token for one test."""

    def __init__(self, value=""):
        self.value = value

    def __enter__(self):
        self.saved = config.ADMIN_TOKEN
        config.ADMIN_TOKEN = self.value
        return self

    def __exit__(self, *a):
        config.ADMIN_TOKEN = self.saved


def _skip():
    if CLIENT is None:
        print("  (fastapi TestClient unavailable: {})".format(_WHY))
    return CLIENT is None


# -----------------------------------------------------------------------------
#  The gate
# -----------------------------------------------------------------------------

def test_account_endpoints_refuse_without_a_token():
    if _skip():
        return
    with Token("s3cret"):
        for path in SENSITIVE:
            r = CLIENT.get(path)
            assert r.status_code == 401, (path, r.status_code, r.text[:200])


def test_a_wrong_token_is_refused():
    if _skip():
        return
    with Token("s3cret"):
        for path in SENSITIVE:
            r = CLIENT.get(path, headers={"X-Admin-Token": "wrong"})
            assert r.status_code == 401, (path, r.status_code)


def test_an_unset_admin_token_disables_rather_than_opens():
    """
    The property that matters most. A blank secret must never read as "checks
    off" - that is how a gate added for safety becomes the thing that removed
    it.
    """
    if _skip():
        return
    with Token(""):
        for path in SENSITIVE:
            r = CLIENT.get(path)
            assert r.status_code == 503, (path, r.status_code)
            assert "disabled" in r.text.lower()


def test_no_account_data_appears_in_any_refusal():
    if _skip():
        return
    with Token(""):
        for path in SENSITIVE:
            body = CLIENT.get(path).text.lower()
            for leak in ("access_token", "cash", "collateral", "user_id",
                         "api_secret"):
                assert leak not in body, (path, leak)


# -----------------------------------------------------------------------------
#  The callback is public, and must stay thin
# -----------------------------------------------------------------------------

def test_the_callback_stays_public():
    """
    Kite redirects a BROWSER here after login; it cannot carry a header. So it
    has to be reachable, and everything else about it has to be careful.
    """
    if _skip():
        return
    with Token("s3cret"):
        r = CLIENT.get("/api/kite/callback")
        assert r.status_code != 401, "the login callback cannot require a header"


def test_the_callback_returns_no_account_detail():
    """
    It used to hand back user_id, name, exchanges and products. Public endpoint,
    so it now confirms success and nothing more.
    """
    import inspect
    src = inspect.getsource(api.kite_callback)
    tail = src.split("return {")[-1]
    for leak in ("user_id", "user_name", "exchanges", "products", "**info"):
        assert leak not in tail, leak


def test_a_failed_exchange_does_not_echo_the_token():
    if _skip():
        return
    with Token("s3cret"):
        r = CLIENT.get("/api/kite/callback?request_token=SECRET-TOKEN-VALUE")
        assert "SECRET-TOKEN-VALUE" not in r.text


# -----------------------------------------------------------------------------
#  Nothing anywhere returns a secret
# -----------------------------------------------------------------------------

def test_no_endpoint_returns_the_access_token():
    """
    Behaviour, not prose. An earlier version of this test scanned api.py for
    the string "access_token" and failed on the DOCSTRING explaining that the
    token never leaves - the third time in this project a text scan flagged a
    comment about a rule as a breach of it. So: stand up a real session with a
    known token, call every gated endpoint, and look at what comes back.
    """
    if _skip():
        return
    import datetime
    import db

    saved_fetch = db.fetch_kite_session
    saved_margins = kite_api.margins
    try:
        db.fetch_kite_session = lambda: {
            "access_token": "SECRET-TOKEN-VALUE",
            "user_id": "AB1234",
            "expires_at": datetime.datetime.now(kite_api.IST)
            + datetime.timedelta(hours=5),
        }
        kite_api.margins = lambda segment="equity": {
            "equity": {"net": 99725.05, "available": {"cash": 245431.6}}}

        with Token("s3cret"):
            hdr = {"X-Admin-Token": "s3cret"}
            for path in ("/api/kite/status", "/api/kite/margins"):
                body = CLIENT.get(path, headers=hdr).text
                assert "SECRET-TOKEN-VALUE" not in body, path
                assert "s3cret" not in body, path
    finally:
        db.fetch_kite_session = saved_fetch
        kite_api.margins = saved_margins


def test_the_api_layer_never_touches_the_secret():
    """
    The api_secret is used only inside kite.exchange(). Checked by attribute
    access rather than by text, so a comment mentioning it does not fail.
    """
    assert not hasattr(api, "KITE_API_SECRET")
    # config holds it; the API module must not copy it anywhere of its own.
    module_values = [v for k, v in vars(api).items()
                     if isinstance(v, str) and k.isupper()]
    assert config.KITE_API_SECRET not in module_values or not config.KITE_API_SECRET


def test_status_is_gated_because_it_names_the_account():
    """
    /api/kite/status carries the Zerodha user id. Harmless-looking, and not
    something to publish next to a list of what the account is about to buy.
    """
    import inspect
    src = inspect.getsource(kite_api.status)
    assert "user_id" in src, "if this stops naming the account, re-check the gate"


def test_the_public_endpoints_are_the_expected_set():
    """
    A new endpoint is public unless someone remembers to gate it - which is
    exactly how /api/kite/margins was added. This fails when the set changes,
    so the decision has to be made again deliberately.
    """
    if _skip():
        return
    import fastapi
    public = set()
    for r in api.app.routes:
        path = getattr(r, "path", "")
        if not path.startswith(("/api", "/health")):
            continue
        names = [getattr(d.dependency, "__name__", "")
                 for d in (getattr(r, "dependencies", []) or [])]
        if "require_admin" not in names:
            public.add(path)

    expected = {
        "/health", "/api/config", "/api/alerts", "/api/stats", "/api/companies",
        "/api/filings", "/api/market", "/api/volume-alerts", "/api/near-misses",
        "/api/latency", "/api/kite/callback",
        "/api/alerts/{announcement_id}/pdf", "/api/filings/{announcement_id}/pdf",
    }
    assert public == expected, (
        "the public surface changed.\n  added: {}\n  removed: {}\n"
        "Gate it with Depends(require_admin) or add it here deliberately."
        .format(sorted(public - expected), sorted(expected - public)))


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

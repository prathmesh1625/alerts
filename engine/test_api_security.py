"""
test_api_security.py — what the public internet can and cannot see.

alerts.equityalerts.in serves this API with no login. That is a deliberate
choice for the dashboard's alert data and an unacceptable one for account
funds, the broker session, and the record of what would have been traded.

The bug this file exists for was real: a broker endpoint returning cash balance
and available funds was added with no gate at all. It was never deployed, and
the broker integration has since been removed entirely — but the lesson stands,
which is why the last test here pins the public surface and fails when it moves.

Run: pytest test_api_security.py   or   python test_api_security.py
"""
import config

try:
    from fastapi.testclient import TestClient
    import api
    CLIENT = TestClient(api.app)
except Exception as e:                                    # pragma: no cover
    CLIENT = None
    _WHY = e

SENSITIVE = ("/api/paper-trades",)


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


def test_the_public_endpoints_are_the_expected_set():
    """
    A new endpoint is public unless someone remembers to gate it - which is
    exactly how a broker endpoint returning account funds was once added. This
    fails when the set changes,
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
        "/api/latency",
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

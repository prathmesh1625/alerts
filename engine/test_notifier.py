"""
test_notifier.py — WhatsApp delivery.

The message formatting is the small half. The important half is the brakes:
this sends from the phone number the paying NSE product runs on, so the tests
that matter are the ones asserting it does NOT send.

No network and no database — the API and db calls are stubbed.

Run: pytest test_notifier.py   or   python test_notifier.py
"""
import datetime

import config
import db
import notifier
import whatsapp

ALERT = {
    "announcement_id": 4211,
    "company_symbol": "RAILTEL",
    "company_name": "RailTel Corporation of India Limited",
    "headline": "Order win Rs 164.79 Cr",
    "conviction": "STRONG",
    "score": 28.4,
    "order_value_cr": 164.79,
    "market_cap_cr": 12500.0,
    "announced_at": datetime.datetime(2026, 8, 20, 17, 20),
}


class Sent(list):
    """Stands in for the Meta API."""

    def __init__(self, fail_with=None):
        super().__init__()
        self.fail_with = fail_with

    def __call__(self, to, text, template_params=None):
        if self.fail_with:
            raise self.fail_with
        self.append((to, text))
        return "text", "wamid.TEST"


def _patch(monkey, sender=None, pending=None, today=0):
    """Swap out the network and the database for this test."""
    saved = (whatsapp.send, db.fetch_unnotified_alerts, db.count_notified_today,
             db.mark_notified)
    # `is not None`, not `or`: Sent subclasses list, so an empty one is falsy
    # and `sender or Sent()` silently swapped the failing stub for a working
    # one — the failure test passed while testing nothing.
    whatsapp.send = sender if sender is not None else Sent()
    db.fetch_unnotified_alerts = lambda p, s, a, limit=20: list(
        pending if pending is not None else [ALERT])[:limit]
    db.count_notified_today = lambda p: today
    db.mark_notified = lambda *a, **k: True
    monkey.append(saved)
    return whatsapp.send


def _restore(monkey):
    for saved in reversed(monkey):
        (whatsapp.send, db.fetch_unnotified_alerts, db.count_notified_today,
         db.mark_notified) = saved


class Config:
    """Set config for one test and put it back."""

    KEYS = ("WHATSAPP_ENABLED", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_RECIPIENTS", "WHATSAPP_MIN_SCORE", "WHATSAPP_MAX_PER_DAY",
            "WHATSAPP_TEMPLATE_NAME", "PUBLIC_BASE_URL")

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.saved = {k: getattr(config, k) for k in self.KEYS}
        for k, v in self.kw.items():
            setattr(config, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(config, k, v)


def enabled(**kw):
    base = dict(WHATSAPP_ENABLED=True, WHATSAPP_TOKEN="tok",
                WHATSAPP_PHONE_NUMBER_ID="123", WHATSAPP_RECIPIENTS=["919876543210"],
                WHATSAPP_MIN_SCORE=0.0, WHATSAPP_MAX_PER_DAY=25)
    base.update(kw)
    return Config(**base)


# -----------------------------------------------------------------------------
#  The brakes — it must refuse to send
# -----------------------------------------------------------------------------

def test_disabled_by_default():
    """A fresh deployment must not message anyone until asked to."""
    with Config(WHATSAPP_ENABLED=False):
        assert notifier._preflight() is False


def test_no_recipients_means_no_send():
    """
    There is deliberately no default audience. An empty list must stop the
    notifier, not fall back to "everyone" or to the NSE bot's subscribers.
    """
    with enabled(WHATSAPP_RECIPIENTS=[]):
        assert notifier._preflight() is False


def test_missing_credentials_refuse_to_start():
    for kw in ({"WHATSAPP_TOKEN": ""}, {"WHATSAPP_PHONE_NUMBER_ID": ""}):
        with enabled(**kw):
            assert notifier._preflight() is False, kw


def test_the_daily_cap_holds():
    """The cap is what stops a formula bug becoming a hundred messages."""
    m = []
    try:
        sender = _patch(m, today=25)
        with enabled(WHATSAPP_MAX_PER_DAY=25):
            assert notifier.run_once() == 0
        assert sender == []
    finally:
        _restore(m)


def test_the_cap_limits_the_batch_not_just_the_start():
    """With 3 left in the budget, a backlog of 10 sends 3."""
    m = []
    try:
        sender = _patch(m, pending=[dict(ALERT, announcement_id=i) for i in range(10)],
                        today=22)
        with enabled(WHATSAPP_MAX_PER_DAY=25):
            assert notifier.run_once() == 3
        assert len(sender) == 3
    finally:
        _restore(m)


def test_a_send_failure_is_not_recorded_as_sent():
    """
    If it is marked delivered on failure, the dedup key means the person never
    receives that alert at all. Nothing may be recorded unless the API confirms.
    """
    m = []
    marked = []
    try:
        _patch(m, sender=Sent(fail_with=whatsapp.WhatsAppError(400, 131000, "boom")))
        db.mark_notified = lambda *a, **k: marked.append(a)
        with enabled():
            assert notifier.run_once() == 0
        assert marked == [], "recorded a delivery that never happened"
    finally:
        _restore(m)


def test_every_recipient_gets_the_alert():
    m = []
    try:
        sender = _patch(m)
        with enabled(WHATSAPP_RECIPIENTS=["919876543210", "919000000001"]):
            assert notifier.run_once() == 2
        assert sorted(to for to, _ in sender) == ["919000000001", "919876543210"]
    finally:
        _restore(m)


def test_recipients_are_parsed_with_spaces_and_blanks():
    """A pasted env var is rarely tidy: "91111, 91222," must give two numbers."""
    import os
    saved = os.environ.get("WHATSAPP_RECIPIENTS")
    os.environ["WHATSAPP_RECIPIENTS"] = " 919876543210 , 919000000001 ,"
    try:
        import importlib
        importlib.reload(config)
        assert config.WHATSAPP_RECIPIENTS == ["919876543210", "919000000001"]
    finally:
        if saved is None:
            os.environ.pop("WHATSAPP_RECIPIENTS", None)
        else:
            os.environ["WHATSAPP_RECIPIENTS"] = saved
        import importlib
        importlib.reload(config)


def test_the_cap_and_the_dedup_are_per_recipient():
    """
    Adding a second number must not eat the first one's budget, and one
    recipient being capped must not silence the other.
    """
    m = []
    try:
        saved = (whatsapp.send, db.fetch_unnotified_alerts,
                 db.count_notified_today, db.mark_notified)
        m.append(saved)
        sender = Sent()
        whatsapp.send = sender
        # First number is at its cap; second has room.
        db.count_notified_today = lambda p: 25 if p == "919876543210" else 0
        db.fetch_unnotified_alerts = lambda p, s, a, limit=20: [ALERT][:limit]
        db.mark_notified = lambda *a, **k: True
        with enabled(WHATSAPP_RECIPIENTS=["919876543210", "919000000001"],
                     WHATSAPP_MAX_PER_DAY=25):
            assert notifier.run_once() == 1
        assert [to for to, _ in sender] == ["919000000001"]
    finally:
        _restore(m)


def test_one_recipient_failing_does_not_stop_the_others():
    """A number that is blocked or invalid must not silence the rest."""
    m = []
    try:
        saved = (whatsapp.send, db.fetch_unnotified_alerts,
                 db.count_notified_today, db.mark_notified)
        m.append(saved)
        got = []

        def flaky(to, text, template_params=None):
            if to == "919876543210":
                raise whatsapp.WhatsAppError(400, 131026, "not a test number")
            got.append(to)
            return "text", "wamid.OK"

        whatsapp.send = flaky
        db.count_notified_today = lambda p: 0
        db.fetch_unnotified_alerts = lambda p, s, a, limit=20: [ALERT][:limit]
        db.mark_notified = lambda *a, **k: True
        with enabled(WHATSAPP_RECIPIENTS=["919876543210", "919000000001"]):
            assert notifier.run_once() == 1
        assert got == ["919000000001"]
    finally:
        _restore(m)


def test_it_never_reads_a_subscriber_list():
    """
    The audience comes from config and nowhere else. This asserts the module
    has no lookup that could widen it - the NSE bot's users are not ours.
    """
    src = open(notifier.__file__, encoding="utf-8").read()
    for forbidden in ("subscriptions", "get_subscribers", "bot_data", "sqlite"):
        assert forbidden not in src, forbidden


# -----------------------------------------------------------------------------
#  What it sends
# -----------------------------------------------------------------------------

def test_the_message_leads_with_symbol_and_event():
    with Config(PUBLIC_BASE_URL="https://alerts.equityalerts.in"):
        msg = notifier.build_message(ALERT)
    first = msg.splitlines()[0]
    assert "RAILTEL" in first
    assert "STRONG" in first
    assert "Order win Rs 164.79 Cr" in msg
    assert "Score 28.4" in msg


def test_the_message_links_to_the_filing():
    with Config(PUBLIC_BASE_URL="https://alerts.equityalerts.in"):
        assert "https://alerts.equityalerts.in/api/alerts/4211/pdf" in \
            notifier.build_message(ALERT)


def test_the_message_survives_a_sparse_alert():
    """Most fields are nullable; a thin alert must still produce something."""
    thin = {"announcement_id": 1, "company_symbol": "ABC", "conviction": "WATCH"}
    msg = notifier.build_message(thin)
    assert "ABC" in msg and msg.strip()


def test_template_params_match_the_approved_shape():
    """`nse_bot` takes exactly 5 body variables; a mismatch is rejected."""
    params = notifier.build_template_params(ALERT)
    assert len(params) == 5
    assert all(isinstance(p, str) for p in params)
    assert "RAILTEL" in params[0]


def test_template_params_are_flattened_before_they_reach_meta():
    """
    Meta rejects newlines and 4+ space runs inside a {{n}} variable. The
    flattening belongs to whatsapp.send_template, so this asserts the payload
    that actually leaves the process rather than the notifier's intermediate.
    """
    captured = {}

    def fake_post(endpoint, payload):
        captured["payload"] = payload

        class R:
            @staticmethod
            def json():
                return {"messages": [{"id": "wamid.X"}]}
        return R()

    saved = whatsapp._post
    whatsapp._post = fake_post
    try:
        dirty = notifier.build_template_params(
            dict(ALERT, company_name="Rail\nTel    Corporation\tLtd"))
        whatsapp.send_template("919876543210", dirty, "nse_bot")
    finally:
        whatsapp._post = saved

    params = captured["payload"]["template"]["components"][0]["parameters"]
    assert len(params) == 5
    for p in params:
        t = p["text"]
        assert "\n" not in t and "\t" not in t and "    " not in t, repr(t)
        assert t, "an empty template variable is rejected by Meta"


def test_dry_run_sends_nothing():
    m = []
    try:
        sender = _patch(m)
        with enabled():
            assert notifier.run_once(dry_run=True) == 1
        assert sender == [], "dry run reached the API"
    finally:
        _restore(m)


# -----------------------------------------------------------------------------
#  The 24-hour window
# -----------------------------------------------------------------------------

def test_reengagement_is_recognised():
    assert whatsapp.WhatsAppError(400, 131047, "closed").is_reengagement is True
    assert whatsapp.WhatsAppError(400, 131000, "other").is_reengagement is False


def test_a_closed_window_falls_back_to_the_template():
    calls = []

    def fake_text(to, message):
        raise whatsapp.WhatsAppError(400, 131047, "window closed")

    def fake_template(to, params, template_name=None):
        calls.append((to, params))
        return "wamid.TPL"

    saved = (whatsapp.send_text, whatsapp.send_template)
    whatsapp.send_text, whatsapp.send_template = fake_text, fake_template
    try:
        with Config(WHATSAPP_TEMPLATE_NAME="nse_bot"):
            channel, wamid = whatsapp.send("919876543210", "hi", ["a"])
        assert channel == "template" and wamid == "wamid.TPL"
        assert len(calls) == 1
    finally:
        whatsapp.send_text, whatsapp.send_template = saved


def test_a_real_error_is_not_swallowed_by_the_fallback():
    """Only 131047 means "use a template". Everything else must surface."""
    def fake_text(to, message):
        raise whatsapp.WhatsAppError(401, 190, "token expired")

    saved = whatsapp.send_text
    whatsapp.send_text = fake_text
    try:
        with Config(WHATSAPP_TEMPLATE_NAME="nse_bot"):
            try:
                whatsapp.send("919876543210", "hi", ["a"])
                assert False, "a token error was swallowed"
            except whatsapp.WhatsAppError as e:
                assert e.error_code == 190
    finally:
        whatsapp.send_text = saved


def test_sanitize_never_returns_empty():
    """Meta rejects an empty template variable."""
    assert whatsapp.sanitize_template_param("")
    assert whatsapp.sanitize_template_param(None)
    assert whatsapp.sanitize_template_param("   \n  ")


def test_sanitize_truncates_long_values():
    out = whatsapp.sanitize_template_param("x" * 5000)
    assert len(out) <= whatsapp.TEMPLATE_PARAM_MAX_LEN


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

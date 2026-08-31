"""
test_rescore.py — a rule fix must repair what it already got wrong.

Every rule fix so far applied only to filings that arrived afterwards. E2E
Networks' Rs 1,000 Cr order was still sitting at score 0.0 after the rule that
rejected it was corrected, because nothing revisited a filing already marked
ANALYZED. These cover the mechanism that fixes that.

No database and no network.

Run: pytest test_rescore.py   or   python test_rescore.py
"""
import config
import db
import scoring
import signals as S

config.ORDER_RULE_ENABLED = True

E2E_SIGNALS = {
    "company_name": "E2E Networks Limited",
    "document_type": "ORDER_WIN",
    "orders": [{
        "raw_value": 1000.0, "unit": "crore", "status": "NEW",
        "customer": "Sovereign AI company based in India",
        "scope": "NVIDIA Blackwell cloud GPUs and allied services",
        "quote": ("the Company has entered into the binding term sheet with a "
                  "Sovereign AI company based in India requiring "
                  "high-performance computing infrastructure, for the provision "
                  "of NVIDIA Blackwell cloud GPUs and allied services."),
    }],
    "evidence": ["Approximately Rs. 1,000 Crore (exclusive of applicable taxes)."],
}
E2E_TITLE = "Bagging/Receiving of orders/contracts"


# -----------------------------------------------------------------------------
#  The version stamp
# -----------------------------------------------------------------------------

def test_the_version_moves_when_a_rule_changes():
    """
    Derived, not hand-maintained. A rule change that did not move the version
    would leave the affected filings stranded at their old verdict.
    """
    before = scoring.formula_version()
    saved = config.ORDER_MIN_CR
    try:
        config.ORDER_MIN_CR = saved + 1
        assert scoring.formula_version() != before
    finally:
        config.ORDER_MIN_CR = saved
    assert scoring.formula_version() == before


def test_the_version_moves_when_a_PATTERN_changes():
    """
    Every miss so far came from the patterns, not the thresholds — so the
    version has to follow those too, or exactly the fixes that matter most
    would not trigger a re-score.
    """
    before = scoring.formula_version()
    saved = S._IS_AN_ORDER_RE
    try:
        import re
        S._IS_AN_ORDER_RE = re.compile(saved.pattern + "|widget")
        assert scoring.formula_version() != before
    finally:
        S._IS_AN_ORDER_RE = saved
    assert scoring.formula_version() == before


def test_the_version_is_stable_across_calls():
    assert scoring.formula_version() == scoring.formula_version()


def test_the_version_fits_the_column():
    assert len(scoring.formula_version()) <= 16


# -----------------------------------------------------------------------------
#  Re-scoring from stored signals
# -----------------------------------------------------------------------------

def test_stored_signals_rebuild_into_a_scorable_filing():
    """
    The whole approach rests on raw_signals being a faithful round trip. If
    JSONB cannot be rebuilt into FilingSignals, re-scoring is impossible and
    every fix stays forward-only.
    """
    sig = S.FilingSignals(**E2E_SIGNALS)
    assert sig.document_type == "ORDER_WIN"
    assert len(sig.orders) == 1
    assert sig.orders[0].raw_value == 1000.0
    assert sig.orders[0].status == "NEW"


def test_the_e2e_filing_recovers_under_the_current_formula():
    """
    The exact stored row from production, which scored 0.0. Re-scored now, it
    must qualify — that is what a re-score pass is for.
    """
    sig = S.FilingSignals(**E2E_SIGNALS)
    r = scoring.score_filing(sig, "", E2E_TITLE)
    assert "ORDER_WIN" in r["rules_hit"], r["breakdown"]["rules"][-1]["note"]
    assert r["order_value_cr"] == 1000.0
    assert r["qualifies"] is True


def test_rescoring_does_not_resurrect_a_rejection():
    """
    A re-score can only ever turn a rejection into an alert, so it must not
    become a way for the guarded cases to sneak back in.
    """
    cases = {
        "certificate of deposit": {
            "raw_value": 35000.0, "unit": "crore", "status": "NEW",
            "scope": "Certificate of deposit",
            "quote": "Certificate of deposit programme of Rs 35,000 crore"},
        "terminated order": {
            "raw_value": 205.89, "unit": "crore", "status": "TERMINATED",
            "scope": "work order",
            "quote": "the value of the work order is Rs 205,89,14,000/-"},
        "related-party ceiling": {
            "raw_value": 1500.0, "unit": "crore", "status": "NEW",
            "customer": "Maruti Suzuki India Limited",
            "scope": "contract(s) / arrangement(s)",
            "quote": "the consent of Members is hereby accorded for related "
                     "party transactions"},
    }
    for label, order in cases.items():
        sig = S.FilingSignals(document_type="ORDER_WIN", orders=[order])
        r = scoring.score_filing(sig, "", E2E_TITLE)
        assert r["rules_hit"] == [], "{} recovered: {}".format(
            label, r["breakdown"]["rules"][-1]["note"])


def test_a_termination_still_needs_the_document_to_be_re_read():
    """
    document_reports_order_loss is the backstop for a termination the model
    labelled NEW, and it needs the PDF text. Re-scoring without that text
    applies WEAKER checks than the original pass — so agent.rescore_one must
    bail out rather than judge on less evidence. Asserted against the source,
    because the guarantee is the early return.
    """
    import inspect
    import agent
    src = inspect.getsource(agent.rescore_one)
    assert "document_reports_order_loss" in src or "extract_text_from_pdf_file" in src
    assert 'return ""' in src, "no path that declines to re-score without text"

    # And the check itself still works when text IS available.
    sig = S.FilingSignals(document_type="ORDER_WIN", orders=[{
        "raw_value": 205.89, "unit": "crore", "status": "NEW",
        "scope": "work order",
        "quote": "the value of the work order is Rs 205,89,14,000/-"}])
    text = ("Subject: Termination of work order. The Company has received a "
            "communication regarding termination of the aforesaid work order.")
    assert scoring.score_filing(sig, text, E2E_TITLE)["rules_hit"] == []


# -----------------------------------------------------------------------------
#  Withdrawal — the other direction
#
#  HAPPSTMNDS' promoter share sale alerted as a Rs 1,330 Cr order win.
#  Correcting the rule fixed future filings but left the wrong alert on the
#  dashboard, because re-scoring could only ever ADD one.
# -----------------------------------------------------------------------------

SPA_SIGNALS = {
    "company_name": "Happiest Minds Technologies Ltd",
    "document_type": "BOTH",
    "orders": [{
        "raw_value": 1329.72, "unit": "crore", "status": "NEW",
        "customer": "ITC Infotech India Limited",
        "scope": "sale of equity shares to ITC Infotech India Limited",
        "quote": ("for sale of 3,36,61,700 equity shares of Happiest Minds "
                  "Technologies Limited held by them, representing 22.106% of "
                  "the paid-up equity share capital of the Company, to the "
                  "Purchaser for an aggregate consideration of INR "
                  "13,29,71,77,710"),
    }],
}


def test_the_share_sale_no_longer_qualifies():
    sig = S.FilingSignals(**SPA_SIGNALS)
    r = scoring.score_filing(sig, "", "Board Meeting Outcome")
    assert r["rules_hit"] == [], r["breakdown"]["rules"][-1]["note"]
    assert r["qualifies"] is False


def test_a_corrected_rule_can_withdraw_an_alert():
    """
    A rule fix has to work in both directions, or the dashboard keeps showing
    something the formula no longer stands behind.
    """
    import inspect
    import agent
    assert hasattr(agent, "withdraw_stale_alerts")
    src = inspect.getsource(agent.withdraw_stale_alerts)
    assert "withdraw_alert" in src
    # Same evidence bar as recovery: stale stamp, re-read PDF, still not
    # qualifying. Never on a guess.
    assert 'result["qualifies"]' in src
    assert "extract_text_from_pdf_file" in src
    assert "fetch_stale_alerts" in src


def test_withdrawal_is_logged_with_what_it_removed():
    """A card that vanishes with no explanation is worse than a wrong one."""
    import inspect
    import agent
    src = inspect.getsource(agent.withdraw_stale_alerts)
    assert "WITHDRAWN" in src
    assert "headline" in src


def test_withdrawal_leaves_the_filing_record_intact():
    """
    Only the dashboard card goes. The filings browser must still show that the
    filing was seen and what was made of it.
    """
    import inspect
    src = inspect.getsource(db.withdraw_alert)
    # The statements only — the docstring is free to mention the ledger, and
    # matching prose rather than SQL is how this test failed first time round.
    statements = [ln.strip().lower() for ln in src.splitlines()
                  if "delete" in ln.lower() or "update" in ln.lower()
                  or "insert" in ln.lower()]
    assert statements, "no write statement found"
    assert all("stock_alerts" in st for st in statements), statements
    assert not any("filing_analyses" in st for st in statements), \
        "withdrawal must not erase the ledger"


def test_rescore_runs_only_when_nothing_new_is_waiting():
    """
    Repairing yesterday must never delay today. The rescore pass is spare-time
    work, taken only when the live queue is empty.
    """
    import inspect
    import agent
    src = inspect.getsource(agent.run_cycle)
    assert "rescore_cycle()" in src
    before = src.index("fetch_unanalyzed")
    after = src.index("rescore_cycle()")
    assert before < after, "re-scoring competes with live filings"


def test_rescore_is_bounded():
    """It must drain gradually, not scan the whole history in one cycle."""
    assert 0 < config.RESCORE_BATCH <= 50
    assert 0 < config.RESCORE_DAYS <= 30


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

"""
test_prefilter.py — the gate that decides whether we pay OpenAI for a filing.

Two ways to be wrong, and they cost differently:
  * letting routine compliance filings through wastes money on every one;
  * blocking a real results or order filing loses an alert entirely.

The second is much worse, so the cases below lean on proving that real filings
get through even when their titles are unhelpful.

Run: pytest test_prefilter.py   or   python test_prefilter.py
"""
from prefilter import should_analyze, should_open_pdf

RESULTS_BODY = """
    STATEMENT OF UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED 30 SEPTEMBER 2025
    (Rs. in Lakhs)
    Particulars                          Quarter ended   Quarter ended   Quarter ended
    Revenue from operations                   45,231.00       38,110.00       29,870.00
    Other income                                 812.00          640.00          590.00
    Total income                               46,043.00       38,750.00       30,460.00
    Total expenses                             39,120.00       33,400.00       27,010.00
    Profit before tax                           6,923.00        5,350.00        3,450.00
    Profit after tax                            5,180.00        4,000.00        2,580.00
    Earnings per share (basic and diluted)          4.15            3.20            2.06
"""

ORDER_BODY = """
    We wish to inform you that the Company has received a Letter of Award from
    NTPC Limited for the supply and installation of solar modules. The value of
    the order is Rs. 412.50 crore (excluding GST), to be executed over 18 months.
"""

COMPLIANCE_BODY = """
    Pursuant to Regulation 7(3) of the SEBI (Listing Obligations and Disclosure
    Requirements) Regulations, 2015, we enclose the compliance certificate for
    the half year ended 30 September 2025, duly signed by the Compliance Officer
    and the Registrar and Share Transfer Agent of the Company.
"""


def _yes(title, body):
    ok, reason = should_analyze(title, body)
    assert ok, "expected ANALYZE for {!r}, got: {}".format(title, reason)
    return reason


def _no(title, body):
    ok, reason = should_analyze(title, body)
    assert not ok, "expected SKIP for {!r}, but it was sent to the model".format(title)
    return reason


# --- things that must reach the model ---------------------------------------

def test_results_title_passes():
    _yes("Financial Results for the quarter ended 30.09.2025", RESULTS_BODY)


def test_board_meeting_outcome_passes():
    _yes("Outcome of Board Meeting", RESULTS_BODY)


def test_results_pass_on_body_even_with_an_opaque_title():
    # The title says nothing useful; the statement in the body must carry it.
    reason = _yes("Submission under Regulation 33", RESULTS_BODY)
    assert "results statement" in reason


def test_order_win_title_passes():
    _yes("Receipt of Order", ORDER_BODY)


def test_order_win_passes_on_body_alone():
    reason = _yes("Intimation to Stock Exchange", ORDER_BODY)
    assert "order win" in reason


def test_newspaper_publication_of_results_passes():
    # Scanned newspaper result cuttings are a real and easily-lost case: the
    # text here is what OCR recovered from the image, which is ragged but
    # still has to get through on the strength of the title.
    ocr_text = "EXTRACT OF UNAUDITED FINANCIAL RESULTS Revenue frm operatons 45,231"
    _yes("Newspaper Advertisement for Financial Results", ocr_text)


def test_order_stated_only_as_a_value_passes():
    body = "The Order value is Rs. 412.50 crore, awarded by Indian Railways."
    _yes("Intimation", body)


# --- things that must NOT reach the model -----------------------------------

def test_trading_window_is_skipped():
    _no("Closure of Trading Window", COMPLIANCE_BODY)


def test_shareholding_pattern_is_skipped():
    _no("Shareholding Pattern for the quarter ended September 2025", COMPLIANCE_BODY)


def test_compliance_certificate_is_skipped():
    _no("Certificate under Regulation 7(3)", COMPLIANCE_BODY)


def test_unremarkable_filing_is_skipped():
    reason = _no("Intimation of change in address", "We wish to inform the exchange of a change in the registered office address.")
    assert "no results statement" in reason


def test_empty_text_is_skipped_with_a_clear_reason():
    ok, reason = should_analyze("Financial Results", "")
    assert not ok
    assert "no extractable text" in reason


# --- the ordering guarantee -------------------------------------------------

def test_a_misleading_title_cannot_discard_real_results():
    # Title matches the never-relevant list, body is a genuine results table.
    # The body check runs FIRST, so this must still go to the model.
    _yes("Shareholding Pattern", RESULTS_BODY)


# --- the title screen: the CPU lever -----------------------------------------
#
# should_open_pdf runs BEFORE the PDF is parsed, so a wrong "no" here loses an
# alert with no chance of recovery. These cases guard that boundary.

def test_routine_compliance_titles_are_not_opened():
    for title in ("Closure of Trading Window",
                  "Shareholding Pattern for the quarter ended September 2025",
                  "Certificate under Regulation 7(3)",
                  "Corporate Governance Report",
                  "Voting results of the AGM"):
        ok, why = should_open_pdf(title)
        assert not ok, "should not have opened: {}".format(title)
        assert "not opened" in why


def test_results_and_order_titles_are_always_opened():
    for title in ("Financial Results for the quarter ended 30.09.2025",
                  "Outcome of Board Meeting",
                  "Receipt of Order",
                  "Intimation of Letter of Award",
                  "Newspaper Advertisement for Financial Results"):
        ok, _ = should_open_pdf(title)
        assert ok, "should have opened: {}".format(title)


def test_inconclusive_title_is_opened():
    """The default must be to read the document, not to discard it."""
    for title in ("Intimation to Stock Exchange",
                  "Submission under Regulation 33",
                  "Press Release",
                  "Disclosure"):
        ok, why = should_open_pdf(title)
        assert ok, title
        assert "inconclusive" in why


def test_blank_title_is_opened():
    # devseed leaves titles null; a live scraper always sets one. Either way,
    # no title means no basis to judge — so read the document.
    assert should_open_pdf("")[0]
    assert should_open_pdf(None)[0]
    assert should_open_pdf("   ")[0]


def test_a_positive_signal_beats_a_routine_one_in_the_same_title():
    # "Outcome of Board Meeting and closure of trading window" must still open:
    # the results half is what matters.
    ok, _ = should_open_pdf("Outcome of Board Meeting and Closure of Trading Window")
    assert ok


def test_title_screen_can_be_disabled():
    import config
    old = config.SKIP_BY_TITLE
    config.SKIP_BY_TITLE = False
    try:
        ok, why = should_open_pdf("Closure of Trading Window")
        assert ok and "disabled" in why
    finally:
        config.SKIP_BY_TITLE = old


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
            print("  FAIL  {}\n        {}".format(name, e))
            failed += 1
        except Exception as e:
            print("  ERROR {}  {}: {}".format(name, type(e).__name__, e))
            failed += 1
    print("\n{} passed, {} failed".format(passed, failed))
    raise SystemExit(1 if failed else 0)

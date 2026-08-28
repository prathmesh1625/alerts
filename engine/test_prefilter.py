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


# --- presentations and transcripts restate results, they do not report them ---
#
# These pass every results test — the numbers in them are real — but they are
# numbers already published and already alerted on, so letting one through
# produces a duplicate alert days later on stale data.

DECK = """
    Q2 FY26 Investor Presentation
    Disclaimer: This presentation has been prepared by the Company.
    This presentation contains forward-looking statements. Safe Harbour.
    Revenue from operations 45,231 Total income 46,043 Profit after tax 5,180
    Profit before tax 6,923 Earnings per share 4.15 quarter ended
"""

TRANSCRIPT = """
    Moderator: Ladies and gentlemen, good day and welcome to the Q2 FY26
    earnings conference call of the Company. Moderator: We will now begin the
    question-and-answer session. Moderator: The next question is from the line
    of an analyst. Revenue from operations grew to 45,231 lakh and profit after
    tax was 5,180 lakh for the quarter ended.
"""


def test_presentation_titles_are_not_opened():
    for title in ("Investor Presentation",
                  "Q2 FY26 Earnings Presentation",
                  "Corporate Presentation",
                  "Results Presentation",
                  "Analyst Presentation"):
        ok, why = should_open_pdf(title)
        assert not ok, "opened a presentation: {}".format(title)
        assert "not opened" in why


def test_transcript_titles_are_not_opened():
    for title in ("Transcript of the Q2 FY26 Earnings Call",
                  "Audio recording of the conference call",
                  "Analysts/Institutional Investor Meet/Con. Call Updates",
                  "Intimation of Analyst Meet"):
        assert not should_open_pdf(title)[0], title


def test_a_deck_with_an_innocent_title_is_caught_by_its_body():
    """
    Title gives nothing away, and the body contains a genuine results table —
    so every results test passes. Only the slide-deck markers stop it.
    """
    ok, why = should_analyze("Submission under Regulation 30", DECK)
    assert not ok, "a presentation reached the model"
    assert "presentation" in why


def test_a_transcript_with_an_innocent_title_is_caught_by_its_body():
    ok, why = should_analyze("Intimation to Stock Exchange", TRANSCRIPT)
    assert not ok, "a transcript reached the model"
    assert "transcript" in why


def test_a_real_results_filing_is_still_analysed():
    """The exclusion must not swallow the filings the screen exists for."""
    ok, _ = should_analyze("Financial Results for the quarter ended 30.09.2025",
                           RESULTS_BODY)
    assert ok


def test_a_results_filing_mentioning_a_call_once_is_still_analysed():
    """
    Results filings routinely say "an earnings call will be held on...". One
    mention must not disqualify them — hence the two-marker rule.
    """
    body = RESULTS_BODY + "\nAn earnings call will be held on 5 November 2025."
    ok, _ = should_analyze("Outcome of Board Meeting", body)
    assert ok


def test_a_results_filing_with_one_disclaimer_is_still_analysed():
    body = RESULTS_BODY + "\nDisclaimer: figures are subject to audit."
    ok, _ = should_analyze("Financial Results", body)
    assert ok


def test_an_order_win_is_never_mistaken_for_a_deck():
    ok, _ = should_analyze("Receipt of Order", ORDER_BODY)
    assert ok


# -----------------------------------------------------------------------------
#  A terminated order is not an order win — the free layer
#
#  "Termination of work order" matches _ORDER_TITLE on the bare word "order",
#  so RPPINFRA's cancelled Rs 205.89 Cr contract was opened, read and scored
#  27.94 STRONG. Catching it here costs nothing and saves the model call.
# -----------------------------------------------------------------------------

def test_a_terminated_order_title_is_not_opened():
    for title in (
        "Termination of work order - intimation pursuant to Regulation 30 of "
        "the SEBI (LODR) Regulations 2015",
        "Termination of Work Order",
        "Cancellation of purchase order",
        "Intimation regarding termination of contract",
        "Withdrawal of Letter of Award",
        "Work order terminated by the customer",
        "Foreclosure of the project awarded earlier",
    ):
        ok, why = should_open_pdf(title)
        assert not ok, "should not have opened: {}".format(title)
        assert "terminated or cancelled" in why, why


def test_termination_screening_does_not_swallow_genuine_wins():
    """
    The risk of the check above. These must all still be opened — an order won
    is the only thing the live formula looks for, so a false positive here is
    the whole rule going quiet.
    """
    for title in (
        "Receipt of Order",
        "Intimation of Letter of Award",
        "Receipt of work order from NTPC Limited",
        "Bagging of a new contract",
        "Award of contract worth Rs. 412 crore",
        "Declared L1 for a project",
        # Wins that merely CONTAIN a termination word in another role.
        "Receipt of work order - no compensation for cancellation clause",
    ):
        ok, why = should_open_pdf(title)
        assert ok, "should have opened: {} ({})".format(title, why)


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

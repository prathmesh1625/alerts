"""
prefilter.py — the cheap gate in front of the expensive model call.

The scraper ingests EVERY filing for every subscribed company, and the large
majority of them (trading-window closures, shareholding patterns, AGM notices,
compliance certificates, credit-rating affirmations) can never satisfy any of
the three formula rules. Sending each one to gpt-4o-mini would multiply the
bill by roughly an order of magnitude for zero additional alerts.

So: a filing goes to the model only if its title or its text shows a results
statement or an order win. Everything else is recorded as SKIPPED with a reason,
which keeps the ledger honest — a skipped filing is a decision we can audit,
not a silent drop.

Set PREFILTER_ENABLED=false to send everything to the model.
"""
import re

import config
from pdf_text import count_metric_terms, looks_like_financial_results

# --- titles that are never actionable ----------------------------------------
# Matched against the announcement title only, and only used to skip EARLY (we
# still fall through to the text checks, so a misleading title cannot on its own
# discard a filing that really does contain results).
_NEVER_RELEVANT_TITLE = re.compile(
    r"trading window|shareholding pattern|closure of trading|"
    r"notice of (?:agm|egm|postal ballot)|scrutinizer|voting results|"
    # The spelled-out form, with the ordinal exchanges put in it ("Notice Of
    # 29Th Annual General Meeting"). The abbreviation above missed all of them:
    # 21 such filings were in a 900-filing sample and 5 had been sent to the
    # model, producing no alerts.
    r"notice of\s+(?:the\s+)?(?:\d+\s*(?:st|nd|rd|th)\s*)?"
    r"(?:agm|annual general meeting)|"
    # A REGULATORY order - a penalty, demand or adjudication passed AGAINST the
    # company - is not an order win. This is not merely noise: "Action(s) taken
    # or orders passed" was being reported as "Order win Rs 364.73 Cr" for
    # ICICIPRULI, turning enforcement action into a buy signal.
    r"action\(?s?\)? taken or orders? passed|"
    r"orders? passed by|penalty|adjudicat|show cause|demand notice|"
    r"prosecution|search and seizure|"
    r"reg\.?\s*7\(3\)|certificate under regulation 7|"
    r"compliance certificate|investor complaint|grievance redressal|"
    r"loss of share certificate|duplicate share|"
    r"change in (?:company )?secretary|appointment of (?:company )?secretary|"
    r"registrar and (?:share )?transfer agent|"
    r"disclosure under regulation 29|regulation 31|"
    r"annual secretarial|corporate governance report",
    re.IGNORECASE,
)

# --- titles that are worth the model call regardless of what the text yields --
_RESULTS_TITLE = re.compile(
    r"financial results|unaudited results|audited results|"
    r"outcome of board meeting|board meeting outcome|"
    r"quarterly result|standalone and consolidated|"
    r"newspaper (?:publication|advertisement).*result|result.*newspaper",
    re.IGNORECASE,
)

_ORDER_TITLE = re.compile(
    r"\border\b|\borders\b|contract|letter of (?:award|intent|acceptance)|"
    r"\bloa\b|\bloi\b|work order|purchase order|supply order|"
    r"bags?\b|bagged|secured|awarded|awarding|"
    r"receipt of order|new project|project win",
    re.IGNORECASE,
)

# --- body text signals -------------------------------------------------------
_ORDER_BODY = re.compile(
    r"(received|receipt of|secured|bagged|awarded|won|declared\s+l1|"
    r"lowest bidder|letter of award|letter of acceptance|letter of intent|"
    r"work order|purchase order|supply order)"
    r"[\s\S]{0,400}?"
    r"(crore|cr\.|lakh|million|billion|rs\.?\s*\d|inr\s*\d|₹\s*\d)",
    re.IGNORECASE,
)

# A crore/lakh amount anywhere near the word "order" — catches phrasings the
# verb list above misses ("the Order value is Rs. 412.50 crore").
_ORDER_AMOUNT = re.compile(
    r"order[\s\S]{0,200}?(?:rs\.?|inr|₹)\s*[\d,.]+\s*(?:crore|cr\b|lakh|million)",
    re.IGNORECASE,
)


# ── documents that RESTATE results rather than report them ───────────────────
#
# An investor presentation or an earnings-call transcript repeats last
# quarter's figures. Every growth test passes on them — the numbers are real —
# but they are numbers the screen has already seen and alerted on, so they
# produce a duplicate alert days later on stale data.
#
# The title list extends bot/output.py's `_NON_RESULTS_TITLE_RE`, which covers
# transcripts and meets but not presentations.
_RESTATES_RESULTS_TITLE = re.compile(
    r"transcript|audio recording|audio link|earnings call|conference call|"
    r"con\.?\s*call|analyst meet|investor meet|analyst day|investor day|"
    r"(?:investor|earnings|corporate|results?|analyst|institutional)"
    # "presentaion" and "presentaton" are common misspellings in filed titles,
    # and a title is typed by hand at the company that files it.
    r"[\s\-]*presenta(?:tion|ion|ton)|"
    r"presenta(?:tion|ion|ton)\s+(?:to|for)\s+(?:investors|analysts)|"
    r"schedule of (?:analyst|investor)|intimation of (?:analyst|investor)|"
    # Annual reports restate a year already reported at Q4 - the same defect,
    # found in live data: four of 39 alerts were "Reg. 34 (1) Annual Report".
    #
    # Regulation 34 is the annual report; 36(1)(b) is the copy sent to members.
    # Both are last year's numbers. Regulation 33 is deliberately ABSENT: that
    # is the regulation quarterly RESULTS are filed under, so excluding it would
    # suppress the filings this screen exists to catch.
    r"annual report|"
    r"reg(?:ulation)?\.?\s*34\b|"
    r"reg(?:ulation)?\.?\s*36\s*\(\s*1\s*\)|"
    # The machine-readable XBRL copy of a results table already filed as a PDF.
    r"machine[\s\-]?readable|"
    # Business-responsibility and sustainability reports are an annual-report
    # annexe; one 106k-character BRSR already slipped past the body prefilter.
    r"brsr|business responsibility",
    re.IGNORECASE,
)

# Dialogue shape, for a transcript whose title does not say so. Lifted from
# bot/output.py's `_TRANSCRIPT_MARKERS`, including its rule that TWO markers
# must clear their thresholds — one passing mention of a call is not enough.
_TRANSCRIPT_MARKERS = (
    (re.compile(r"\bmoderator\b", re.IGNORECASE), 3),
    (re.compile(r"\bnext question\b", re.IGNORECASE), 2),
    (re.compile(r"ladies and gentlemen", re.IGNORECASE), 1),
    (re.compile(r"question[\s\-]?and[\s\-]?answer session", re.IGNORECASE), 1),
    (re.compile(r"\bearnings (?:conference )?call\b", re.IGNORECASE), 2),
    (re.compile(r"\bconference call\b", re.IGNORECASE), 2),
)

# A slide deck's own furniture. Same two-marker rule, so a results PDF that
# happens to say "safe harbour" once is unaffected.
_PRESENTATION_MARKERS = (
    (re.compile(r"safe harbou?r", re.IGNORECASE), 1),
    (re.compile(r"this presentation", re.IGNORECASE), 2),
    (re.compile(r"forward[\s\-]looking statements", re.IGNORECASE), 1),
    (re.compile(r"\bdisclaimer\b", re.IGNORECASE), 2),
    (re.compile(r"investor presentation", re.IGNORECASE), 1),
    (re.compile(r"\bQ[1-4]\s*FY\s*\d{2,4}\s+(?:earnings|results)\s+presentation",
                re.IGNORECASE), 1),
)


# Enforcement and tax matters. These carry large rupee figures and the word
# "order", so without them a penalty reads as new business — the one failure
# mode here that is worse than a missed alert, because it points the wrong way.
_REGULATORY_ORDER = re.compile(
    r"action\(?s?\)? taken or orders? passed|"
    r"orders? passed by|assessment order|adjudicat|"
    r"penalt|show cause|demand notice|prosecution|search and seizure|"
    r"\bfine (?:of|imposed)|imposition of",
    re.IGNORECASE,
)


def _markers_clear(text: str, markers) -> int:
    return sum(1 for rx, threshold in markers if len(rx.findall(text)) >= threshold)


def restates_old_results(title: str, text: str = "") -> tuple:
    """
    True when this document REPEATS results rather than reporting them.

    Returns (is_restatement, reason).

    Deliberately checked BEFORE the positive results/order signals: a deck
    titled "Q2 FY26 Investor Presentation" contains a genuine results table,
    so every body test passes and only this check can stop it.
    """
    title = title or ""
    if _RESTATES_RESULTS_TITLE.search(title):
        return True, "presentation/call material, not a fresh result"

    if not text:
        return False, ""

    if _markers_clear(text, _TRANSCRIPT_MARKERS) >= 2:
        return True, "reads as an earnings-call transcript"
    if _markers_clear(text, _PRESENTATION_MARKERS) >= 2:
        return True, "reads as an investor presentation"
    return False, ""


def should_open_pdf(title: str) -> tuple:
    """
    Decide from the TITLE ALONE whether this PDF is worth opening at all.

    Returns (should_open: bool, reason: str).

    This is the CPU lever. Parsing a PDF is the most expensive thing this
    service does — far more than the network wait on OpenAI — and the scraper
    ingests every filing for every subscribed company, most of which are
    routine compliance documents. Without this check we pay full extraction
    cost on all of them just to discard them a millisecond later.

    The trade-off is deliberate and narrow. `should_analyze` checks the BODY
    before it honours a never-relevant title, precisely so a misleading title
    cannot discard a real results filing. Skipping here gives that protection
    up for the titles on `_NEVER_RELEVANT_TITLE` only — captions like "Trading
    Window closure" and "Shareholding Pattern", which are defined by regulation
    and never carry a results statement or an order win.

    Set SKIP_BY_TITLE=false to always open the PDF (more thorough, several
    times the CPU).
    """
    if not config.SKIP_BY_TITLE:
        return True, "title screening disabled"

    title = title or ""
    if not title.strip():
        # The scrapers always store a title; a blank one means we cannot judge,
        # so fall through to the full read rather than guess.
        return True, "no title to screen on"

    # Checked BEFORE the positive signals: "Q2 FY26 Results Presentation"
    # matches _RESULTS_TITLE, but it restates figures already alerted on.
    restated, why = restates_old_results(title)
    if restated:
        return False, "{}, not opened".format(why)

    # ALSO before the positive signals, and for the same structural reason:
    # _ORDER_TITLE matches the bare word "order", so "Action(s) taken or orders
    # passed" and "Order passed by SEBI" were being read as order wins and
    # opened. A regulatory order is not a commercial one, and mistaking the two
    # inverts the signal instead of merely adding noise.
    if _REGULATORY_ORDER.search(title):
        return False, "regulatory action, not an order win ({})".format(
            title.strip()[:70])

    # A positive signal always wins, even if a never-relevant pattern also
    # matches somewhere in the same caption.
    if _RESULTS_TITLE.search(title) or _ORDER_TITLE.search(title):
        return True, "title indicates results or an order win"

    if _NEVER_RELEVANT_TITLE.search(title):
        return False, "routine compliance filing, not opened ({})".format(
            title.strip()[:80])

    return True, "title inconclusive - reading the document"


def should_analyze(title: str, text: str) -> tuple:
    """
    Decide whether this filing is worth a model call.

    Returns (should_analyze: bool, reason: str). The reason is stored on the
    filing_analyses row either way, so every decision is explainable.
    """
    title = title or ""
    text = text or ""

    if not text.strip():
        return False, "no extractable text (scanned PDF, OCR recovered nothing)"

    # Checked FIRST, and even when the prefilter is disabled: a presentation
    # repeating last quarter's numbers passes every results test below, so this
    # is the only thing standing between it and a duplicate alert on stale data.
    restated, why = restates_old_results(title, text)
    if restated:
        return False, why

    if not config.PREFILTER_ENABLED:
        return True, "prefilter disabled"

    # 1. Positive title signals win immediately.
    if _RESULTS_TITLE.search(title):
        return True, "title indicates financial results"
    if _ORDER_TITLE.search(title):
        return True, "title indicates an order win"

    # 2. Body text — a real results statement, whatever the title said.
    if looks_like_financial_results(text):
        return True, "body contains a results statement ({} metric terms)".format(
            count_metric_terms(text)
        )

    # 3. Body text — an order win described in money terms.
    if _ORDER_BODY.search(text) or _ORDER_AMOUNT.search(text):
        return True, "body describes an order win with a value"

    # 4. Only now does a known-irrelevant title get to end it, so the checks
    #    above always had their chance first.
    if _NEVER_RELEVANT_TITLE.search(title):
        return False, "routine compliance filing ({})".format(title.strip()[:80])

    return False, "no results statement and no order win found"

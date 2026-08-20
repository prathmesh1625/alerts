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

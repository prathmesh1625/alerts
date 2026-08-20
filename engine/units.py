"""
units.py — turning a printed figure into crore, deterministically.

Indian filings print the denomination once, in the table header ("Rs. in
Lakhs"), never beside the figures. Getting it wrong rescales every number by
100x, and the model gets it wrong: asked to convert 45,231.00 lakh to crore,
gpt-4o-mini returned 45.231 rather than 452.31 — a 10x error, on a prompt that
spelled the division out. bot/output.py hit the same class of bug in production
from the other direction (a bank PAT of Rs 1,07,496 lakh shipped as
"Rs 1,07,496 Cr") and reached the same conclusion its `detect_document_scale`
encodes: the document's own heading is far more reliable than anything the
model says about units.

So the model is asked only to READ — the number exactly as printed, and the
denomination it believes applies — and every conversion happens here.

Why the 10x error mattered less than it looks, and still had to be fixed: a
growth rate divides two figures of the same scale, so rules 1 and 2 were
immune. Rule 3 compares an ABSOLUTE order value against a crore threshold, so
a Rs 412 Cr win read as Rs 41 Cr scores far below what it should.
"""
import re

# Multiplier from one unit to CRORE.
TO_CRORE = {
    "rupee": 1e-7,      # 1 crore = 10,000,000 rupees
    "thousand": 1e-4,
    "lakh": 0.01,       # 100 lakh = 1 crore
    "million": 0.1,     # 10 million = 1 crore
    "crore": 1.0,
    "billion": 100.0,
}

# Everything a filing might call each denomination.
_ALIASES = {
    "rs": "rupee", "rupee": "rupee", "rupees": "rupee", "inr": "rupee",
    "absolute": "rupee", "unit": "rupee", "units": "rupee",
    "thousand": "thousand", "thousands": "thousand", "k": "thousand",
    "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "lacs": "lakh",
    "crore": "crore", "crores": "crore", "cr": "crore", "crs": "crore",
    "million": "million", "millions": "million", "mn": "million", "mns": "million",
    "billion": "billion", "billions": "billion", "bn": "billion", "bns": "billion",
}

# "(Rs. in Lakhs)", "Amounts are in INR Crores", "Figures in Mn" ...
# Same shape as bot/output.py's _DOC_SCALE_RE.
_DOC_SCALE_RE = re.compile(
    r"(?:rs\.?|inr|₹|amount|amounts|figures)?\s*(?:are\s+)?in\s+"
    r"(?:(?:rs\.?|inr|₹)\s*)?"
    r"(?:(?P<long>lakhs?|lacs?|crores?|millions?|billions?|thousands?)[a-z]{0,2}"
    r"|(?P<short>crs?|mns?|bns?))\b",
    re.IGNORECASE,
)

# Where the results table is, so we can pick the heading printed NEAREST it.
_METRIC_KEYWORDS = (
    "revenue from operations", "total income", "profit before tax",
    "profit after tax", "net profit", "total expenses",
    "earnings per share", "ebitda", "operating profit",
)


def normalise_unit(unit):
    """Map whatever the model (or a filing) called a unit onto our vocabulary."""
    if not unit:
        return None
    key = str(unit).strip().lower().rstrip(".")
    key = re.sub(r"^(?:rs\.?|inr|₹)\s*", "", key)
    key = re.sub(r"^in\s+", "", key)
    return _ALIASES.get(key)


def to_crore(value, unit):
    """
    Convert a printed figure to crore. None if either input is unusable.

    `value` is the number exactly as printed (45231.0 for "45,231.00"), and
    `unit` the denomination that applies to it.
    """
    if value is None:
        return None
    canonical = normalise_unit(unit)
    if canonical is None:
        return None
    try:
        return round(float(value) * TO_CRORE[canonical], 4)
    except (TypeError, ValueError):
        return None


def detect_statement_unit(text):
    """
    The denomination the results table is printed in, read from the document's
    own heading — or None if it never states one.

    When a filing states several (a P&L "in Crore" plus notes "in lakhs"), the
    heading printed NEAREST the results table wins. A majority vote gets this
    wrong: bot/output.py records a crore-denominated P&L with two lakhs notes
    being voted "lakh", which would have divided every correct figure by 100.
    """
    if not text:
        return None

    matches = []
    for m in _DOC_SCALE_RE.finditer(text):
        raw = m.group("long") or m.group("short") or ""
        canonical = normalise_unit(raw)
        if canonical:
            matches.append((m.start(), canonical))
    if not matches:
        return None

    low = text.lower()
    anchors = [low.find(k) for k in _METRIC_KEYWORDS if k in low]
    if anchors:
        anchor = min(anchors)
        return min(matches, key=lambda sm: abs(sm[0] - anchor))[1]

    counts = {}
    for _, scale in matches:
        counts[scale] = counts.get(scale, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


# "Rs. 412.50 crore", "₹1,234 Cr" — a value that names its own denomination
# right after the digits, which is how order wins are usually written.
_INLINE_VALUE_RE = re.compile(
    r"(?:rs\.?|inr|₹)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>lakhs?|lacs?|crores?|crs?|millions?|mns?|billions?|bns?)\b",
    re.IGNORECASE,
)

# "INR 720,00,00,000", "Rs. 4,12,50,000" — a currency-prefixed amount with NO
# unit word after it. On an Indian filing that means ABSOLUTE RUPEES.
#
# This case cost a real misread: Cyient's filing said
#   "INR 720,00,00,000 (Indian Rupees Seven Hundred Twenty crores only)"
# and gpt-4o-mini returned raw_value 7200000000 labelled "crore" — off by 10^7.
# The rule above could not correct it because the denomination is spelled in
# WORDS inside a parenthesis, not in digits+unit form.
_BARE_RUPEE_RE = re.compile(
    r"(?:rs\.?|inr|₹)\s*"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d{5,}(?:\.\d+)?)"
    r"(?!\s*(?:lakhs?|lacs?|crores?|crs?|millions?|mns?|billions?|bns?)\b)",
    re.IGNORECASE,
)

# No single Indian corporate order has ever approached this. Anything above it
# is a unit error, not a record-breaking contract — see reinterpret_if_absurd.
MAX_PLAUSIBLE_CR = 500_000.0     # Rs 5 lakh crore


def parse_inline_value_cr(text):
    """
    The first self-denominated money figure in a string, in crore, or None.

    Used as a CROSS-CHECK on order values: an order filing states the value in
    the sentence itself, so we can verify the model's reading against the
    document's own words rather than trusting it.

    A digits+unit form wins; failing that, a currency-prefixed bare amount is
    read as absolute rupees.
    """
    if not text:
        return None

    m = _INLINE_VALUE_RE.search(text)
    if m:
        try:
            return to_crore(float(m.group("num").replace(",", "")), m.group("unit"))
        except ValueError:
            pass

    m = _BARE_RUPEE_RE.search(text)
    if m:
        try:
            return to_crore(float(m.group("num").replace(",", "")), "rupee")
        except ValueError:
            return None
    return None


def reinterpret_if_absurd(value_cr):
    """
    Rescue a figure whose unit was obviously misread.

    A value in the billions-of-crores can only mean the model took an absolute
    RUPEE amount and labelled it crore. Reinterpreting the same digits as rupees
    recovers the real figure (7,200,000,000 "crore" -> Rs 720 Cr, which is what
    the Cyient filing actually said).

    Returns the corrected value, or None when no sane reading exists — better to
    score nothing than to publish a Rs 7-billion-crore order on the dashboard.
    """
    if value_cr is None or abs(value_cr) <= MAX_PLAUSIBLE_CR:
        return value_cr

    # The stated number, had it been rupees all along.
    as_rupees = value_cr * TO_CRORE["rupee"] / TO_CRORE["crore"]
    if abs(as_rupees) <= MAX_PLAUSIBLE_CR:
        return round(as_rupees, 4)
    return None

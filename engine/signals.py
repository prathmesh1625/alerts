"""
signals.py — the structured shape we ask gpt-4o-mini to fill in for one filing.

Deliberately narrower than the bot's `FinancialSummary`: we only need the three
inputs the formula consumes (PAT growth, revenue growth, order value) plus
enough provenance to justify an alert on the dashboard.

The division of labour matters more than the shape. The model READS — a number
exactly as printed, and the denomination it thinks applies. Python does every
piece of arithmetic: unit conversion (units.py) and growth (below). That split
is not stylistic; both halves of it were put here by observed failures:

  * asked to convert 45,231.00 lakh to crore, gpt-4o-mini returned 45.231
    instead of 452.31 — a 10x error on an explicit instruction;
  * bot/output.py carries `recompute_changes` for the same reason on growth.

Models read tables well and do sums badly. So they no longer do the sums.
"""
import re
from typing import List, Optional

from pydantic import BaseModel, Field

from units import (
    detect_statement_unit,
    parse_inline_value_cr,
    reinterpret_if_absurd,
    to_crore,
)

_UNIT_DESCRIPTION = (
    "The denomination this figure is printed in, taken from the statement's "
    "own header line (e.g. '(Rs. in Lakhs)'). One of: 'lakh', 'crore', "
    "'million', 'billion', 'thousand', 'rupee'. Use 'rupee' only when the "
    "figures are in absolute rupees with no denomination stated."
)


class PeriodFigure(BaseModel):
    """One metric for one reporting period, exactly as printed."""
    period_label: str = Field(
        default="",
        description="The period as printed, e.g. 'Q2 FY26', 'Quarter ended 30 Sep 2025'",
    )
    raw_value: Optional[float] = Field(
        default=None,
        description=(
            "The number EXACTLY as printed in the table, with thousands "
            "separators removed and nothing else changed. For '45,231.00' "
            "return 45231.00. DO NOT convert between units — that is done "
            "downstream. A figure in brackets is negative: '(45.20)' is -45.20. "
            "Null if the figure is not in the document."
        ),
    )
    unit: str = Field(default="", description=_UNIT_DESCRIPTION)
    raw: str = Field(
        default="",
        description="The figure as printed, including its unit, e.g. '45,231.00 (Rs. in Lakhs)'",
    )


class MetricYoY(BaseModel):
    """A metric with its year-ago comparative — the pair the formula needs."""
    current: Optional[PeriodFigure] = Field(
        default=None, description="The latest reported period in this filing"
    )
    year_ago: Optional[PeriodFigure] = Field(
        default=None,
        description=(
            "The SAME period one year earlier (the 'corresponding quarter of "
            "the previous year' column). Not the previous quarter."
        ),
    )
    growth_pct: Optional[float] = Field(
        default=None,
        description=(
            "Year-over-year growth in percent, if the filing STATES one. Leave "
            "null if it does not — it is recomputed from the figures above."
        ),
    )


class OrderWin(BaseModel):
    """A single new order / contract / LoA announced in the filing."""
    raw_value: Optional[float] = Field(
        default=None,
        description=(
            "The order value EXACTLY as printed, separators removed, no unit "
            "conversion. For 'Rs. 412.50 crore' return 412.50. Null if the "
            "filing announces an order without disclosing its value."
        ),
    )
    unit: str = Field(
        default="",
        description=(
            "The denomination the order value is printed in - usually stated "
            "inline, e.g. 'crore' for 'Rs. 412.50 crore'. One of: 'lakh', "
            "'crore', 'million', 'billion', 'thousand', 'rupee'."
        ),
    )
    customer: str = Field(default="", description="Who placed the order")
    scope: str = Field(default="", description="One short line on what the order is for")
    quote: str = Field(
        default="",
        description="The sentence from the filing that states this order and its value",
    )


class FilingSignals(BaseModel):
    """Everything the formula needs from one PDF."""

    company_name: str = Field(default="", description="Full company name as printed")

    document_type: str = Field(
        default="OTHER",
        description=(
            "One of: RESULTS (quarterly/annual financial results being "
            "reported for the first time), ORDER_WIN (bagging of a new "
            "order/contract/LoA/work order), BOTH, PRESENTATION (an investor "
            "or earnings slide deck), TRANSCRIPT (an earnings-call or analyst "
            "meet transcript), or OTHER (anything else - governance, AGM "
            "notices, shareholding patterns, credit ratings). "
            "PRESENTATION and TRANSCRIPT matter: they repeat figures already "
            "published elsewhere, so label them as such even though they "
            "contain a real results table."
        ),
    )

    reporting_period: str = Field(
        default="", description="The period these results cover, e.g. 'Q2 FY26'"
    )

    basis: str = Field(
        default="",
        description=(
            "'consolidated' if the figures were taken from the consolidated "
            "(group) statement, 'standalone' if the filing has only a "
            "standalone one. Empty if not determinable."
        ),
    )

    statement_unit: str = Field(
        default="",
        description=(
            "The denomination printed in the results table's own header line, "
            "e.g. 'lakh' for '(Rs. in Lakhs)'. This applies to every figure in "
            "the statement. One of: 'lakh', 'crore', 'million', 'billion', "
            "'thousand', 'rupee'."
        ),
    )

    revenue: Optional[MetricYoY] = Field(
        default=None,
        description=(
            "Revenue from Operations. Prefer this over 'Total Income', which "
            "includes other income. Null for a non-results filing."
        ),
    )

    profit: Optional[MetricYoY] = Field(
        default=None,
        description=(
            "Profit After Tax (PAT) / Net Profit for the period. Use profit "
            "AFTER tax, not PBT. Null for a non-results filing."
        ),
    )

    orders: List[OrderWin] = Field(
        default_factory=list,
        description=(
            "Every NEW order won that the filing announces. Empty list if the "
            "filing announces no new order. Do NOT include order-book totals, "
            "pipeline figures, or orders merely referred to from the past."
        ),
    )

    evidence: List[str] = Field(
        default_factory=list,
        description=(
            "2-4 short verbatim quotes from the document backing the figures "
            "above, so a human can check the alert without opening the PDF."
        ),
    )

    notes: str = Field(
        default="",
        description="One line on anything ambiguous, e.g. a restated comparative",
    )


# -----------------------------------------------------------------------------
#  Conversion — Python's job, not the model's
# -----------------------------------------------------------------------------

def figure_cr(figure: Optional[PeriodFigure], fallback_unit: Optional[str] = None):
    """
    A period figure in crore.

    The unit is resolved best-evidence-first: the figure's own, then the
    statement header the caller detected from the document text. That order
    matters — see `resolve_statement_unit`, which is what actually protects us
    from the model mislabelling a denomination.
    """
    if figure is None:
        return None
    return to_crore(figure.raw_value, figure.unit or fallback_unit)


def resolve_statement_unit(signals: "FilingSignals", pdf_text: str = "") -> Optional[str]:
    """
    The denomination to trust for this filing's statement figures.

    The DOCUMENT'S OWN HEADING WINS over the model. That is the whole lesson of
    bot/output.py's `detect_document_scale`: a heading sitting directly above
    the table is far more reliable than a model's recollection of it, and the
    failure mode when the model is wrong is a silent 100x error in the figures
    the dashboard shows.

    Falls back to what the model reported when the document states no
    denomination at all (common on one-page order-win letters, where the value
    is written inline instead).
    """
    from units import normalise_unit

    detected = detect_statement_unit(pdf_text)
    if detected:
        return detected
    return normalise_unit(signals.statement_unit)


def yoy_growth(metric: Optional[MetricYoY], unit: Optional[str] = None):
    """
    Year-over-year growth for a metric, recomputed from the two period figures.

    We do the arithmetic ourselves rather than trusting the model's
    `growth_pct`, for the same reason bot/output.py has `recompute_changes`:
    LLMs are reliable at pulling a number out of a table and unreliable at
    dividing two of them. The model's own figure is only a fallback for when
    the filing prints a growth percentage without the underlying comparative.

    Note this is scale-invariant — both figures share a denomination, so growth
    is right even if the unit is wrong. That is exactly why the unit bug hid
    behind correct-looking percentages until an order value exposed it.

    Returns None when growth is not meaningfully defined — notably when the
    year-ago figure is zero or negative, where a percentage would be nonsense
    (a swing from a -10 Cr loss to +5 Cr profit is not "+150% growth").
    """
    if metric is None:
        return None

    cur = figure_cr(metric.current, unit)
    prior = figure_cr(metric.year_ago, unit)

    if cur is not None and prior is not None and prior > 0:
        return round((cur - prior) / prior * 100.0, 2)

    return metric.growth_pct


def turnaround(metric: Optional[MetricYoY], unit: Optional[str] = None) -> bool:
    """
    True when the company went from a loss (or zero) a year ago to a profit now.

    `yoy_growth` refuses to put a percentage on this, but it is exactly the kind
    of result that moves a stock — so the scorer surfaces it as its own note
    rather than letting it vanish.
    """
    if metric is None:
        return False
    cur = figure_cr(metric.current, unit)
    prior = figure_cr(metric.year_ago, unit)
    return cur is not None and prior is not None and prior <= 0 < cur


# Corporate actions that carry a large rupee figure but are NOT orders won.
#
# gpt-4o-mini classified a Cyient SHARE BUYBACK as an ORDER_WIN on a real
# filing — the document type is wrong, but the money is real and large, so
# without this the buyback would have scored the maximum on rule 3. These
# announcements are common and every one of them names a big number, so the
# prompt alone is not a safe place to handle it.
_NOT_AN_ORDER_RE = re.compile(
    r"buy\s?-?back|repurchase of (?:equity )?shares|"
    r"\bdividend\b|rights issue|bonus issue|stock split|sub-?division of shares|"
    r"qualified institutions? placement|\bqips?\b|preferential (?:allotment|issue)|"
    r"debentures?|\bncds?\b|commercial paper|fund\s?rais|"
    r"employee stock options?|\besops?\b|"
    r"scheme of (?:arrangement|amalgamation)|merger|demerger|"
    r"capital expenditure|\bcapex\b|"
    r"acquisition of (?:\d+|a |the )?.{0,40}(?:stake|shares|equity)|"
    # A REGULATORY order is not an order win, and getting this wrong inverts
    # the signal rather than merely adding noise: an enforcement action reads
    # as new business. Observed live - ICICIPRULI's "Action(s) taken or orders
    # passed" was reported as "Order win Rs 364.73 Cr".
    #
    # Only unambiguous enforcement language. A bare "gst" or "income tax" is
    # far too broad: order values are routinely quoted "excluding GST", so that
    # rejected a genuine Rs 412.50 Cr win (caught by
    # test_quote_overrides_a_misread_value).
    r"penalt|adjudicat|show cause|demand notice|"
    r"orders? passed by|assessment order|"
    r"(?:gst|income[\s\-]tax|tax)\s+(?:department|authorit|demand|notice|officer)|"
    r"prosecution|search and seizure",
    re.IGNORECASE,
)


def is_real_order(order: OrderWin) -> bool:
    """
    False when this 'order' is actually some other corporate action.

    Checked against the order's own scope and quote — the document's words,
    not the model's label for them.
    """
    blob = "{} {}".format(order.scope or "", order.quote or "")
    return not _NOT_AN_ORDER_RE.search(blob)


def order_value_cr(order: OrderWin):
    """
    One order's value in crore, cross-checked against its own quote.

    Order announcements state the value inline ("the value of the order is
    Rs. 412.50 crore"), so unlike a statement figure we can verify the model's
    reading directly against the sentence it came from. When the two disagree
    the QUOTE wins — it is the document's own words, where the structured
    fields are the model's interpretation of them.
    """
    if not is_real_order(order):
        return None

    parsed = reinterpret_if_absurd(to_crore(order.raw_value, order.unit))
    from_quote = parse_inline_value_cr(order.quote)

    if parsed is None:
        return from_quote
    if from_quote is None:
        return parsed

    # Agreement within 1% is just rounding; a wider gap means the model
    # misread the figure or its unit, and the text is the better authority.
    if parsed and abs(parsed - from_quote) / max(abs(parsed), 1e-9) > 0.01:
        return from_quote
    return parsed


def total_order_value_cr(signals: FilingSignals):
    """Sum of every disclosed order value in the filing, or None if none were."""
    values = [order_value_cr(o) for o in (signals.orders or [])]
    values = [v for v in values if v is not None]
    return round(sum(values), 2) if values else None


def real_orders(signals: FilingSignals):
    """The orders that survived the corporate-action filter."""
    return [o for o in (signals.orders or []) if is_real_order(o)]

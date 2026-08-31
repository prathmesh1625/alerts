"""
test_units.py — the conversion that gpt-4o-mini got wrong.

Asked to convert 45,231.00 lakh to crore, the model returned 45.231 instead of
452.31 — a 10x error, on a prompt that spelled the division out. The model now
only reads; everything below is the code that replaced its arithmetic, so it is
the code that has to be right.

Run: pytest test_units.py   or   python test_units.py
"""
import config
import units
from signals import FilingSignals, MetricYoY, OrderWin, PeriodFigure, order_value_cr, \
    resolve_statement_unit, total_order_value_cr, yoy_growth
from units import (detect_statement_unit, normalise_unit, parse_inline_value_cr,
                   reinterpret_if_absurd, to_crore)


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) < tol


# -----------------------------------------------------------------------------
#  to_crore — the conversion itself
# -----------------------------------------------------------------------------

def test_lakh_to_crore_is_divide_by_one_hundred():
    # The exact case the model got wrong: 45,231 lakh is 452.31 Cr, NOT 45.231.
    assert close(to_crore(45231.0, "lakh"), 452.31)
    assert close(to_crore(5180.0, "lakh"), 51.80)


def test_every_denomination():
    assert close(to_crore(100.0, "crore"), 100.0)
    assert close(to_crore(100.0, "lakh"), 1.0)        # 100 lakh = 1 Cr
    assert close(to_crore(10.0, "million"), 1.0)      # 10 mn   = 1 Cr
    assert close(to_crore(1.0, "billion"), 100.0)     # 1 bn    = 100 Cr
    assert close(to_crore(10000.0, "thousand"), 1.0)
    assert close(to_crore(10_000_000.0, "rupee"), 1.0)


def test_negative_figures_survive():
    # A loss in brackets, "(45.20)", reaches us as -45.20 and must stay negative.
    assert close(to_crore(-4520.0, "lakh"), -45.20)


def test_unit_aliases_filings_actually_use():
    for alias in ("Lakhs", "lacs", "LAKH", "Rs. in Lakhs", "in lakhs"):
        assert normalise_unit(alias) == "lakh", alias
    for alias in ("Crores", "Cr", "crs", "CRORE"):
        assert normalise_unit(alias) == "crore", alias
    for alias in ("Mn", "millions"):
        assert normalise_unit(alias) == "million", alias


def test_unknown_or_missing_unit_yields_nothing():
    assert to_crore(100.0, "") is None
    assert to_crore(100.0, "bananas") is None
    assert to_crore(None, "crore") is None


# -----------------------------------------------------------------------------
#  detect_statement_unit — the document's own heading
# -----------------------------------------------------------------------------

def test_reads_the_header_line():
    for header, expected in [
        ("(Rs. in Lakhs)", "lakh"),
        ("(Rs. in Crores)", "crore"),
        ("(₹ in Millions)", "million"),
        ("Amounts are in INR Crore", "crore"),
        ("Figures in Mn", "million"),
    ]:
        text = "Particulars {}\nRevenue from operations 45,231.00".format(header)
        assert detect_statement_unit(text) == expected, header


def test_no_denomination_stated_returns_none():
    assert detect_statement_unit("Revenue from operations 45,231.00") is None
    assert detect_statement_unit("") is None


def test_heading_nearest_the_results_table_wins():
    """
    A filing that states several denominations: the P&L in crore, plus a
    shareholding note in lakhs. A majority vote would pick lakh and divide every
    correct figure by 100 — bot/output.py records exactly that regression.
    """
    text = (
        "STATEMENT OF FINANCIAL RESULTS (Rs. in Crore)\n"
        "Revenue from operations 452.31\n"
        "Profit after tax 51.80\n"
        "\n"
        "Note 4: Shareholding pattern, figures in lakhs\n"
        "Note 7: Employee stock options, figures in lakhs\n"
    )
    assert detect_statement_unit(text) == "crore"


def test_document_heading_beats_the_model():
    """The whole point: when the two disagree, the document wins."""
    signals = FilingSignals(statement_unit="crore")  # model says crore...
    text = "Particulars (Rs. in Lakhs)\nRevenue from operations 45,231.00"
    assert resolve_statement_unit(signals, text) == "lakh"  # ...document says lakh


def test_model_unit_used_only_when_document_states_none():
    signals = FilingSignals(statement_unit="crore")
    assert resolve_statement_unit(signals, "no denomination anywhere here") == "crore"


# -----------------------------------------------------------------------------
#  End-to-end through the metric helpers
# -----------------------------------------------------------------------------

def lakh_metric(cur, prior):
    return MetricYoY(
        current=PeriodFigure(raw_value=cur, unit="lakh"),
        year_ago=PeriodFigure(raw_value=prior, unit="lakh"),
    )


def test_growth_is_scale_invariant():
    """
    Why the 10x bug hid: growth divides two figures of the same scale, so it is
    right even when the unit is wrong. That is exactly why it had to be caught
    by an absolute value instead.
    """
    in_lakh = yoy_growth(lakh_metric(45231.0, 29870.0))
    in_crore = yoy_growth(MetricYoY(
        current=PeriodFigure(raw_value=452.31, unit="crore"),
        year_ago=PeriodFigure(raw_value=298.70, unit="crore"),
    ))
    assert close(in_lakh, in_crore, tol=0.01)
    assert close(in_lakh, 51.43, tol=0.01)


def test_statement_unit_fills_in_for_a_figure_that_lacks_one():
    m = MetricYoY(
        current=PeriodFigure(raw_value=45231.0, unit=""),
        year_ago=PeriodFigure(raw_value=29870.0, unit=""),
    )
    assert yoy_growth(m, "lakh") is not None


# -----------------------------------------------------------------------------
#  Order values — where the bug actually bit
# -----------------------------------------------------------------------------

def test_order_value_in_crore():
    o = OrderWin(raw_value=412.50, unit="crore", scope="supply of equipment",
                 quote="The value of the order is Rs. 412.50 crore")
    assert close(order_value_cr(o), 412.50)


def test_order_value_stated_in_lakh():
    o = OrderWin(raw_value=41250.0, unit="lakh", scope="supply of equipment",
                 quote="The order value is Rs. 41,250 lakh")
    assert close(order_value_cr(o), 412.50)


def test_quote_overrides_a_misread_value():
    """
    Order filings state the value inline, so the sentence is a check on the
    model. When they disagree, the document's own words win.
    """
    o = OrderWin(raw_value=41.25, unit="crore", scope="supply of equipment",
                 quote="The value of the order is Rs. 412.50 crore (excluding GST)")
    assert close(order_value_cr(o), 412.50)


def test_quote_agreeing_within_rounding_is_left_alone():
    o = OrderWin(raw_value=412.50, unit="crore", scope="supply of equipment",
                 quote="The value of the order is Rs. 412.50 crore")
    assert close(order_value_cr(o), 412.50)


def test_value_survives_an_unparseable_quote():
    o = OrderWin(raw_value=412.50, unit="crore", scope="supply of equipment",
                 quote="the order was received today")
    assert close(order_value_cr(o), 412.50)


def test_value_recovered_from_quote_when_fields_are_empty():
    o = OrderWin(raw_value=None, unit="", scope="supply of equipment",
                 quote="an order valued at Rs. 87.20 crore was received")
    assert close(order_value_cr(o), 87.20)


def test_orders_are_summed_in_crore():
    s = FilingSignals(orders=[
        OrderWin(raw_value=412.50, unit="crore", scope="supply order",
                 quote="order of Rs. 412.50 crore"),
        OrderWin(raw_value=8720.0, unit="lakh", scope="supply order",
                 quote="order of Rs. 8,720 lakh"),
    ])
    assert close(total_order_value_cr(s), 499.70, tol=0.01)


# -----------------------------------------------------------------------------
#  Regressions from REAL filings (Cyient, 11 Jun 2026)
# -----------------------------------------------------------------------------

CYIENT_QUOTE = ("for an aggregate amount not exceeding INR 720,00,00,000 "
                "(Indian Rupees Seven Hundred Twenty crores only)")


def test_bare_rupee_amount_is_read_as_rupees():
    """
    "INR 720,00,00,000" with the denomination spelled in WORDS inside a
    parenthesis. The digits are absolute rupees = Rs 720 Cr. gpt-4o-mini
    returned 7200000000 labelled "crore" on this exact filing.
    """
    assert close(parse_inline_value_cr(CYIENT_QUOTE), 720.0)


def test_absurd_value_is_reinterpreted_as_rupees():
    # 7,200,000,000 "crore" is Rs 72 quadrillion. It can only mean rupees.
    assert close(reinterpret_if_absurd(7_200_000_000.0), 720.0)
    # A plausible figure is left completely alone.
    assert close(reinterpret_if_absurd(412.50), 412.50)
    assert close(reinterpret_if_absurd(50_000.0), 50_000.0)
    assert reinterpret_if_absurd(None) is None


def test_absurd_beyond_rescue_is_dropped_not_published():
    # No reading of this is sane; scoring nothing beats a nonsense headline.
    assert reinterpret_if_absurd(1e20) is None


def test_the_cyient_buyback_scores_nothing():
    """The end-to-end regression: a buyback must not become an order win."""
    buyback = OrderWin(
        raw_value=7_200_000_000.0, unit="crore",
        scope="buyback of equity shares", quote=CYIENT_QUOTE,
    )
    assert order_value_cr(buyback) is None
    assert total_order_value_cr(FilingSignals(orders=[buyback])) is None


def test_other_corporate_actions_are_not_orders():
    from signals import is_real_order
    for scope in ("buy-back of shares", "interim dividend declaration",
                  "qualified institutions placement", "issue of NCDs",
                  "rights issue of equity shares", "scheme of amalgamation",
                  "capital expenditure plan", "employee stock option grant"):
        assert not is_real_order(OrderWin(scope=scope)), scope


def test_a_regulatory_order_is_not_an_order_win():
    """
    Observed live: ICICIPRULI's "Action(s) taken or orders passed" — a penalty
    — was reported as "Order win Rs 364.73 Cr". That does not merely add noise,
    it points the wrong way: enforcement action rendered as new business.
    """
    from signals import is_real_order
    for scope, quote in [
        ("penalty imposed", "an order imposing a penalty of Rs. 364.73 crore"),
        ("tax demand", "a demand notice of Rs. 1.24 crore from the GST department"),
        ("assessment order", "an assessment order for Rs. 50 crore was received"),
        ("adjudication", "the adjudicating officer passed an order of Rs. 5 crore"),
    ]:
        o = OrderWin(raw_value=100.0, unit="crore", scope=scope, quote=quote)
        assert not is_real_order(o), scope


def test_excluding_gst_does_not_disqualify_a_real_order():
    """
    Order values are routinely quoted "excluding GST". A bare tax keyword in
    the exclusion list rejected a genuine Rs 412.50 Cr win.
    """
    from signals import is_real_order
    o = OrderWin(raw_value=412.50, unit="crore",
                 scope="supply of equipment",
                 quote="The value of the order is Rs. 412.50 crore (excluding GST)")
    assert is_real_order(o)
    assert close(order_value_cr(o), 412.50)


def test_a_genuine_order_still_passes_the_filter():
    from signals import is_real_order
    o = OrderWin(
        raw_value=412.50, unit="crore", customer="NTPC Renewable Energy Limited",
        scope="supply and installation of solar modules",
        quote="The value of the order is Rs. 412.50 crore",
    )
    assert is_real_order(o)
    assert close(order_value_cr(o), 412.50)


def test_inline_parser_handles_indian_grouping():
    assert close(parse_inline_value_cr("valued at Rs. 1,23,456.78 lakh"), 1234.5678, tol=1e-3)
    assert close(parse_inline_value_cr("₹6,240 crore order book"), 6240.0)
    assert parse_inline_value_cr("no money mentioned") is None


# -----------------------------------------------------------------------------

def test_a_bank_deposit_programme_is_not_an_order():
    """
    IDBI Bank's CARE Ratings filing was extracted as a "Certificate of deposit"
    order of Rs 35,000 crore, scoring the maximum. A bank's funding programme is
    money RAISED, not business won — the exact case this whitelist exists for.
    """
    from signals import is_real_order
    o = OrderWin(raw_value=35000.0, unit="crore", scope="Certificate of deposit",
                 quote="Certificate of deposit RBI 35,000.00 CARE A1+ Reaffirmed")
    assert not is_real_order(o)
    assert order_value_cr(o) is None


def test_borrowings_are_not_orders():
    from signals import is_real_order
    for scope, quote in [
        ("term loan", "secured a term loan of Rs. 250 crore from HDFC Bank"),
        ("bank facilities", "Long Term Bank Facilities Rs. 500.00 crore"),
        ("working capital", "sanction of working capital limits of Rs. 80 crore"),
        ("NCD issue", "issue of non-convertible debentures of Rs. 300 crore"),
        ("credit rating", "CARE reaffirmed the rating on facilities of Rs. 1,200 crore"),
    ]:
        o = OrderWin(raw_value=100.0, unit="crore", scope=scope, quote=quote)
        assert not is_real_order(o), scope


def test_an_order_needs_positive_evidence_not_just_a_number():
    """
    A blacklist alone cannot hold — the ways of not being an order are
    unbounded. The document has to SAY it won something.
    """
    from signals import is_real_order
    vague = OrderWin(raw_value=500.0, unit="crore", scope="general update",
                     quote="the aggregate amount is Rs. 500 crore")
    assert not is_real_order(vague)

    real = OrderWin(raw_value=500.0, unit="crore", scope="supply of pipes",
                    quote="received a work order valued at Rs. 500 crore")
    assert is_real_order(real)


# -----------------------------------------------------------------------------
#  Currency — a separate axis from denomination
#
#  STLTECH signed a supply contract worth "Approximately USD 288 Million".
#  Read as rupees that is Rs 28.8 Cr; it is really about Rs 2,500 Cr. No unit
#  handling could have caught it, because "million" was the correct unit — the
#  currency was simply not being read.
# -----------------------------------------------------------------------------

def test_usd_is_converted_to_rupees():
    inr = units.to_crore(288.0, "million", "INR")
    usd = units.to_crore(288.0, "million", "USD")
    assert abs(inr - 28.8) < 0.01
    assert usd > 2000, "USD 288 million must be thousands of crore, got {}".format(usd)
    assert abs(usd / inr - config.FX_RATES["USD"]) < 0.01


def test_a_missing_currency_still_means_rupees():
    """Every existing caller omits it, and every Indian filing is in rupees."""
    assert units.to_crore(412.5, "crore") == units.to_crore(412.5, "crore", "INR")
    assert units.to_crore(412.5, "crore", "") == 412.5
    assert units.to_crore(412.5, "crore", None) == 412.5
    assert units.to_crore(412.5, "crore", "banana") == 412.5


def test_currency_aliases():
    for alias in ("USD", "usd", "US$", "$", "dollars"):
        assert units.normalise_currency(alias) == "USD", alias
    for alias in ("INR", "Rs.", "₹", "rupees", "", None):
        assert units.normalise_currency(alias) == "INR", alias


def test_the_quote_parser_reads_the_currency_too():
    """
    The cross-check has to agree with the model, or it 'corrects' a right
    answer back to a wrong one.
    """
    assert units.parse_inline_value_cr("Approximately USD 288 Million") > 2000
    assert units.parse_inline_value_cr("a contract worth $45 million") > 300
    assert abs(units.parse_inline_value_cr("Rs. 412.50 crore") - 412.5) < 0.01
    assert abs(units.parse_inline_value_cr("INR 45,231.00 lakh") - 452.31) < 0.01


def test_the_currency_dict_does_not_shadow_the_unit_dict():
    """
    Caught in review: the currency aliases were first added under the name the
    UNIT aliases already used, which silently broke every conversion in the
    engine rather than only the currency ones.
    """
    assert units.normalise_unit("million") == "million"
    assert units.normalise_unit("lakhs") == "lakh"
    assert units.normalise_unit("crore") == "crore"
    assert units.normalise_unit("not a unit") is None


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

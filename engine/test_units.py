"""
test_units.py — the conversion that gpt-4o-mini got wrong.

Asked to convert 45,231.00 lakh to crore, the model returned 45.231 instead of
452.31 — a 10x error, on a prompt that spelled the division out. The model now
only reads; everything below is the code that replaced its arithmetic, so it is
the code that has to be right.

Run: pytest test_units.py   or   python test_units.py
"""
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
    o = OrderWin(raw_value=412.50, unit="crore",
                 quote="The value of the order is Rs. 412.50 crore")
    assert close(order_value_cr(o), 412.50)


def test_order_value_stated_in_lakh():
    o = OrderWin(raw_value=41250.0, unit="lakh",
                 quote="The order value is Rs. 41,250 lakh")
    assert close(order_value_cr(o), 412.50)


def test_quote_overrides_a_misread_value():
    """
    Order filings state the value inline, so the sentence is a check on the
    model. When they disagree, the document's own words win.
    """
    o = OrderWin(raw_value=41.25, unit="crore",       # model misread by 10x
                 quote="The value of the order is Rs. 412.50 crore (excluding GST)")
    assert close(order_value_cr(o), 412.50)


def test_quote_agreeing_within_rounding_is_left_alone():
    o = OrderWin(raw_value=412.50, unit="crore",
                 quote="The value of the order is Rs. 412.50 crore")
    assert close(order_value_cr(o), 412.50)


def test_value_survives_an_unparseable_quote():
    o = OrderWin(raw_value=412.50, unit="crore", quote="the order was received today")
    assert close(order_value_cr(o), 412.50)


def test_value_recovered_from_quote_when_fields_are_empty():
    o = OrderWin(raw_value=None, unit="",
                 quote="an order valued at Rs. 87.20 crore was received")
    assert close(order_value_cr(o), 87.20)


def test_orders_are_summed_in_crore():
    s = FilingSignals(orders=[
        OrderWin(raw_value=412.50, unit="crore", quote="Rs. 412.50 crore"),
        OrderWin(raw_value=8720.0, unit="lakh", quote="Rs. 8,720 lakh"),
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

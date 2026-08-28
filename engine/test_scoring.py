"""
test_scoring.py — the formula is the product, so it gets tests.

Pure functions only: no database, no OpenAI, no PDFs. Runs in well under a
second with either `pytest test_scoring.py` or `python test_scoring.py`.
"""
import config
import scoring

# Rules 1 and 2 are switched OFF in production while the order rule is being
# made precise. These tests exercise the rule LOGIC, which must stay correct
# for when they are switched back on, so they run with every rule enabled.
# test_only_the_enabled_rules_are_scored covers the flags themselves.
config.PROFIT_RULE_ENABLED = True
config.REVENUE_RULE_ENABLED = True
config.ORDER_RULE_ENABLED = True
# Bands are tuned for the order-only score range in production; restore the
# 0-100 ones so the banding assertions below mean what they say.
config.BAND_STRONG = 70.0
config.BAND_MODERATE = 45.0
from signals import FilingSignals, MetricYoY, OrderWin, PeriodFigure, yoy_growth


def metric(current, year_ago, growth=None):
    """Figures already in crore — the unit conversion is tested in test_units.py."""
    return MetricYoY(
        current=PeriodFigure(period_label="Q2 FY26", raw_value=current, unit="crore"),
        year_ago=PeriodFigure(period_label="Q2 FY25", raw_value=year_ago, unit="crore"),
        growth_pct=growth,
    )


def results(revenue=None, profit=None, orders=None, **kw):
    return FilingSignals(
        company_name="Test Ltd",
        document_type=kw.pop("document_type", "RESULTS"),
        reporting_period="Q2 FY26",
        basis="consolidated",
        revenue=revenue,
        profit=profit,
        orders=orders or [],
        **kw,
    )


# -----------------------------------------------------------------------------
#  Growth arithmetic
# -----------------------------------------------------------------------------

def test_growth_is_recomputed_not_trusted():
    # The model claims +5%, the figures say +100%. The figures win.
    m = metric(200.0, 100.0, growth=5.0)
    assert yoy_growth(m) == 100.0


def test_growth_falls_back_to_model_when_comparative_missing():
    m = MetricYoY(current=PeriodFigure(raw_value=200.0, unit="crore"),
                  year_ago=None, growth_pct=42.0)
    assert yoy_growth(m) == 42.0


def test_growth_undefined_against_a_loss():
    # -10 Cr to +5 Cr is not "+150% growth"; a percentage here is meaningless.
    assert yoy_growth(metric(5.0, -10.0)) is None


# -----------------------------------------------------------------------------
#  Rule 1 — profit
# -----------------------------------------------------------------------------

def test_profit_below_threshold_does_not_fire():
    r = scoring.score_filing(results(profit=metric(124.0, 100.0)))  # +24%
    assert "PROFIT_GROWTH" not in r["rules_hit"]
    assert r["qualifies"] is False


def test_profit_at_threshold_fires_with_base_credit():
    r = scoring.score_filing(results(profit=metric(125.0, 100.0)))  # exactly +25%
    assert "PROFIT_GROWTH" in r["rules_hit"]
    expected = config.PROFIT_WEIGHT * config.BASE_CREDIT
    assert abs(r["score"] - expected) < 0.01
    assert r["qualifies"] is True


def test_profit_blowout_earns_full_weight():
    r = scoring.score_filing(results(profit=metric(300.0, 100.0)))  # +200%, past full
    assert abs(r["score"] - config.PROFIT_WEIGHT) < 0.01


def test_loss_to_profit_turnaround_fires():
    r = scoring.score_filing(results(profit=metric(5.0, -10.0)))
    assert "PROFIT_GROWTH" in r["rules_hit"]
    rule = next(x for x in r["breakdown"]["rules"] if x["rule"] == "PROFIT_GROWTH")
    assert "Turnaround" in rule["note"]


# -----------------------------------------------------------------------------
#  Rule 2 — revenue
# -----------------------------------------------------------------------------

def test_revenue_needs_fifty_percent_not_twentyfive():
    below = scoring.score_filing(results(revenue=metric(140.0, 100.0)))  # +40%
    assert "REVENUE_GROWTH" not in below["rules_hit"]

    at = scoring.score_filing(results(revenue=metric(150.0, 100.0)))  # +50%
    assert "REVENUE_GROWTH" in at["rules_hit"]


# -----------------------------------------------------------------------------
#  Rule 3 — orders
# -----------------------------------------------------------------------------

def test_order_below_one_crore_does_not_fire():
    s = results(document_type="ORDER_WIN",
                orders=[OrderWin(unit="crore", raw_value=0.5, customer="NTPC", scope="supply order")])
    assert "ORDER_WIN" not in scoring.score_filing(s)["rules_hit"]


def test_order_of_one_crore_fires():
    s = results(document_type="ORDER_WIN",
                orders=[OrderWin(unit="crore", raw_value=1.0, customer="NTPC", scope="supply order")])
    r = scoring.score_filing(s)
    assert "ORDER_WIN" in r["rules_hit"]
    assert r["qualifies"] is True


def test_order_scoring_is_logarithmic():
    small = scoring.score_filing(results(orders=[OrderWin(unit="crore", raw_value=5.0, scope="supply of equipment",
                                 quote="received an order")]))["score"]
    mid = scoring.score_filing(results(orders=[OrderWin(unit="crore", raw_value=250.0, scope="supply of equipment",
                                 quote="received an order")]))["score"]
    big = scoring.score_filing(results(orders=[OrderWin(unit="crore", raw_value=5000.0, scope="supply of equipment",
                                 quote="received an order")]))["score"]
    assert small < mid < big
    assert abs(big - config.ORDER_WEIGHT) < 0.01  # past ORDER_FULL_CR, maxed


def test_multiple_orders_are_summed():
    s = results(orders=[OrderWin(unit="crore", raw_value=100.0, scope="supply of equipment",
                                 quote="received an order"), OrderWin(unit="crore", raw_value=150.0, scope="supply of equipment",
                                 quote="received an order")])
    r = scoring.score_filing(s)
    assert r["order_value_cr"] == 250.0
    rule = next(x for x in r["breakdown"]["rules"] if x["rule"] == "ORDER_WIN")
    assert "2 orders" in rule["note"]


def test_order_with_undisclosed_value_is_noted_but_not_scored():
    s = results(orders=[OrderWin(raw_value=None, unit="", customer="Indian Railways", scope="work order")])
    r = scoring.score_filing(s)
    assert "ORDER_WIN" not in r["rules_hit"]
    rule = next(x for x in r["breakdown"]["rules"] if x["rule"] == "ORDER_WIN")
    assert "not disclosed" in rule["note"]


# -----------------------------------------------------------------------------
#  Combination and banding
# -----------------------------------------------------------------------------

def test_all_three_rules_max_at_one_hundred():
    s = results(
        profit=metric(500.0, 100.0),
        revenue=metric(500.0, 100.0),
        orders=[OrderWin(unit="crore", raw_value=5000.0, scope="supply of equipment",
                                 quote="received an order")],
        document_type="BOTH",
    )
    r = scoring.score_filing(s)
    assert abs(r["score"] - 100.0) < 0.01
    assert r["conviction"] == "STRONG"
    assert len(r["rules_hit"]) == 3


def test_strong_results_filing_lands_moderate_or_better():
    r = scoring.score_filing(results(
        profit=metric(160.0, 100.0),   # +60%
        revenue=metric(180.0, 100.0),  # +80%
    ))
    assert r["conviction"] in ("MODERATE", "STRONG")
    assert r["score"] >= config.BAND_MODERATE


def test_nothing_qualifies_on_an_ordinary_filing():
    r = scoring.score_filing(results(
        profit=metric(105.0, 100.0),
        revenue=metric(108.0, 100.0),
    ))
    assert r["rules_hit"] == []
    assert r["qualifies"] is False
    assert r["headline"] == "No formula rule cleared"


def test_empty_signals_are_safe():
    r = scoring.score_filing(results())
    assert r["score"] == 0.0
    assert r["qualifies"] is False


def test_headline_names_every_rule_that_fired():
    r = scoring.score_filing(results(
        profit=metric(200.0, 100.0),
        revenue=metric(200.0, 100.0),
        orders=[OrderWin(unit="crore", raw_value=400.0, scope="supply of equipment",
                                 quote="received an order")],
    ))
    assert "PAT" in r["headline"]
    assert "Revenue" in r["headline"]
    assert "Order win" in r["headline"]
    assert "Q2 FY26" in r["headline"]


# -----------------------------------------------------------------------------

def test_only_the_enabled_rules_are_scored():
    """
    A disabled rule contributes nothing and does not appear in rules_hit, and
    max_possible follows the enabled set — so a score always reads against the
    rules actually in force.
    """
    strong = results(profit=metric(200.0, 100.0), revenue=metric(200.0, 100.0),
                     orders=[OrderWin(unit="crore", raw_value=400.0,
                                      scope="supply of equipment",
                                      quote="received an order of Rs. 400 crore")])
    saved = (config.PROFIT_RULE_ENABLED, config.REVENUE_RULE_ENABLED,
             config.ORDER_RULE_ENABLED)
    try:
        config.PROFIT_RULE_ENABLED = False
        config.REVENUE_RULE_ENABLED = False
        r = scoring.score_filing(strong)
        assert r["rules_hit"] == ["ORDER_WIN"], r["rules_hit"]
        assert r["breakdown"]["max_possible"] == config.ORDER_WEIGHT
        assert "PAT" not in r["headline"] and "Revenue" not in r["headline"]

        # A filing that ONLY has profit growth must now score nothing.
        only_profit = results(profit=metric(200.0, 100.0))
        r2 = scoring.score_filing(only_profit)
        assert r2["score"] == 0.0
        assert r2["qualifies"] is False
    finally:
        (config.PROFIT_RULE_ENABLED, config.REVENUE_RULE_ENABLED,
         config.ORDER_RULE_ENABLED) = saved


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

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


# -----------------------------------------------------------------------------
#  A terminated order is not a win
#
#  RPPINFRA announced the termination of a Rs 205.89 Cr SDAT work order and it
#  scored 27.94 STRONG as "Order win Rs 205.89 Cr". The order was real and the
#  extracted quote was accurate — "the value of the work order is Rs.
#  205,89,14,000/-" — so nothing about the ORDER was wrong. The company was
#  losing it. That makes this the inverted signal again, and worse than noise.
# -----------------------------------------------------------------------------

# Verbatim from the RPPINFRA filing.
TERMINATION_TEXT = """
Subject: Termination of work order - intimation pursuant to Regulation 30
The Company has received a communication from SDAT regarding termination of the
aforesaid work order, pursuant to the decision to restructure and redesign the
Global Sports City project. The value of the work order is Rs. 205,89,14,000/-.
"""

# The two traps. Both appear in real terminations AND in genuine wins, so
# neither may be treated as evidence of anything.
SEBI_ANNEXURE_ROW = (
    "4 Details of amendment or reasons for terminations and impact thereof "
    "(to the extent possible); Not applicable"
)
QUOTED_CLAUSE_NAMES = (
    'in exercise of the powers conferred under clause 55 "No Compensation for '
    'Cancellation / Reduction of Works", Clause 68.4- "Cancellation/'
    'Determination of Contract in Full or Part", Clause 71.0 "Force Majeure"'
)

GENUINE_WIN_TEXT = """
Subject: Receipt of work order
We wish to inform you that the Company has received a work order from NTPC
Limited for the design, engineering, procurement and construction of a
sub-station. The value of the work order is Rs. 205,89,14,000/-.
"""


def terminated_order(**kw):
    kw.setdefault("unit", "crore")
    kw.setdefault("raw_value", 205.89)
    kw.setdefault("customer", "Sports Development Authority of Tamil Nadu")
    kw.setdefault("scope", "Establishment of Global Sports City, Chennai - "
                           "design, Engineering, procurement and construction")
    kw.setdefault("quote", "the value of the work order is Rs. 205,89,14,000/-")
    return OrderWin(**kw)


def test_a_terminated_order_does_not_fire():
    s = results(document_type="OTHER",
                orders=[terminated_order(status="TERMINATED")])
    r = scoring.score_filing(s)
    assert r["rules_hit"] == [], r["rules_hit"]
    assert r["score"] == 0.0
    assert r["qualifies"] is False


def test_a_terminated_order_reports_no_value():
    """
    The dashboard column must not carry the value either. Showing
    "Rs 205.89 Cr" against a cancelled contract reads as a win at a glance,
    whatever the score says.
    """
    s = results(document_type="OTHER",
                orders=[terminated_order(status="TERMINATED")])
    assert scoring.score_filing(s)["order_value_cr"] is None


def test_a_termination_says_so_rather_than_shrugging():
    s = results(document_type="OTHER",
                orders=[terminated_order(status="TERMINATED")])
    note = scoring.score_filing(s)["breakdown"]["rules"][-1]["note"]
    assert "LOST" in note, note


def test_amended_and_completed_orders_do_not_fire():
    """Neither is new business won today."""
    for st in ("AMENDED", "COMPLETED"):
        r = scoring.score_filing(results(document_type="OTHER",
                                         orders=[terminated_order(status=st)]))
        assert r["rules_hit"] == [], (st, r["rules_hit"])


def test_the_document_catches_a_termination_the_model_missed():
    """
    The backstop. The model labels it NEW — every field it looked at points
    that way — and the document text alone has to stop it.
    """
    s = results(document_type="ORDER_WIN",
                orders=[terminated_order(status="NEW")])
    assert "ORDER_WIN" in scoring.score_filing(s)["rules_hit"], \
        "fixture must fire without the document text, or this proves nothing"

    r = scoring.score_filing(s, TERMINATION_TEXT)
    assert r["rules_hit"] == [], r["rules_hit"]
    assert r["order_value_cr"] is None


def test_missing_status_behaves_as_it_did_before_the_field_existed():
    """An omitted or junk status must not silently suppress a real win."""
    assert OrderWin(unit="crore", raw_value=5.0).status == "NEW"
    assert OrderWin(unit="crore", raw_value=5.0, status=None).status == "NEW"
    assert OrderWin(unit="crore", raw_value=5.0, status="banana").status == "NEW"
    assert OrderWin(unit="crore", raw_value=5.0, status="terminated").status \
        == "TERMINATED"


# --- the traps: these must NOT block a genuine win ---------------------------

def test_the_sebi_annexure_row_is_not_a_termination():
    """
    "Details of amendment or reasons for terminations" is printed on EVERY
    Reg 30 order disclosure, a genuine win included. Reading it as an event
    would silence the entire rule.
    """
    from signals import document_reports_order_loss
    assert document_reports_order_loss(GENUINE_WIN_TEXT + SEBI_ANNEXURE_ROW) is False


def test_quoted_contract_clause_names_are_not_a_termination():
    """
    Real EPC wins quote clause 55 "No Compensation for Cancellation" and
    Clause 68.4 "Cancellation/Determination of Contract" as a matter of course.
    """
    from signals import document_reports_order_loss
    assert document_reports_order_loss(GENUINE_WIN_TEXT + QUOTED_CLAUSE_NAMES) is False


def test_hypothetical_termination_language_is_not_a_termination():
    from signals import document_reports_order_loss
    for clause in (
        "The contract may be terminated by either party on 90 days notice.",
        "The customer reserves the right to terminate the contract for convenience.",
        "In the event of termination, the Company shall be compensated for work done.",
    ):
        assert document_reports_order_loss(GENUINE_WIN_TEXT + clause) is False, clause


def test_a_genuine_win_carrying_both_traps_still_scores():
    """The end-to-end version: full scoring, both traps present, must alert."""
    s = results(document_type="ORDER_WIN",
                orders=[OrderWin(unit="crore", raw_value=205.89, customer="NTPC",
                                 scope="design, engineering, procurement and construction",
                                 quote="the value of the work order is Rs. 205,89,14,000/-")])
    text = GENUINE_WIN_TEXT + SEBI_ANNEXURE_ROW + QUOTED_CLAUSE_NAMES
    r = scoring.score_filing(s, text)
    assert "ORDER_WIN" in r["rules_hit"], r["breakdown"]["rules"][-1]["note"]
    assert r["order_value_cr"] == 205.89


# -----------------------------------------------------------------------------
#  A services deal is still an order win
#
#  E2E Networks announced a Rs 1,000 Cr GPU deal and it scored 0.0. The order
#  was real, the value was right and document_type was ORDER_WIN — but the
#  whitelist read only the model's quote, which said "binding term sheet ...
#  for the provision of NVIDIA Blackwell cloud GPUs" and contains none of the
#  EPC vocabulary the whitelist was built from.
#
#  Worse, it was a coin flip: the same filing scored 30.0 when the model
#  happened to quote the sentence containing "contract value" instead.
# -----------------------------------------------------------------------------

E2E_ORDER = dict(
    raw_value=1000.0, unit="crore",
    customer="Sovereign AI company based in India",
    scope="NVIDIA Blackwell cloud GPUs and allied services",
    quote=("the Company has entered into the binding term sheet with a Sovereign "
           "AI company based in India requiring high-performance computing "
           "infrastructure, for the provision of NVIDIA Blackwell cloud GPUs and "
           "allied services."))
E2E_TITLE = "Bagging/Receiving of orders/contracts"


def test_a_services_deal_scores_as_an_order_win():
    s = results(document_type="ORDER_WIN", orders=[OrderWin(**E2E_ORDER)])
    r = scoring.score_filing(s, "", E2E_TITLE)
    assert "ORDER_WIN" in r["rules_hit"], r["breakdown"]["rules"][-1]["note"]
    assert r["order_value_cr"] == 1000.0
    assert r["qualifies"] is True


def test_the_document_type_alone_is_enough_evidence():
    """Even with no title, the model's reading of the whole document counts."""
    s = results(document_type="ORDER_WIN", orders=[OrderWin(**E2E_ORDER)])
    assert "ORDER_WIN" in scoring.score_filing(s, "", "")["rules_hit"]


def test_the_exchange_title_alone_is_enough_evidence():
    """
    NSE assigns "Bagging/Receiving of orders/contracts" itself. That is better
    evidence than any sentence the model chose to quote.
    """
    s = results(document_type="OTHER", orders=[OrderWin(**E2E_ORDER)])
    assert "ORDER_WIN" in scoring.score_filing(s, "", E2E_TITLE)["rules_hit"]


def test_the_verdict_does_not_depend_on_which_sentence_was_quoted():
    """
    The actual defect. Two quotes from the SAME filing, one naming the value
    and one describing the work, must reach the same verdict.
    """
    with_value = OrderWin(**dict(E2E_ORDER, quote=(
        "The arrangement has an aggregate contract value of approximately "
        "Rs. 1,000 crore (exclusive of applicable taxes).")))
    without = OrderWin(**E2E_ORDER)
    a = scoring.score_filing(results(document_type="ORDER_WIN", orders=[with_value]),
                             "", E2E_TITLE)
    b = scoring.score_filing(results(document_type="ORDER_WIN", orders=[without]),
                             "", E2E_TITLE)
    assert a["rules_hit"] == b["rules_hit"], (a["rules_hit"], b["rules_hit"])
    assert a["score"] == b["score"], (a["score"], b["score"])


# -----------------------------------------------------------------------------
#  A share sale is not an order win
#
#  HAPPSTMNDS' promoters sold 22.106% of the company to ITC Infotech for an
#  "aggregate consideration" of Rs 1,329.72 Cr, and it alerted STRONG as an
#  order win. No business was won — for a shareholder that is a change of
#  control. It got through because the model labelled the document BOTH, and
#  BOTH had just been made sufficient evidence on its own.
# -----------------------------------------------------------------------------

SPA_QUOTE = ("for sale of 3,36,61,700 equity shares of Happiest Minds "
             "Technologies Limited held by them, representing 22.106% of the "
             "paid-up equity share capital of the Company, to the Purchaser for "
             "an aggregate consideration of INR 13,29,71,77,710")


def spa_order():
    return OrderWin(raw_value=1329.72, unit="crore",
                    customer="ITC Infotech India Limited",
                    scope="sale of equity shares to ITC Infotech India Limited",
                    quote=SPA_QUOTE)


def test_a_promoter_share_sale_is_not_an_order_win():
    r = scoring.score_filing(results(document_type="BOTH", orders=[spa_order()]))
    assert r["rules_hit"] == [], r["breakdown"]["rules"][-1]["note"]
    assert r["order_value_cr"] is None
    assert r["qualifies"] is False


def test_a_share_sale_is_rejected_under_every_favourable_label():
    """It must not depend on which label the model happened to choose."""
    for dt in ("BOTH", "ORDER_WIN", "OTHER"):
        r = scoring.score_filing(results(document_type=dt, orders=[spa_order()]),
                                 "", "Bagging/Receiving of orders/contracts")
        assert r["rules_hit"] == [], "{}: {}".format(
            dt, r["breakdown"]["rules"][-1]["note"])


def test_BOTH_is_not_positive_evidence_on_its_own():
    """
    BOTH is what the model reaches for when a document has figures and
    something else going on, which describes most of the misreads. An order
    with no order language in it must not qualify on that label alone.
    """
    vague = OrderWin(raw_value=500.0, unit="crore", customer="Someone",
                     scope="the arrangement", quote="the value is Rs 500 crore")
    assert scoring.score_filing(
        results(document_type="BOTH", orders=[vague]))["rules_hit"] == []
    # ORDER_WIN still is, which is what recovered E2E Networks.
    assert "ORDER_WIN" in scoring.score_filing(
        results(document_type="ORDER_WIN", orders=[vague]))["rules_hit"]


def test_a_genuine_order_in_a_results_filing_still_fires():
    """
    The cost of dropping BOTH. A filing that really is results AND an order win
    says so in words, so the whitelist carries it — this checks that it does.
    """
    real = OrderWin(raw_value=412.5, unit="crore", customer="NTPC Limited",
                    scope="supply and installation of equipment",
                    quote="the Company has received a work order of Rs. 412.50 crore")
    r = scoring.score_filing(results(document_type="BOTH", orders=[real]))
    assert "ORDER_WIN" in r["rules_hit"], r["breakdown"]["rules"][-1]["note"]


def test_ma_language_does_not_block_an_ordinary_order():
    """
    The counterpart risk: the M&A terms must not catch orders that mention a
    transfer or a sale in passing.
    """
    for quote in (
        "received a purchase order for the supply of 500 units",
        "work order for transfer of material handling equipment to the site",
        "letter of award for the sale and installation of solar modules",
    ):
        o = OrderWin(raw_value=250.0, unit="crore", scope="supply", quote=quote)
        r = scoring.score_filing(results(document_type="ORDER_WIN", orders=[o]))
        assert "ORDER_WIN" in r["rules_hit"], quote


def test_the_wider_evidence_cannot_override_the_blacklist():
    """
    The safety property. Widening what counts as positive evidence must not
    let a non-order back in, so each rejection is retried here with the most
    favourable document_type and title available.
    """
    cases = {
        "certificate of deposit": OrderWin(
            raw_value=35000.0, unit="crore", scope="Certificate of deposit",
            quote="Certificate of deposit programme of Rs 35,000 crore"),
        "terminated order": OrderWin(
            raw_value=205.89, unit="crore", status="TERMINATED", scope="work order",
            quote="the value of the work order is Rs 205,89,14,000/-"),
        "related-party ceiling": OrderWin(
            raw_value=1500.0, unit="crore", customer="Maruti Suzuki India Limited",
            scope="contract(s) / arrangement(s)",
            quote="the consent of Members is hereby accorded for related party "
                  "transactions"),
        "regulatory penalty": OrderWin(
            raw_value=364.73, unit="crore", scope="penalty",
            quote="penalty of Rs 364.73 crore imposed by the authority"),
        "secured loan": OrderWin(
            raw_value=500.0, unit="crore", scope="term loan facility",
            quote="term loan facility of Rs 500 crore sanctioned by the bank"),
    }
    for label, order in cases.items():
        r = scoring.score_filing(
            results(document_type="ORDER_WIN", orders=[order]), "", E2E_TITLE)
        assert r["rules_hit"] == [], "{} was let through: {}".format(
            label, r["breakdown"]["rules"][-1]["note"])


def test_an_ordinary_filing_is_not_an_order_just_because_of_its_title():
    """A title cannot conjure an order out of a filing with none in it."""
    r = scoring.score_filing(results(document_type="OTHER", orders=[]), "", E2E_TITLE)
    assert r["rules_hit"] == []
    assert r["order_value_cr"] is None


def test_a_related_party_approval_is_not_an_order():
    """
    An AGM resolution authorising a CEILING for related-party dealings has a
    counterparty, a scope and a value in crore, so it extracts exactly like an
    order. ASAHIINDIA's annual report produced three of these and scored 30.0
    STRONG as "Order win Rs 92,850 Cr". Permission to transact is not business.
    """
    rpt = OrderWin(
        unit="crore", raw_value=1500.0, customer="Maruti Suzuki India Limited",
        scope="contract(s) / arrangement(s) / transaction(s)",
        quote="the consent of Members is hereby accorded for entering into "
              "and / or carrying out and / or continuing with existing, "
              "contract(s) / arrangement(s) / transaction(s)")
    r = scoring.score_filing(results(document_type="OTHER", orders=[rpt]))
    assert r["rules_hit"] == [], r["rules_hit"]
    assert r["order_value_cr"] is None
    assert r["qualifies"] is False


def test_related_party_wording_does_not_block_a_normal_order():
    """The counterpart risk: "party" appears in ordinary contract language."""
    ok = OrderWin(unit="crore", raw_value=412.5, customer="NTPC Limited",
                  scope="supply and installation of equipment",
                  quote="the Company has received a work order valued at "
                        "Rs. 412.50 crore from NTPC Limited")
    r = scoring.score_filing(results(document_type="ORDER_WIN", orders=[ok]))
    assert "ORDER_WIN" in r["rules_hit"], r["breakdown"]["rules"][-1]["note"]


def test_determination_is_not_termination():
    """
    "deTERMINATion" contains "termination". Without a word boundary, "basis of
    determination of price" and "determination of the contract price" — both
    ordinary in genuine filings — read as the contract being terminated. Found
    by sweeping cached filings; a related-party policy tripped it.
    """
    from signals import document_reports_order_loss
    for wording in (
        "Basis of determination of price for the related party transaction.",
        "the determination of the contract price shall be as per the schedule",
        "determination of contract value is subject to final measurement",
    ):
        assert document_reports_order_loss(GENUINE_WIN_TEXT + wording) is False, wording


def test_unrelated_terminations_are_not_order_losses():
    """
    Employment, nomination and option terminations are common in filings and
    have nothing to do with an order. All four of these appeared in the cached
    live corpus.
    """
    from signals import document_reports_order_loss
    for wording in (
        "management of career endings resulting from retirement or termination "
        "of employment",
        "Form SH-14 and ISR-3: Cancellation of Nomination",
        "conditions under which the option may lapse in case of termination of "
        "employment for misconduct",
        "Forfeiture / cancellation of options granted",
    ):
        assert document_reports_order_loss(GENUINE_WIN_TEXT + wording) is False, wording


def test_the_real_termination_wording_is_still_caught():
    """Having excluded the noise, the actual statements must still be found."""
    from signals import document_reports_order_loss
    for wording in (
        "Termination of work order - intimation pursuant to Regulation 30",
        "the Company has received a communication regarding termination of the "
        "aforesaid work order",
        "the work order was subsequently terminated by SDAT",
        'the contract "is hereby terminated with immediate effect"',
        "Cancellation of the purchase order received earlier",
        "the Letter of Award has been withdrawn by the authority",
    ):
        assert document_reports_order_loss(wording) is True, wording


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

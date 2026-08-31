"""
scoring.py — THE FORMULA.

    1. 25% profit growth        (PAT, year-over-year)
    2. 50% revenue growth       (year-over-year)
    3. Orders received in crore

Each rule is independent. A rule that fires contributes

    weight x (BASE_CREDIT + (1 - BASE_CREDIT) x strength)

where `strength` is 0 at the threshold and 1 at the "this is a blowout" mark in
config.py. So clearing the bar banks 70% of the rule's weight and the last 30%
is earned by the size of the beat — which is what makes the dashboard's ranking
mean something rather than just listing every qualifying filing alphabetically.

Weights sum to 100, so a filing that triggers all three with room to spare
scores 100. In practice results filings fire rules 1-2 and order filings fire
rule 3, so most real alerts land in the 21-80 band.

Everything here is pure and deterministic — no I/O, no model calls — so it can
be unit-tested against known filings. See test_scoring.py.
"""
import math

import config
from signals import (
    FilingSignals,
    document_reports_order_loss,
    figure_cr,
    resolve_statement_unit,
    real_orders,
    total_order_value_cr,
    turnaround,
    yoy_growth,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _linear_strength(value: float, threshold: float, full: float) -> float:
    """0 at the threshold, 1 at `full`, linear in between."""
    if full <= threshold:
        return 1.0
    return _clamp01((value - threshold) / (full - threshold))


def _log_strength(value: float, threshold: float, full: float) -> float:
    """
    Logarithmic 0..1 — for order values, which span four orders of magnitude.

    A Rs 5 Cr order and a Rs 5,000 Cr order both clear "orders for crores", but
    they are not the same news, and a linear ramp would leave everything below
    ~Rs 100 Cr indistinguishable from the floor.
    """
    if value <= threshold or full <= threshold:
        return 0.0
    return _clamp01(math.log10(value / threshold) / math.log10(full / threshold))


def _award(weight: float, strength: float) -> float:
    return round(weight * (config.BASE_CREDIT + (1.0 - config.BASE_CREDIT) * strength), 2)


def _fmt_cr(v) -> str:
    if v is None:
        return "n/a"
    if v >= 1000:
        return "Rs {:,.0f} Cr".format(v)
    return "Rs {:,.2f} Cr".format(v).replace(".00 Cr", " Cr")


def _fmt_pct(v) -> str:
    return "n/a" if v is None else "{:+.1f}%".format(v)


# -----------------------------------------------------------------------------
#  The three rules
# -----------------------------------------------------------------------------

def _rule_profit(signals: FilingSignals, unit=None) -> dict:
    growth = yoy_growth(signals.profit, unit)
    rule = {
        "rule": "PROFIT_GROWTH",
        "label": "Profit growth >= {:.0f}% YoY".format(config.PROFIT_GROWTH_MIN_PCT),
        "value": growth,
        "display": _fmt_pct(growth),
        "threshold": config.PROFIT_GROWTH_MIN_PCT,
        "weight": config.PROFIT_WEIGHT,
        "hit": False,
        "points": 0.0,
        "note": "",
    }

    if turnaround(signals.profit, unit) and (growth is None or growth <= 0):
        # Loss last year, profit now. yoy_growth deliberately refuses to put a
        # percentage on this (the denominator is <= 0), but it is exactly the
        # kind of result the formula is meant to catch — so it fires at
        # threshold credit and says why, rather than being silently dropped.
        rule.update({
            "hit": True,
            "points": _award(config.PROFIT_WEIGHT, 0.0),
            "display": "loss -> profit",
            "note": "Turnaround: {} a year ago to {}".format(
                _fmt_cr(figure_cr(signals.profit.year_ago, unit)),
                _fmt_cr(figure_cr(signals.profit.current, unit)),
            ),
        })
        return rule

    if growth is not None and growth >= config.PROFIT_GROWTH_MIN_PCT:
        strength = _linear_strength(
            growth, config.PROFIT_GROWTH_MIN_PCT, config.PROFIT_GROWTH_FULL_PCT
        )
        rule.update({"hit": True, "points": _award(config.PROFIT_WEIGHT, strength)})

    return rule


def _rule_revenue(signals: FilingSignals, unit=None) -> dict:
    growth = yoy_growth(signals.revenue, unit)
    rule = {
        "rule": "REVENUE_GROWTH",
        "label": "Revenue growth >= {:.0f}% YoY".format(config.REVENUE_GROWTH_MIN_PCT),
        "value": growth,
        "display": _fmt_pct(growth),
        "threshold": config.REVENUE_GROWTH_MIN_PCT,
        "weight": config.REVENUE_WEIGHT,
        "hit": False,
        "points": 0.0,
        "note": "",
    }
    if growth is not None and growth >= config.REVENUE_GROWTH_MIN_PCT:
        strength = _linear_strength(
            growth, config.REVENUE_GROWTH_MIN_PCT, config.REVENUE_GROWTH_FULL_PCT
        )
        rule.update({"hit": True, "points": _award(config.REVENUE_WEIGHT, strength)})
    return rule


def _rule_orders(signals: FilingSignals, pdf_text: str = "",
                 title: str = "") -> dict:
    total = total_order_value_cr(signals, pdf_text, title)
    rule = {
        "rule": "ORDER_WIN",
        "label": "Order win >= {}".format(_fmt_cr(config.ORDER_MIN_CR)),
        "value": total,
        "display": _fmt_cr(total),
        "threshold": config.ORDER_MIN_CR,
        "weight": config.ORDER_WEIGHT,
        "hit": False,
        "points": 0.0,
        "note": "",
    }

    # Only genuine order wins count. A buyback / QIP / dividend carries a big
    # rupee figure and gpt-4o-mini has been observed labelling one ORDER_WIN on
    # a real filing, which would otherwise max this rule out. A TERMINATED order
    # carries one too, and is the same size as the win it cancels.
    orders = real_orders(signals, pdf_text, title)

    if total is not None and total >= config.ORDER_MIN_CR:
        strength = _log_strength(total, config.ORDER_MIN_CR, config.ORDER_FULL_CR)
        rule.update({"hit": True, "points": _award(config.ORDER_WEIGHT, strength)})
        if len(orders) > 1:
            rule["note"] = "{} orders totalling {}".format(len(orders), _fmt_cr(total))
        elif orders and orders[0].customer:
            rule["note"] = "From {}".format(orders[0].customer)
    elif signals.orders and not orders:
        # Say WHICH kind of not-an-order, so a rejection is checkable rather
        # than a shrug. These are the three families that carry a large rupee
        # figure and get mistaken for business won.
        blob = " ".join("{} {}".format(o.scope or "", o.quote or "")
                        for o in signals.orders).lower()
        lost = [o for o in signals.orders if o.status == "TERMINATED"]
        if lost or document_reports_order_loss(pdf_text):
            # Worth stating plainly: this is not a near miss, it is the
            # opposite of the news the rule is looking for.
            why = "an order LOST - terminated or cancelled, not won"
        elif any(o.status in ("AMENDED", "COMPLETED") for o in signals.orders):
            why = "an existing order amended or closed out, not newly won"
        elif any(k in blob for k in ("loan", "facilit", "deposit", "borrow",
                                     "debenture", "rating", "credit")):
            why = "money raised or borrowed, not an order won"
        elif any(k in blob for k in ("penalt", "adjudicat", "show cause",
                                     "demand notice", "assessment order")):
            why = "a regulatory order against the company, not an order won"
        else:
            why = "buyback / issue / other corporate action"
        rule["note"] = "Not an order win ({})".format(why)
    elif orders:
        # An order was announced but no value disclosed — worth saying so on the
        # dashboard, but it cannot clear a rule stated in crore.
        rule["note"] = "Order announced, value not disclosed"

    return rule


# -----------------------------------------------------------------------------
#  Public entry point
# -----------------------------------------------------------------------------

def conviction_band(score: float) -> str:
    if score >= config.BAND_STRONG:
        return "STRONG"
    if score >= config.BAND_MODERATE:
        return "MODERATE"
    return "WATCH"


def build_headline(signals: FilingSignals, rules: list) -> str:
    """A one-line 'why is this on my dashboard' summary."""
    parts = []
    for r in rules:
        if not r["hit"]:
            continue
        if r["rule"] == "PROFIT_GROWTH":
            parts.append("PAT {}".format(r["display"]))
        elif r["rule"] == "REVENUE_GROWTH":
            parts.append("Revenue {}".format(r["display"]))
        elif r["rule"] == "ORDER_WIN":
            parts.append("Order win {}".format(r["display"]))

    if not parts:
        return "No formula rule cleared"

    head = " | ".join(parts)
    if signals.reporting_period:
        head += " ({})".format(signals.reporting_period)
    return head


def score_filing(signals: FilingSignals, pdf_text: str = "",
                 title: str = "") -> dict:
    """
    Apply the formula to one filing's extracted signals.

    Returns a dict with the total score, the conviction band, which rules fired,
    and a full per-rule breakdown — the breakdown is stored alongside the alert
    so the dashboard can show WHY a stock was flagged, not just that it was.
    """
    # The denomination to read the statement in. Resolved from the DOCUMENT's
    # own heading where it has one, because the model mislabels units — pass
    # pdf_text in and this protects every figure below from a 100x error.
    unit = resolve_statement_unit(signals, pdf_text)

    # A disabled rule is not evaluated at all — it contributes no points and
    # does not appear in rules_hit, so a score cannot come from a rule that is
    # supposed to be off.
    rules = []
    if config.PROFIT_RULE_ENABLED:
        rules.append(_rule_profit(signals, unit))
    if config.REVENUE_RULE_ENABLED:
        rules.append(_rule_revenue(signals, unit))
    if config.ORDER_RULE_ENABLED:
        rules.append(_rule_orders(signals, pdf_text, title))

    score = round(sum(r["points"] for r in rules), 2)
    hits = [r["rule"] for r in rules if r["hit"]]

    return {
        "score": score,
        "conviction": conviction_band(score),
        "rules_hit": hits,
        "qualifies": score >= config.ALERT_MIN_SCORE and bool(hits),
        "headline": build_headline(signals, rules),
        "profit_growth_pct": yoy_growth(signals.profit, unit),
        "revenue_growth_pct": yoy_growth(signals.revenue, unit),
        "order_value_cr": total_order_value_cr(signals, pdf_text, title),
        "breakdown": {
            "rules": rules,
            # Follows the ENABLED set, so the score is always readable against
            # what is actually in force.
            "max_possible": sum(r["weight"] for r in rules),
            "base_credit": config.BASE_CREDIT,
            "statement_unit": unit or "",
        },
    }

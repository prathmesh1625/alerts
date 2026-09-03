"""
trader.py — decides which alerts WOULD be traded, and records the decision.

SHADOW MODE ONLY. This module contains no code that can place an order. That is
deliberate and it is the whole point of this stage: not a flag that could be
flipped by accident, but no execution path to flip. Placing real orders through
a broker is a separate change, to be made once there is shadow data worth
acting on.

Why that sequencing. Over five days of live filings this engine produced, and
then had fixed: a deposit programme read as a Rs 35,000 Cr order, a TERMINATED
contract read as a win, an AGM approval ceiling read as Rs 92,850 Cr of orders,
a promoter share sale read as a Rs 1,330 Cr order, a USD contract undervalued
80x, and a two-month-old award recapped in a press release. Each is fixed and
each was found the same way — a wrong alert somebody noticed. While the cost of
a mistake is an ignored message, that loop is cheap. With money attached it is
not, so the recorder runs first and the executor waits on its results.

The gate is deliberately narrow:

    conviction STRONG, order >= Rs 100 Cr, and order worth >= 10% of market cap

The last condition is what makes the other two mean something. Over 30 days it
admits 8 alerts and rejects 9 that clear the first two: it keeps CCME, whose
Rs 133 Cr order is 78% of a Rs 171 Cr company, and rejects NHPC, whose Rs 392 Cr
order is 0.52% of a Rs 76,000 Cr one. Same "big order", entirely different news.

Run:  python trader.py            the loop
      python trader.py --once     one pass
      python trader.py --report   what it would have done so far
"""
import argparse
import datetime
import sys
import time

import config
import db
import marketcap

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _log(msg):
    print("[trader] {}".format(msg), flush=True)


# -----------------------------------------------------------------------------
#  The gate
# -----------------------------------------------------------------------------

def evaluate(alert):
    """
    (would_trade, reason, detail) for one alert.

    `detail` carries the numbers the decision turned on, so a recorded trade can
    be argued with later rather than taken on trust.
    """
    sym = alert.get("company_symbol")
    conviction = alert.get("conviction")
    order = alert.get("order_value_cr")
    cap = alert.get("market_cap_cr")
    pct = (round(order / cap * 100.0, 2)
           if order and cap and cap > 0 else None)

    detail = {"conviction": conviction, "order_cr": order,
              "market_cap_cr": cap, "order_to_mcap_pct": pct}

    if conviction != "STRONG":
        return False, "not STRONG ({})".format(conviction), detail
    if not order or order < config.TRADE_MIN_ORDER_CR:
        return False, "order below Rs {:.0f} Cr".format(config.TRADE_MIN_ORDER_CR), detail
    if cap is None:
        # NOT silently skipped. Six of 23 otherwise-qualifying alerts in a
        # month had no market cap, and an invisible exclusion is how a rule
        # stops applying without anyone noticing.
        return False, "market cap unknown - cannot judge materiality", detail
    if pct is None or pct < config.TRADE_MIN_ORDER_TO_MCAP_PCT:
        return False, "order is only {:.2f}% of market cap".format(pct or 0.0), detail

    return True, "STRONG, Rs {:.0f} Cr, {:.1f}% of market cap".format(order, pct), detail


def session_state(when=None):
    """
    Whether an alert at this moment could be traded now, and why not.

    56% of alerts in a week arrived after 15:30, so "execute on the alert" means
    the next open for most of them - a different trade, after everyone else has
    read the same filing overnight. Recorded explicitly rather than assumed.
    """
    now = when or datetime.datetime.now(IST)
    hhmm = now.strftime("%H:%M")
    if now.weekday() >= 5:
        return "WEEKEND", "market closed - would queue to Monday's open"
    if hhmm < "09:15":
        return "PRE_OPEN", "before the open - would queue to today's open"
    if hhmm >= "15:30":
        return "AFTER_CLOSE", "after the close - would queue to the next open"
    return "OPEN", "market open - would execute now"


# -----------------------------------------------------------------------------
#  Reference price
# -----------------------------------------------------------------------------

def reference_price(symbol, company_name=None):
    """
    The price a shadow fill is recorded against, or None.

    BSE's last traded price, from the quote endpoint the market-cap lookup
    already uses. It is a reference, not a fill: a real order would move
    against it, and for an after-hours alert the real entry is the next open,
    which nobody knows yet.
    """
    try:
        scrip = marketcap.symbol_to_scrip(symbol, company_name)
        if not scrip:
            return None
        import requests
        r = requests.get(
            "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w",
            params={"Debtflag": "", "scripcode": scrip, "seriesid": ""},
            headers=marketcap.HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        ltp = ((r.json() or {}).get("CurrRate") or {}).get("LTP")
        return float(str(ltp).replace(",", "")) if ltp else None
    except Exception as e:
        _log("price lookup failed for {}: {}".format(symbol, e))
        return None


# -----------------------------------------------------------------------------
#  One pass
# -----------------------------------------------------------------------------

def run_once(verbose=True):
    """Record a shadow decision for every alert not yet judged."""
    try:
        alerts = db.fetch_untraded_alerts(config.TRADE_LOOKBACK_DAYS,
                                          config.TRADE_BATCH)
    except Exception as e:
        _log("cannot read alerts: {}".format(e))
        return 0

    if not alerts:
        return 0

    taken = 0
    for a in alerts:
        sym = a["company_symbol"]
        would, reason, detail = evaluate(a)

        if would:
            # One position per symbol. SUGSLLOYD announced the same Rs 214.27
            # Cr order on two consecutive days and alerted twice; a trader
            # acting on both would have bought the same thing twice.
            try:
                if db.traded_recently(sym, config.TRADE_DEDUP_DAYS):
                    would, reason = False, "already taken within {} day(s)".format(
                        config.TRADE_DEDUP_DAYS)
            except Exception as e:
                _log("dedup check failed for {}: {}".format(sym, e))
                continue

        if would:
            try:
                today = db.count_trades_today()
            except Exception:
                today = 0
            if today >= config.TRADE_MAX_PER_DAY:
                would, reason = False, "daily cap of {} reached".format(
                    config.TRADE_MAX_PER_DAY)

        state, state_note = session_state()
        price = reference_price(sym, a.get("company_name")) if would else None
        qty = (int(config.TRADE_VALUE_INR // price)
               if would and price and price > 0 else None)

        try:
            db.save_paper_trade({
                "announcement_id": a["announcement_id"],
                "company_symbol": sym,
                "would_trade": would,
                "reason": reason,
                "session_state": state,
                "session_note": state_note,
                "conviction": detail["conviction"],
                "order_cr": detail["order_cr"],
                "market_cap_cr": detail["market_cap_cr"],
                "order_to_mcap_pct": detail["order_to_mcap_pct"],
                "reference_price": price,
                "quantity": qty,
                "intended_value_inr": (qty * price) if qty and price else None,
            })
        except Exception as e:
            _log("could not record {}: {}".format(sym, e))
            continue

        if would:
            taken += 1
            _log("WOULD BUY {:12} {} qty {} @ ~{} | {} | {}".format(
                sym, reason, qty, price, state_note, a.get("headline")))
        elif verbose:
            _log("skip      {:12} {}".format(sym, reason))

    return taken


def report(days=30):
    """What the gate would have done, for reading before anything goes live."""
    rows = db.fetch_paper_trades(days, 500)
    taken = [r for r in rows if r["would_trade"]]
    print("=" * 78)
    print("SHADOW TRADES - last {} days".format(days))
    print("=" * 78)
    print("  alerts judged : {}".format(len(rows)))
    print("  would trade   : {}".format(len(taken)))
    print()
    for r in taken:
        print("  {:12} {:>10} Cr  {:>6}%  qty {:>6}  @ {:>9}  {}".format(
            r["company_symbol"],
            r["order_cr"], r["order_to_mcap_pct"], r["quantity"],
            r["reference_price"], (r["session_state"] or "")))
    if not taken:
        print("  (nothing has cleared the gate yet)")
    print()
    print("  NOTE: no order was placed. This module cannot place one.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--report", action="store_true", help="show recorded decisions")
    ap.add_argument("--days", type=int, default=30, help="window for --report")
    args = ap.parse_args()

    db.ensure_schema()

    if args.report:
        report(args.days)
        return

    _log("SHADOW MODE - records decisions, places nothing")
    _log("gate: STRONG, order >= Rs {:.0f} Cr, >= {:.0f}% of market cap".format(
        config.TRADE_MIN_ORDER_CR, config.TRADE_MIN_ORDER_TO_MCAP_PCT))
    _log("notional per trade: Rs {:,.0f}, max {}/day".format(
        config.TRADE_VALUE_INR, config.TRADE_MAX_PER_DAY))

    if args.once:
        _log("{} would-trade decision(s)".format(run_once()))
        return

    while True:
        try:
            run_once(verbose=False)
        except Exception as e:
            print("[trader] pass failed: {}: {}".format(type(e).__name__, e),
                  file=sys.stderr)
        time.sleep(config.TRADE_POLL_SEC)


if __name__ == "__main__":
    main()

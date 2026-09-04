"""
papertrader.py — buys on an alert, with virtual money.

The same gate the shadow recorder uses (trader.evaluate), so the paper trades
and the shadow record are measuring one thing rather than two: STRONG
conviction, an order of at least Rs 100 Cr, and that order worth at least 10%
of the company. About two a week.

Deliberately the same gate rather than "every alert". At roughly eight alerts a
day against Rs 5,00,000 of virtual capital, buying everything would exhaust the
account in a morning and measure nothing except how fast money runs out.

Everything it can do happens on MegaBull's simulator. megabull.BASE is a
constant, so there is no configuration that turns this into a real order.

The brakes are the ones the shadow recorder already had, because the reasons
for them have not changed:

  * one position per symbol inside a window - SUGSLLOYD announced the same
    Rs 214.27 Cr order on two consecutive days;
  * a daily cap, because the gate admits about two a WEEK and hitting a daily
    limit means something upstream broke;
  * an age bound, so switching this on against a populated database does not
    replay a month of alerts into the account in one pass;
  * sizing read from the live balance rather than assumed, since the API
    rejects an order that exceeds the virtual money left.

Run:  python papertrader.py            the loop
      python papertrader.py --once
      python papertrader.py --status    account and gate, places nothing
"""
import argparse
import sys
import time

import config
import db
import megabull
import trader


# How long to sleep between "still disabled" checks. Nothing depends on it
# being short - the flag is only read at startup - so it exists to keep the
# process parked rather than exiting. See main().
IDLE_SLEEP_SEC = 3600


def _log(msg):
    print("[paper] {}".format(msg), flush=True)


def quantity_for(price, available):
    """
    How many shares Rs TRADE_VALUE_INR buys, capped by what is actually left.

    Returns 0 when the position would be meaningless or unaffordable, which the
    caller records as a skip rather than sending an order the API will reject.
    """
    try:
        price = float(price or 0)
        available = float(available or 0)
    except (TypeError, ValueError):
        return 0
    if price <= 0:
        return 0
    budget = min(float(config.PAPER_TRADE_VALUE_INR), available)
    qty = int(budget // price)
    return min(qty, 10000)      # MegaBull's own ceiling


def _preflight():
    if not config.PAPER_TRADING_ENABLED:
        _log("PAPER_TRADING_ENABLED is false - nothing to do")
        return False
    if not megabull.configured():
        _log("REFUSING TO START: ALERT_MEGABULL_API_KEY is not set")
        return False
    try:
        acct = megabull.account()
    except megabull.MegaBullError as e:
        _log("REFUSING TO START: {}".format(e))
        return False

    _log("paper trading against {} - VIRTUAL MONEY ONLY".format(megabull.BASE))
    _log("available Rs {:,.0f} of Rs {:,.0f}".format(
        acct.get("available") or 0, acct.get("virtual_money") or 0))
    _log("gate: STRONG, order >= Rs {:.0f} Cr, >= {:.0f}% of market cap; "
         "Rs {:,.0f} per position, max {}/day".format(
             config.TRADE_MIN_ORDER_CR, config.TRADE_MIN_ORDER_TO_MCAP_PCT,
             config.PAPER_TRADE_VALUE_INR, config.PAPER_MAX_PER_DAY))
    return True


_last_session = None


def run_once(verbose=True):
    """Buy anything that clears the gate and has not been bought already."""
    global _last_session

    # Nothing is attempted while the market is shut, and - this is the point -
    # nothing is RECORDED either. A recorded attempt is keyed on
    # announcement_id, so writing a rejection here would mark the alert handled
    # and it would never be retried at the open. 56% of alerts arrive after
    # 15:30 (see trader.session_state), so that is the common case, not a
    # corner one. Leave them pending instead.
    state, why = trader.session_state()
    if state != _last_session:
        _log(why)
        _last_session = state
    if state != "OPEN":
        return 0

    try:
        alerts = db.fetch_unpapered_alerts(config.PAPER_LOOKBACK_HOURS,
                                           config.PAPER_BATCH)
    except Exception as e:
        _log("cannot read alerts: {}".format(e))
        return 0
    if not alerts:
        return 0

    try:
        available = (megabull.account() or {}).get("available") or 0
    except megabull.MegaBullError as e:
        _log("cannot read the account: {}".format(e))
        return 0

    bought = 0
    for a in alerts:
        sym = a["company_symbol"]
        would, reason, detail = trader.evaluate(a)

        if would:
            try:
                if db.papered_recently(sym, config.PAPER_DEDUP_DAYS):
                    would, reason = False, "already held from a recent alert"
                elif db.count_paper_orders_today() >= config.PAPER_MAX_PER_DAY:
                    would, reason = False, "daily cap of {} reached".format(
                        config.PAPER_MAX_PER_DAY)
            except Exception as e:
                _log("{}: pre-check failed: {}".format(sym, e))
                continue

        token = qty = price = None
        order_id = None
        status = "SKIPPED"

        if would:
            token = megabull.token_for(sym)
            if not token:
                would, reason = False, "MegaBull does not list this symbol"

        if would:
            price = trader.reference_price(sym, a.get("company_name"))
            qty = quantity_for(price, available)
            if not qty:
                would, reason = False, (
                    "no affordable quantity at Rs {} with Rs {:,.0f} left".format(
                        price, available))

        if would:
            try:
                resp = megabull.place_buy(token, qty, price=price)
                order_id = str((resp or {}).get("orderId")
                               or (resp or {}).get("id") or "")
                fill = (resp or {}).get("price") or price
                status = "PLACED"
                reason = "bought {} at Rs {}".format(qty, fill)
                available -= qty * float(fill or 0)
                price = fill
                bought += 1
                _log("BOUGHT {:12} {:>5} @ {:<9} order {}".format(
                    sym, qty, fill, order_id or "?"))
            # Exception, not just MegaBullError. An unexpected error here used
            # to escape the pass entirely, so nothing was recorded and the same
            # alert failed the same way every 60 seconds forever. Recording it
            # costs one alert; letting it escape costs all of them.
            except Exception as e:
                status = "FAILED"
                reason = "{}: {}".format(type(e).__name__, e)
                _log("{:12} order rejected: {}".format(sym, reason))

        try:
            db.save_paper_order({
                "announcement_id": a["announcement_id"],
                "company_symbol": sym,
                "status": status,
                "reason": reason,
                "instrument_token": token,
                "quantity": qty,
                "price": price,
                "order_id": order_id or None,
                "order_to_mcap_pct": detail.get("order_to_mcap_pct"),
                "order_cr": detail.get("order_cr"),
                "market_cap_cr": detail.get("market_cap_cr"),
            })
        except Exception as e:
            _log("could not record {}: {}".format(sym, e))
        if verbose and status == "SKIPPED":
            _log("skip   {:12} {}".format(sym, reason))

    return bought


def show_status():
    st = megabull.status()
    print("=" * 72)
    print("PAPER TRADING - virtual money only")
    print("=" * 72)
    for k, v in st.items():
        print("  {:16} {}".format(k, v))
    if st.get("live"):
        try:
            pos = megabull.positions()
            print("  {:16} {}".format("open positions", len(pos)))
        except megabull.MegaBullError as e:
            print("  positions unavailable: {}".format(e))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true",
                    help="account and gate; places nothing")
    args = ap.parse_args()

    if args.status:
        show_status()
        return

    if not _preflight():
        if args.once:
            return
        # Do NOT exit here. Under `restart: always` a container that exits
        # cleanly is restarted at once, exits again, and keeps doing it - a
        # restart storm against the Docker daemon that the whole stack shares.
        # This service is off by default, so that is the DEFAULT path, and it
        # took the site down the first time it ran. Idle instead: inert is not
        # the same as thrashing.
        _log("idling - set ALERT_PAPER_TRADING_ENABLED=true and redeploy")
        while True:
            time.sleep(IDLE_SLEEP_SEC)

    db.ensure_schema()
    if args.once:
        _log("{} order(s) placed".format(run_once()))
        return

    while True:
        try:
            run_once(verbose=False)
        except Exception as e:
            print("[paper] pass failed: {}: {}".format(type(e).__name__, e),
                  file=sys.stderr)
        time.sleep(config.PAPER_POLL_SEC)


if __name__ == "__main__":
    main()

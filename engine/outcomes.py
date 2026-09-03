"""
outcomes.py — what the stock was doing before an alert, and what it did after.

Written because five names looked like they had something in common and did
not. TEJASNET, ASHOKA and WELCORP each moved 7-15% after an alert; TEXRAIL,
with a LARGER order relative to its size, did not. The order-to-market-cap
ratio separated none of them: 16.9% and 19.0% ran, 25.6% did not.

Splitting each move into its overnight gap and its intraday part explained it,
and killed the idea at the same time:

    WELCORP   filed 22:52, market shut   gap +8.47   open→close +6.29
    TEJASNET  filed 18:20, market shut   gap +5.25   open→close +2.26
    ASHOKA    filed Saturday             gap +4.48   open→close +2.20
    TEXRAIL   filed 13:11, market OPEN   gap -0.60   open→close +4.40

The "winners" were filings the market read while it was closed, so most of the
move happened before anyone could act. Buying TEJASNET at the open earned
2.26%, not 7.63%. TEXRAIL, the apparent failure, offered the most capturable
move of the four.

So this module records the number that matters — `capturable_pct`, the
open-to-close part — rather than the number that looks impressive. Two passes:
momentum before the alert, when the decision is recorded; the outcome once the
next session has been published.

Run:  python outcomes.py            the loop
      python outcomes.py --once
      python outcomes.py --report   what the record says so far
"""
import argparse
import datetime
import sys
import time

import config
import db

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _log(msg):
    print("[outcomes] {}".format(msg), flush=True)


def _pct(frm, to):
    try:
        frm, to = float(frm), float(to)
    except (TypeError, ValueError):
        return None
    return round((to - frm) / frm * 100.0, 2) if frm > 0 else None


def momentum(symbol, before_date):
    """
    (20d %, 5d %, range position) heading into the alert, or Nones.

    `range position` is where the last close sat inside the 20-day range: 100
    means at the top of it, 0 at the bottom. INOXWIND is the reason it is here
    — it was the one name of five that had been falling for three weeks, and
    the only variable that separated it.
    """
    rows = db.sessions_for(symbol, before_date, limit=21)
    if len(rows) < 6:
        return None, None, None
    closes = [float(r["close"]) for r in rows if r.get("close")]
    if len(closes) < 6:
        return None, None, None

    last = closes[-1]
    m20 = _pct(closes[0], last) if len(closes) >= 15 else None
    m5 = _pct(closes[-6], last)
    lo, hi = min(closes[:-1]), max(closes[:-1])
    # Clamped to 0-100 so it reads as a position in the range. A breakout
    # would otherwise score above 100, which is true but makes the number
    # harder to compare across names than it is worth.
    pos = (round(max(0.0, min(100.0, (last - lo) / (hi - lo) * 100.0)), 2)
           if hi > lo else None)
    return m20, m5, pos


def outcome(symbol, announced_at):
    """
    How the first tradeable session after the alert actually went.

    Returns (session_date, gap %, capturable %, total %) or Nones.

    A filing published after 15:30 is repriced at the NEXT open, so the gap is
    the part that was gone before anyone could act and open-to-close is the
    part that was not. Keeping them apart is the whole point: a headline
    "+7.63% day" was +5.25% gap.
    """
    if not announced_at:
        return None, None, None, None
    d = announced_at.date() if hasattr(announced_at, "date") else announced_at
    hhmm = announced_at.strftime("%H:%M") if hasattr(announced_at, "strftime") else "00:00"

    # Filed after the close, or on a non-trading day: the reaction lands on the
    # next session that exists, which first_session_on_or_after finds.
    target = d + datetime.timedelta(days=1) if hhmm >= "15:30" else d
    row = db.first_session_on_or_after(symbol, target)
    if not row:
        return None, None, None, None

    o, c, p = row.get("open"), row.get("close"), row.get("prev_close")
    return (row["session_date"], _pct(p, o), _pct(o, c), _pct(p, c))


def run_once(days=None, verbose=True):
    """Fill in momentum and outcome for decisions that are missing them."""
    days = days or config.OUTCOME_LOOKBACK_DAYS
    try:
        rows = db.fetch_trades_awaiting_outcome(days, config.OUTCOME_BATCH)
    except Exception as e:
        _log("cannot read decisions: {}".format(e))
        return 0
    if not rows:
        return 0

    done = 0
    for t in rows:
        sym = t["company_symbol"]
        announced = t.get("announced_at")
        fields = {}

        if t.get("momentum_20d_pct") is None:
            m20, m5, pos = momentum(
                sym, announced.date() if hasattr(announced, "date") else announced)
            if m20 is not None or m5 is not None:
                fields.update(momentum_20d_pct=m20, momentum_5d_pct=m5,
                              range_position=pos)

        sess, gap, capturable, total = outcome(sym, announced)
        if sess is not None:
            # Only once the session is genuinely over. A part-day bar would
            # record a half-formed move as the answer.
            today = datetime.datetime.now(IST).date()
            if sess < today or datetime.datetime.now(IST).strftime("%H:%M") > "18:30":
                fields.update(outcome_date=sess, gap_pct=gap,
                              capturable_pct=capturable, total_move_pct=total)

        if not fields:
            continue
        try:
            db.update_trade_metrics(t["id"], fields)
        except Exception as e:
            _log("{}: {}".format(sym, e))
            continue
        done += 1
        if verbose and fields.get("outcome_date"):
            _log("{:12} gap {:>6} | capturable {:>6} | total {:>6}".format(
                sym,
                "{:+.2f}".format(gap) if gap is not None else "-",
                "{:+.2f}".format(capturable) if capturable is not None else "-",
                "{:+.2f}".format(total) if total is not None else "-"))
    return done


def report(days=60):
    """What the record says. Deliberately blunt about the sample size."""
    rows = [r for r in db.fetch_paper_trades(days, 500)
            if r.get("capturable_pct") is not None]
    print("=" * 96)
    print("MEASURED ALERTS - last {} days".format(days))
    print("=" * 96)
    if not rows:
        print("  nothing measured yet")
        return

    print("{:12} {:>7} {:>8} {:>8} {:>8} {:>9} {:>9}".format(
        "symbol", "%ofco", "mom20d", "rangePos", "gap%", "capturable", "total"))
    for r in rows[:40]:
        print("{:12} {:>7} {:>8} {:>8} {:>8} {:>9} {:>9}".format(
            r["company_symbol"],
            r.get("order_to_mcap_pct") or "-", r.get("momentum_20d_pct") or "-",
            r.get("range_position") or "-", r.get("gap_pct") or "-",
            r.get("capturable_pct") or "-", r.get("total_move_pct") or "-"))

    cap = [float(r["capturable_pct"]) for r in rows]
    gap = [float(r["gap_pct"]) for r in rows if r.get("gap_pct") is not None]
    print()
    print("  n = {}".format(len(cap)))
    print("  capturable (open->close): mean {:+.2f}%  median {:+.2f}%  positive {}/{}".format(
        sum(cap) / len(cap), sorted(cap)[len(cap) // 2],
        len([x for x in cap if x > 0]), len(cap)))
    if gap:
        print("  overnight gap           : mean {:+.2f}%  - the part nobody could take".format(
            sum(gap) / len(gap)))
    print()
    print("  Capturable is the honest number. A big total move that was mostly")
    print("  gap was never available. Under a few dozen rows none of this means")
    print("  anything yet - it is a record being built, not a result.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    db.ensure_schema()
    if args.report:
        report(args.days)
        return
    if args.once:
        _log("{} decision(s) measured".format(run_once(args.days)))
        return

    while True:
        try:
            run_once(verbose=False)
        except Exception as e:
            print("[outcomes] pass failed: {}: {}".format(type(e).__name__, e),
                  file=sys.stderr)
        time.sleep(config.OUTCOME_POLL_SEC)


if __name__ == "__main__":
    main()

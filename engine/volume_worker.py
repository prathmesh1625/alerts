"""
volume_worker.py — rule 4's daily job.

    sync Bhavcopy  ->  compare each stock against its own history  ->  alert

Runs end-of-day, because that is when NSE publishes the full-market file. That
fits the signal: a spike confirmed on a complete session is what informs
tomorrow's trading, which is what this dashboard was asked for.

Completely independent of the filing pipeline. It reads and writes only
`daily_volume` and `volume_alerts`, never `announcements`, `filing_analyses` or
`stock_alerts` — so nothing here can change a score the other three rules have
already produced.

Run:  python volume_worker.py            (loop, one pass per hour)
      python volume_worker.py --once     (single pass)
      python volume_worker.py --backfill 40
"""
import argparse
import sys
import time
import traceback

import datetime as dt

import bhavcopy
import config
import db
import market
import volume


def sync_sessions(back_days=None, force=False) -> int:
    """
    Download any Bhavcopy sessions we do not have yet. Returns sessions added.

    Days already stored are skipped without a request, so running this hourly
    costs one 404 on a day NSE has not published yet, and nothing at all once
    it has.
    """
    back_days = back_days or config.BHAVCOPY_BACKFILL_SESSIONS
    added = 0
    for day in bhavcopy.recent_sessions(back_days):
        if not force and db.have_session(day):
            continue
        rows = bhavcopy.fetch(day)
        if not rows:
            continue  # weekend, holiday, or not published yet
        n = db.save_daily_volumes(rows)
        added += 1
        print("[volume] {}: stored {} stocks".format(day, n), flush=True)
    return added


def detect_for_session(session_date) -> dict:
    """Apply rule 4 to every stock that traded on `session_date`."""
    candidates = db.fetch_volume_candidates(session_date, config.VOLUME_LOOKBACK_SESSIONS)
    fired = 0
    reasons = {}

    for row in candidates:
        history = [v for v in (row.get("history") or []) if v]

        # Cooldown is measured in TRADING sessions, not calendar days — a
        # weekend must not count as two sessions of silence.
        since = None
        if row.get("last_alert"):
            since = db.sessions_between(row["symbol"], row["last_alert"], session_date)

        verdict = volume.detect_spike(
            row["symbol"],
            {"volume": row["volume"], "turnover_cr": float(row["turnover_cr"] or 0),
             "close": float(row["close"]) if row["close"] is not None else None,
             "prev_close": float(row["prev_close"]) if row["prev_close"] is not None else None},
            history,
            sessions_since_last_alert=since,
        )

        if verdict["hit"]:
            verdict["conviction"] = volume.conviction_band(verdict["score"])
            db.save_volume_alert(verdict, session_date)
            fired += 1
            print("[volume] ALERT {:<12} {}  score {:.1f}".format(
                verdict["symbol"], verdict["headline"], verdict["score"]), flush=True)
        else:
            key = verdict["reason"].split("(")[0].split(",")[0][:40]
            reasons[key] = reasons.get(key, 0) + 1

    return {"examined": len(candidates), "alerts": fired, "reasons": reasons}


def intraday_pass() -> dict:
    """
    Detect spikes DURING the session, from the live movers feed.

    Baselines come from Bhavcopy (the whole market), today's volume from the
    live feed. A stock that spikes is by definition moving, so it appears in
    the movers feed on the day it matters - while the feed alone could never
    have supplied its quiet baseline.

    No session-fraction scaling: see the note on VOLUME_INTRADAY_ENABLED. The
    same detect_spike is used as end-of-day, so an intraday alert and its
    confirmation cannot disagree about what counts as a spike.
    """
    snap = market.snapshot()
    if not snap["status"].get("is_open"):
        return {"skipped": "market closed"}

    stocks = snap["stocks"]
    if not stocks:
        return {"examined": 0, "alerts": 0}

    today = dt.date.today()
    baselines = db.fetch_baselines([s["symbol"] for s in stocks], today)

    fired = 0
    for s in stocks:
        b = baselines.get(s["symbol"])
        if not b:
            continue   # no Bhavcopy history yet for this symbol

        since = None
        if b.get("last_alert"):
            since = db.sessions_between(s["symbol"], b["last_alert"], today)

        verdict = volume.detect_spike(
            s["symbol"],
            {"volume": s.get("volume"), "turnover_cr": s.get("turnover_cr"),
             "close": s.get("ltp"), "prev_close": s.get("prev_close")},
            [v for v in (b.get("history") or []) if v],
            sessions_since_last_alert=since,
        )
        if verdict["hit"]:
            verdict["conviction"] = volume.conviction_band(verdict["score"])
            db.save_volume_alert(verdict, today, is_intraday=True)
            fired += 1
            print("[volume] LIVE  {:<12} {}  score {:.1f}".format(
                verdict["symbol"], verdict["headline"], verdict["score"]), flush=True)

    return {"examined": len(stocks), "alerts": fired}


def run_once(backfill=None) -> None:
    db.ensure_schema()
    added = sync_sessions(backfill)

    session = db.latest_session()
    if not session:
        print("[volume] no Bhavcopy data yet - nothing to score", flush=True)
        return

    result = detect_for_session(session)
    print("[volume] {}: examined {} stocks, {} alert(s)".format(
        session, result["examined"], result["alerts"]), flush=True)

    if result["alerts"] == 0 and result["reasons"]:
        # A quiet day is normal; showing WHY makes it auditable rather than
        # leaving you wondering whether the rule ran at all.
        top = sorted(result["reasons"].items(), key=lambda kv: -kv[1])[:4]
        for reason, n in top:
            print("[volume]   {:>5} x {}".format(n, reason), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Rule 4 - volume spike detection")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--backfill", type=int, default=None,
                    help="how many days of Bhavcopy to reach back for")
    args = ap.parse_args()

    print("=" * 66, flush=True)
    print("Volume Spike Worker (rule 4)", flush=True)
    print("  spike at   : {}x the {}-session median".format(
        config.VOLUME_SPIKE_MIN_X, config.VOLUME_LOOKBACK_SESSIONS), flush=True)
    print("  also needs : a new {}-session high".format(
        config.VOLUME_LOOKBACK_SESSIONS) if config.VOLUME_REQUIRE_NEW_HIGH
        else "  new high   : not required", flush=True)
    print("  floors     : Rs {:.0f} Cr turnover, {:,.0f} shares/day baseline".format(
        config.VOLUME_MIN_TURNOVER_CR, config.VOLUME_MIN_BASELINE_SHARES), flush=True)
    print("  cooldown   : {} session(s)".format(config.VOLUME_COOLDOWN_SESSIONS), flush=True)
    print("=" * 66, flush=True)

    if args.once:
        run_once(args.backfill)
        return

    first = True
    last_eod = 0.0
    while True:
        try:
            # End-of-day pass: sync Bhavcopy and score the full session. Hourly,
            # because Bhavcopy is published once a day and anything faster just
            # collects 404s.
            if first or (time.time() - last_eod) >= 3600:
                run_once(args.backfill if first else 5)
                last_eod = time.time()
                first = False

            # Intraday pass: only while the market is actually open.
            if config.VOLUME_INTRADAY_ENABLED:
                r = intraday_pass()
                if r.get("skipped"):
                    pass                       # market shut; nothing to say
                elif r["alerts"]:
                    print("[volume] intraday: {} of {} movers spiking".format(
                        r["alerts"], r["examined"]), flush=True)
        except Exception as e:
            print("[volume] cycle error: {}".format(e), file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        time.sleep(config.VOLUME_INTRADAY_INTERVAL_SEC
                   if config.VOLUME_INTRADAY_ENABLED else 3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[volume] stopped", flush=True)

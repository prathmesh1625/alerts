"""
pricehistory.py — what the stock did over the last six months.

Context for an order win: a Rs 200 Cr order means something different for a
company whose shares have doubled since March than for one that has halved.

The source is NSE's Bhavcopy archive, which we already download for the volume
rule. That choice was forced rather than preferred — NSE's historical API
answers 503 from this IP, BSE's CSV download returns an empty body, and BSE's
chart endpoint only ever serves the current session, whatever its `flag` says.
Bhavcopy is a plain public archive and it works.

It is also efficient in a way a per-symbol API is not: ONE download gives the
close for every symbol on that date, so a day's worth of alerts costs a single
request rather than one each. The result is cached in `daily_volume`, the table
the volume rule already fills, so nothing new is stored to make this work.

A BSE-only listing has no row in an NSE Bhavcopy, so its change is unknown.
That is reported as unknown rather than guessed.
"""
import datetime

import bhavcopy
import config
import db

# Baselines already fetched this process, so a day's alerts share one download.
_LOADED = set()


def _log(msg):
    print("[price] {}".format(msg), flush=True)


def reference_date(today=None, months=None):
    """
    The trading day closest to `months` ago, stepping back off weekends.

    Holidays are handled by the caller: if a Bhavcopy is missing for this date
    it steps back another day, because the archive simply has no file rather
    than an empty one.
    """
    months = months or config.PRICE_HISTORY_MONTHS
    today = today or datetime.date.today()
    target = today - datetime.timedelta(days=int(round(months * 30.44)))
    while target.weekday() >= 5:
        target -= datetime.timedelta(days=1)
    return target


def ensure_baseline(session_date):
    """
    Make sure `daily_volume` holds the closes for one past session.

    Returns the date actually loaded, which may be a day or two earlier if the
    requested one was a holiday, or None if nothing could be fetched.
    """
    for back in range(0, config.PRICE_BASELINE_MAX_BACKTRACK):
        d = session_date - datetime.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        if d in _LOADED:
            return d
        try:
            if db.have_session(d):
                _LOADED.add(d)
                return d
        except Exception as e:
            _log("cannot check session {}: {}".format(d, e))
            return None

        try:
            rows = bhavcopy.fetch(d)
        except Exception as e:
            _log("bhavcopy {} failed: {}".format(d, e))
            continue
        if not rows:
            continue
        try:
            db.save_daily_volumes(rows)
        except Exception as e:
            _log("could not store baseline {}: {}".format(d, e))
            return None
        _LOADED.add(d)
        _log("baseline loaded for {} ({} symbols)".format(d, len(rows)))
        return d
    return None


def change(symbol, months=None):
    """
    (then, now, pct_change) over the window, or (None, None, None).

    `pct` is the move in percent: 100 -> 600 is +500.0. None anywhere means we
    could not determine it — a BSE-only listing absent from NSE's Bhavcopy, or
    a company not yet listed six months ago.
    """
    symbol = (symbol or "").upper()
    if not symbol:
        return None, None, None

    ref = reference_date(months=months)
    loaded = ensure_baseline(ref)
    if not loaded:
        return None, None, None

    try:
        then = db.close_on(symbol, loaded)
        now = db.latest_close(symbol)
    except Exception as e:
        _log("{}: {}".format(symbol, e))
        return None, None, None

    if then is None or now is None or then <= 0:
        return then, now, None
    return then, now, round((now - then) / then * 100.0, 2)

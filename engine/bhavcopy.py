"""
bhavcopy.py — NSE's daily full-market report, the baseline for rule 4.

Volume spikes need history for EVERY stock, including one that is quiet for a
month and then explodes. The live movers feed cannot provide that: a stock only
appears there once it is already moving, so on the day it spikes there would be
nothing to compare against.

Bhavcopy solves it. NSE publishes one CSV per session covering the whole cash
market — ~2,600 EQ-series stocks with volume, turnover, close and previous
close. One download per day gives a complete, exact baseline.

    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip

Published after the close, so this runs end-of-day. That suits the signal: a
volume spike confirmed on the full session is what informs tomorrow, which is
what this dashboard was asked for in the first place.
"""
import csv
import datetime as dt
import io
import zipfile

import feeds

URL = ("https://nsearchives.nseindia.com/content/cm/"
       "BhavCopy_NSE_CM_0_0_0_{}_F_0000.csv.zip")
REFERER = "https://www.nseindia.com/all-reports"

# Ordinary equity only.
EQUITY_SERIES = {"EQ"}

# ...and company SHARES specifically. The series alone is not enough: ETF and
# mutual-fund units trade in the EQ series too (LIQGRWBEES, SBISILVER), and
# they spike on fund flows rather than on anything about a business. The ISIN
# separates them structurally rather than by guessing at names --
#   INE... = company equity        INF... = fund / ETF units
#   IN0/INF/IN9... = SGBs, debt, and the rest
ISIN_EQUITY_PREFIX = "INE"


def _log(msg):
    print("[bhavcopy] {}".format(msg), flush=True)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch(session_date):
    """
    One session's rows, or None if NSE has nothing for that date.

    A None is expected and not an error: weekends, exchange holidays, and the
    window after the close before the file is published all look the same from
    here.
    """
    ymd = session_date.strftime("%Y%m%d")
    sess = feeds._nse.ensure()
    try:
        r = sess.get(URL.format(ymd), headers={"Referer": REFERER}, timeout=60)
    except Exception as e:
        _log("{}: {}".format(ymd, e))
        return None

    if r.status_code != 200:
        return None
    if r.content[:2] != b"PK":
        # An HTML error page rather than the zip.
        return None

    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        raw = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    except Exception as e:
        _log("{}: could not read the archive ({})".format(ymd, e))
        return None

    rows = []
    for rec in csv.DictReader(io.StringIO(raw)):
        if (rec.get("SctySrs") or "").strip() not in EQUITY_SERIES:
            continue
        if not (rec.get("ISIN") or "").strip().upper().startswith(ISIN_EQUITY_PREFIX):
            continue   # ETF / fund unit / SGB, not a company
        symbol = (rec.get("TckrSymb") or "").strip().upper()
        volume = _num(rec.get("TtlTradgVol"))
        if not symbol or not volume:
            continue
        turnover = _num(rec.get("TtlTrfVal"))
        rows.append({
            "symbol": symbol,
            "session_date": session_date,
            "close": _num(rec.get("ClsPric")),
            "prev_close": _num(rec.get("PrvsClsgPric")),
            "volume": int(volume),
            # TtlTrfVal is in RUPEES; 1 crore = 10,000,000.
            "turnover_cr": round(turnover / 1e7, 4) if turnover else None,
            "trades": int(_num(rec.get("TtlNbOfTxsExctd")) or 0),
        })
    return rows


def recent_sessions(back_days):
    """Candidate dates, newest first. Weekends skipped; holidays just 404."""
    today = dt.date.today()
    out = []
    for i in range(back_days):
        d = today - dt.timedelta(days=i)
        if d.weekday() < 5:
            out.append(d)
    return out

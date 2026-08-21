"""
market.py — live NSE quotes, cached.

Powers the dashboard's Market view: last price, volume and turnover that move
as NSE moves. Separate from everything else in this service, which reads
filings — this is the "what is the stock doing right now?" half.

WHAT NSE ACTUALLY SERVES (probed 2026-08-21):

  /api/marketStatus              200  open/closed + the live Nifty 50 level
  /api/live-analysis-variations  200  per-stock ltp / volume / turnover for the
                                      day's movers, bucketed by index
  /api/equity-stockIndices       404  the full ~250-row table. Renamed in NSE's
                                      Next.js rewrite; no public replacement
                                      found, so the complete table cannot be
                                      mirrored.
  /api/quote-equity              403  per-symbol quotes are bot-protected, so a
                                      live price cannot be fetched for an
                                      arbitrary company on demand.

The consequence worth knowing: this covers the stocks that are MOVING, not
every listed stock, and an alerted company only shows a live price if it
happens to be among them.

CACHING IS NOT OPTIONAL. Every viewer's browser polls this endpoint, and
without a server-side cache each poll would become a request to NSE from the
same server IP that our scraper already uses. One cached fetch serves everyone.
"""
import threading
import time
from datetime import datetime

import feeds
from units import to_crore

_LOCK = threading.Lock()
_CACHE = {}          # key -> (fetched_at, payload)

# Seconds a cached response stays fresh. The browser may poll faster; it just
# gets the cache. Live prices move continuously, but a dashboard is not a
# trading terminal — 20s is plenty and keeps us to ~3 requests/minute.
TTL_OPEN_SEC = 20
# Outside market hours nothing changes, so hold the last snapshot far longer
# rather than politely re-asking NSE for the same numbers all night.
TTL_CLOSED_SEC = 600

NSE_REFERER = "https://www.nseindia.com/market-data/live-equity-market"

# The buckets live-analysis-variations returns, in the order we merge them.
_BUCKETS = ("NIFTY", "BANKNIFTY", "NIFTYNEXT50", "FOSec", "SecGtr20", "SecLwr20", "allSec")


def _log(msg):
    print("[market] {}".format(msg), flush=True)


def _get(path, params=None):
    sess = feeds._nse.ensure()
    headers = {"Accept": "application/json, text/plain, */*", "Referer": NSE_REFERER}
    for attempt in (1, 2):
        try:
            r = sess.get("https://www.nseindia.com" + path, params=params or {},
                         headers=headers, timeout=15)
        except Exception as e:
            _log("{} failed: {}".format(path, e))
            return None
        if r.status_code in (401, 403) and attempt == 1:
            feeds._nse.refresh()
            sess = feeds._nse.ensure()
            continue
        if r.status_code != 200:
            _log("{}: HTTP {}".format(path, r.status_code))
            return None
        try:
            return r.json()
        except Exception:
            return None
    return None


def market_status():
    """Whether the cash market is open, plus the live Nifty 50."""
    data = _get("/api/marketStatus")
    out = {"is_open": False, "as_of": None, "nifty": None,
           "change": None, "pct_change": None}
    if not data:
        return out
    for m in data.get("marketState") or []:
        if (m.get("market") or "").lower().startswith("capital"):
            out["is_open"] = (m.get("marketStatus") or "").lower() == "open"
            out["as_of"] = m.get("tradeDate")
            out["nifty"] = m.get("last") or None
            out["change"] = m.get("variation") or None
            out["pct_change"] = m.get("percentChange") or None
            break
    return out


def _row(raw, bucket):
    """
    One NSE row, normalised.

    `turnover` arrives in LAKHS — cross-checked against price x quantity:
    POWERGRID at 14,320,706 shares x Rs 272.30 is Rs 390 Cr, reported as
    38,908. Converted here with the same helper the filings use, so there is
    one place in this codebase that knows what a lakh is.
    """
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    volume = num(raw.get("trade_quantity"))
    return {
        "symbol": (raw.get("symbol") or "").upper(),
        "bucket": bucket,
        "ltp": num(raw.get("ltp")),
        "open": num(raw.get("open_price")),
        "high": num(raw.get("high_price")),
        "low": num(raw.get("low_price")),
        "prev_close": num(raw.get("prev_price")),
        "change": num(raw.get("net_price")),
        "pct_change": num(raw.get("perChange")),
        "volume": int(volume) if volume is not None else None,
        "turnover_cr": to_crore(num(raw.get("turnover")), "lakh"),
    }


def _fetch_movers():
    """Every distinct stock NSE is reporting movement for, gainers and losers."""
    by_symbol = {}
    for which in ("gainers", "losers"):
        data = _get("/api/live-analysis-variations", {"index": which})
        if not data:
            continue
        for bucket in _BUCKETS:
            block = data.get(bucket)
            if not isinstance(block, dict):
                continue
            for raw in block.get("data") or []:
                row = _row(raw, bucket)
                if not row["symbol"]:
                    continue
                row["direction"] = which
                # A symbol can appear in several buckets; keep the first, which
                # is the narrowest index it belongs to.
                by_symbol.setdefault(row["symbol"], row)
    return list(by_symbol.values())


def snapshot(force=False):
    """
    Live market snapshot, served from cache unless it has gone stale.

    Returns {"status": ..., "stocks": [...], "fetched_at": ..., "stale": bool}.
    On an NSE failure the previous snapshot is returned with stale=True rather
    than an error — a dashboard showing slightly old prices, clearly marked, is
    more useful than one showing nothing.
    """
    with _LOCK:
        cached = _CACHE.get("snapshot")
        if cached and not force:
            age = time.time() - cached[0]
            ttl = TTL_OPEN_SEC if cached[1].get("status", {}).get("is_open") else TTL_CLOSED_SEC
            if age < ttl:
                out = dict(cached[1])
                out["cache_age_sec"] = round(age, 1)
                return out

        status = market_status()
        stocks = _fetch_movers()

        if not stocks and cached:
            out = dict(cached[1])
            out["stale"] = True
            out["cache_age_sec"] = round(time.time() - cached[0], 1)
            _log("NSE returned nothing; serving the previous snapshot")
            return out

        stocks.sort(key=lambda s: (s["turnover_cr"] or 0), reverse=True)
        payload = {
            "status": status,
            "stocks": stocks,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "stale": False,
            "cache_age_sec": 0.0,
            "ttl_sec": TTL_OPEN_SEC if status.get("is_open") else TTL_CLOSED_SEC,
        }
        _CACHE["snapshot"] = (time.time(), payload)
        return payload

"""
marketcap.py — how big is this company, in rupees crore.

Used as a floor: rules 1-3 score a FILING, and a filing says nothing about the
size of the business behind it. A shell company reporting 100% profit growth on
Rs 1 crore of revenue clears rule 1 exactly as a real business does. The market
cap is what separates them.

WHERE THE NUMBER COMES FROM

NSE does not publish one usably. Its Bhavcopy has price but no share count;
`/api/market-data-pre-open` carries a `marketCap` field that is empty ("-") for
all 2,170 symbols; `/api/quote-equity`, which does have it, is bot-protected
(403). BSE's `StockTrading` endpoint returns `MktCapFull` in rupees crore, and
we already carry BSE's scrip master for the filings feed, so every symbol we
alert on can be resolved through it. All ten symbols from a live alert batch
resolved, including a Rs 22 crore microcap.

FETCHED LAZILY. Only symbols that are about to raise an alert are looked up -
a handful a day - rather than the whole 2,600-stock market. Results are cached
in Postgres and refreshed on a TTL.

The cached figure drifts with the share price between refreshes. That is
acceptable for a floor: it decides "bigger than Rs 100 crore?", not what to
display, and a company sitting exactly on the boundary is not one whose
classification a few days of price movement should be trusted to settle anyway.
"""
import re

import requests

import config
import db
import feeds

URL = "https://api.bseindia.com/BseIndiaAPI/api/StockTrading/w"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
}

_SYMBOL_TO_SCRIP = None


def _log(msg):
    print("[mcap] {}".format(msg), flush=True)


# BSE's own company search. The frozen scrip master cannot know about anything
# listed since it was generated, and a symbol missing from it had no scrip, so
# no market cap, so the size floor was never applied to it at all.
_SEARCH_URL = "https://api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w"
_LICLICK_RE = re.compile(r"liclick\('(\d+)'\s*,\s*'([^']*)'", re.IGNORECASE)
_TAGS_RE = re.compile(r"<[^>]+>")

# Memo across the process, negatives included, so a symbol BSE does not know is
# asked about once rather than on every filing it ever makes.
_SEARCHED = {}


def search_scrip(company_name, symbol=""):
    """
    Resolve a company to its BSE scrip code by asking BSE.

    Searched on the company NAME, because BSE's search matches names rather
    than tickers - "SATANIBRG" returns nothing, "Satani Bearings" returns
    505703. Returns None unless the answer looks unambiguous.
    """
    key = (company_name or "").strip().upper()
    if not key:
        return None
    if key in _SEARCHED:
        return _SEARCHED[key]

    scrip = None
    try:
        r = requests.get(_SEARCH_URL, params={"Type": "EQ", "text": company_name},
                         headers=HEADERS, timeout=15)
        body = r.text
        try:
            parsed = r.json()
            if isinstance(parsed, str):
                body = parsed
        except Exception:
            pass

        hits = []
        for chunk in body.split("<li")[1:]:
            m = _LICLICK_RE.search(chunk)
            if m:
                flat = _TAGS_RE.sub("", chunk).replace("&nbsp;", " ")
                hits.append((m.group(1), flat))

        want = (symbol or "").upper().replace(" ", "")
        # A result that shows OUR ticker is unambiguous; failing that, accept a
        # single result. Two results and no ticker match is a guess, and a wrong
        # scrip means a wrong market cap, which is worse than none.
        for code, flat in hits:
            if want and want in flat.upper().replace(" ", "").replace("&NBSP;", ""):
                scrip = code
                break
        else:
            if len(hits) == 1:
                scrip = hits[0][0]
    except Exception as e:
        _log("scrip search failed for {}: {}".format(company_name, e))

    _SEARCHED[key] = scrip
    if scrip:
        _log("resolved {} ({}) to BSE scrip {}".format(
            symbol or "?", company_name, scrip))
    return scrip


def symbol_to_scrip(symbol, company_name=None):
    """
    Ticker -> BSE scrip code.

    The map the BSE feed already uses first; then BSE's own search, which is
    what covers companies listed after that map was frozen.
    """
    global _SYMBOL_TO_SCRIP
    if _SYMBOL_TO_SCRIP is None:
        _SYMBOL_TO_SCRIP = {v: k for k, v in feeds.scrip_map().items()}
    known = _SYMBOL_TO_SCRIP.get((symbol or "").upper())
    if known:
        return known
    return search_scrip(company_name, symbol) if company_name else None


def _fetch(symbol, company_name=None):
    """Live market cap in Rs crore, or None."""
    scrip = symbol_to_scrip(symbol, company_name)
    if not scrip:
        return None, None
    try:
        r = requests.get(URL, params={"flag": "", "quotetype": "EQ",
                                      "scripcode": scrip, "seriesid": ""},
                         headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None, scrip
        raw = (r.json() or {}).get("MktCapFull")
    except Exception as e:
        _log("{}: {}".format(symbol, e))
        return None, scrip

    if raw in (None, "", "-"):
        return None, scrip
    try:
        # Indian grouping: "17,78,175.59" -> 1778175.59, already in crore.
        return float(str(raw).replace(",", "").strip()), scrip
    except ValueError:
        return None, scrip


def get(symbol, company_name=None):
    """
    Market cap in Rs crore for one symbol, from cache or freshly fetched.

    Returns None when it genuinely cannot be determined — an NSE-only listing
    with no BSE scrip, or BSE unreachable. Callers must treat None as UNKNOWN
    and let the alert through: losing a real alert to a data gap is worse than
    letting a small company past the floor, and the reason is recorded either
    way.
    """
    cached = db.fetch_market_cap(symbol, config.MARKET_CAP_TTL_DAYS)
    if cached is not None:
        return cached

    value, scrip = _fetch(symbol, company_name)
    if value is not None:
        db.save_market_cap(symbol, value, scrip)
    return value


def passes_floor(symbol, company_name=None):
    """
    (allowed, market_cap_cr, reason).

    `allowed` is False only when we KNOW the company is below the floor.
    """
    floor = config.MIN_MARKET_CAP_CR
    if floor <= 0:
        return True, None, "market-cap floor disabled"

    cap = get(symbol, company_name)
    if cap is None:
        return True, None, "market cap unknown, allowed through"
    if cap < floor:
        return False, cap, "market cap Rs {:,.0f} Cr is below the Rs {:,.0f} Cr floor".format(
            cap, floor)
    return True, cap, "market cap Rs {:,.0f} Cr".format(cap)

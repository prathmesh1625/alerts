"""
feeds.py — read the NSE and BSE corporate-announcement feeds.

Ported from the two production scrapers in shares/ (`scraper/services/
fetchNseGlobal.js` + `nseSession.js`, and `bse-scraper/services/
fetchBseFeed.js`), because their hard-won details are what make these endpoints
answer 200 instead of 401 — notably NSE's cookie priming.

This deliberately does much LESS than those scrapers: it fetches the FEED only
and never downloads a PDF. The production scraper must download everything
because the bot delivers PDFs to users; the alert engine reads roughly an
eighth of them, and fetches those on demand (pdf_fetch.py). That is what keeps
a second scraper from doubling the load on NSE from the same server IP.
"""
import json
import os
import threading
import time
from datetime import datetime

import requests

import config

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _log(msg):
    print("[feeds] {}".format(msg), flush=True)


# -----------------------------------------------------------------------------
#  NSE
# -----------------------------------------------------------------------------

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
NSE_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"


class NseSession:
    """
    Holds NSE's session cookies.

    NSE's API rejects requests that don't carry the cookies you only get by
    first loading the website — without them the endpoint intermittently
    returns 401/403 or an HTML challenge. Same approach as the production
    scraper's nseSession.js: prime from the homepage, cache, refresh on
    expiry or on an auth failure.
    """

    REFRESH_EVERY_SEC = 5 * 60

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(BROWSER_HEADERS)
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    def refresh(self):
        with self._lock:
            try:
                r = self._session.get(NSE_HOME, timeout=12)
                if r.cookies:
                    self._last_refresh = time.time()
                    _log("NSE cookies refreshed ({} cookies)".format(len(r.cookies)))
                else:
                    _log("NSE cookie refresh returned no cookies")
            except Exception as e:
                _log("NSE cookie refresh failed: {}".format(e))

    def ensure(self):
        if time.time() - self._last_refresh > self.REFRESH_EVERY_SEC:
            self.refresh()
        return self._session


_nse = NseSession()


def _nse_page(page_no):
    """One page of NSE's global feed, newest first. [] on any failure."""
    sess = _nse.ensure()
    headers = {"Accept": "application/json, text/plain, */*", "Referer": NSE_REFERER}

    for attempt in (1, 2):
        try:
            r = sess.get(NSE_API, params={"index": "equities", "pageNo": page_no},
                         headers=headers, timeout=15)
        except Exception as e:
            _log("NSE page {} failed: {}".format(page_no, e))
            return []

        # Auth or challenge — re-prime once and retry, exactly as production does.
        if r.status_code in (401, 403) and attempt == 1:
            _nse.refresh()
            sess = _nse.ensure()
            continue

        if r.status_code != 200:
            _log("NSE page {}: status {}".format(page_no, r.status_code))
            return []

        try:
            data = r.json()
        except Exception:
            _log("NSE page {}: non-JSON body (challenge page?)".format(page_no))
            return []

        return data if isinstance(data, list) else []
    return []


def _clean(v):
    return str(v or "").strip()


def fetch_nse(pages=None):
    """
    Recent NSE filings across ALL companies, newest first.

    Field names are NSE's own: `symbol`, `desc` (the subject line),
    `attchmntFile` (the PDF), `sort_date`.
    """
    pages = pages or config.NSE_FEED_PAGES
    out = []
    for page in range(1, pages + 1):
        for item in _nse_page(page):
            symbol = _clean(item.get("symbol")).upper()
            pdf_url = _clean(item.get("attchmntFile"))
            if not symbol or not pdf_url:
                continue
            out.append({
                "company_symbol": symbol,
                "company_name": _clean(item.get("sm_name")) or symbol,
                "title": _clean(item.get("desc")),
                "pdf_url": pdf_url,
                "announced_at": _parse_dt(item.get("sort_date") or item.get("an_dt")),
                "exchange": "NSE",
            })
    return out


# -----------------------------------------------------------------------------
#  BSE
# -----------------------------------------------------------------------------

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_REFERER = "https://www.bseindia.com/corporates/ann.html"

_SCRIP_MAP = None


def scrip_map():
    """
    BSE scrip code -> ticker.

    BSE's feed identifies companies by numeric scrip code only, while
    everything else here is keyed on the ticker. Generated by inverting the
    scraper's own `config/bseCompanies.js` scrip master, so a company's NSE and
    BSE filings land under one symbol.
    """
    global _SCRIP_MAP
    if _SCRIP_MAP is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "bse_scrips.json")
        try:
            with open(path, encoding="utf-8") as fh:
                _SCRIP_MAP = json.load(fh)
        except Exception as e:
            _log("could not load BSE scrip map ({}) - BSE rows will be skipped".format(e))
            _SCRIP_MAP = {}
    return _SCRIP_MAP


def _bse_page(day, page_no):
    """One page of BSE's announcements for a given yyyymmdd. [] on failure."""
    try:
        r = requests.get(
            BSE_API,
            params={
                "pageno": page_no, "strCat": "-1",
                "strPrevDate": day, "strToDate": day,
                "strScrip": "", "strSearch": "P", "strType": "C",
                "subcategory": "-1",
            },
            headers={**BROWSER_HEADERS,
                     "Accept": "application/json, text/plain, */*",
                     "Referer": BSE_REFERER},
            timeout=15,
        )
    except Exception as e:
        _log("BSE {} page {} failed: {}".format(day, page_no, e))
        return []

    if r.status_code != 200:
        _log("BSE {} page {}: status {}".format(day, page_no, r.status_code))
        return []
    try:
        data = r.json()
    except Exception:
        return []
    table = data.get("Table")
    return table if isinstance(table, list) else []


def fetch_bse(pages=None, day=None):
    """Recent BSE filings across all companies, mapped onto tickers."""
    pages = pages or config.BSE_FEED_PAGES
    day = day or datetime.now().strftime("%Y%m%d")
    mapping = scrip_map()

    out = []
    for page in range(1, pages + 1):
        rows = _bse_page(day, page)
        if not rows:
            break
        for row in rows:
            attachment = _clean(row.get("ATTACHMENTNAME"))
            scrip = _clean(row.get("SCRIP_CD"))
            symbol = mapping.get(scrip)
            if not attachment or not symbol:
                # No attachment, or a scrip we cannot resolve to a ticker.
                continue
            out.append({
                "company_symbol": symbol.upper(),
                "company_name": _clean(row.get("SLONGNAME")) or symbol,
                "title": _clean(row.get("NEWSSUB")) or _clean(row.get("HEADLINE")),
                "pdf_url": BSE_ATTACHMENT_BASE + attachment,
                "announced_at": _parse_dt(
                    row.get("NEWS_DT") or row.get("DT_TM")
                    or row.get("News_submission_dt")),
                "exchange": "BSE",
            })
    return out


# -----------------------------------------------------------------------------

_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
)


def _parse_dt(value):
    """Feed timestamp -> datetime. Falls back to now() rather than dropping a row."""
    raw = _clean(value)
    if raw:
        for fmt in _DT_FORMATS:
            try:
                return datetime.strptime(raw[:26], fmt)
            except ValueError:
                continue
    return datetime.now()


def fetch_all():
    """Both feeds. A failure in one must not lose the other."""
    rows = []
    for name, fn in (("NSE", fetch_nse), ("BSE", fetch_bse)):
        try:
            got = fn()
            rows.extend(got)
            _log("{}: {} filing(s)".format(name, len(got)))
        except Exception as e:
            _log("{} feed failed: {}".format(name, e))
    return rows

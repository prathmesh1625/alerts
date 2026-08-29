"""
pdf_fetch.py — get a filing's PDF, downloading it only if we actually need it.

This is what lets the alert stack run independently of the production scrapers.
Their scraper downloads every PDF because the bot delivers them to users; we
read roughly an eighth of them, so we fetch on demand — after the title screen
has already discarded the routine filings.

The consequence is that this service needs no shared volume: given a `pdf_url`
it can work from an empty disk. When it IS deployed alongside a scraper, a
local file is still preferred and nothing is downloaded at all.

Downloaded files land in a small cache that is pruned by age, so disk use stays
bounded — this is a working set, not an archive. The scrapers own the archive.
"""
import os
import time

import requests

import config

# NSE and BSE both reject PDF requests without a plausible browser referer.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/pdf,*/*",
    "Connection": "keep-alive",
}


def _log(msg):
    print("[pdf] {}".format(msg), flush=True)


def _referer(url):
    return ("https://www.bseindia.com/" if "bseindia.com" in (url or "")
            else "https://www.nseindia.com/")


def cache_path(pdf_url):
    """Stable local path for a URL, namespaced by a hash of the whole URL."""
    import hashlib
    digest = hashlib.sha1((pdf_url or "").encode("utf-8")).hexdigest()[:20]
    base = os.path.basename((pdf_url or "").split("?")[0]) or "filing.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    # Prefix with the digest so two filings sharing a basename cannot collide.
    return os.path.join(config.PDF_CACHE_DIR, "{}_{}".format(digest, base[-60:]))


def download(pdf_url, timeout=None):
    """
    Fetch a PDF to the cache and return its path, or None.

    Returns None rather than raising for the ordinary failures — a 404 on a
    withdrawn filing, or a PDF the exchange has not published to its CDN yet —
    because the caller records those as a settled outcome, not a crash.
    """
    if not pdf_url:
        return None

    dest = cache_path(pdf_url)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest

    # (connect, read). The read half applies BETWEEN BYTES, so a large PDF
    # arriving steadily is never cut off — only a connection that has stopped
    # saying anything, which is what an unpublished filing on the BSE CDN looks
    # like. A single long timeout here is paid again on every retry.
    if timeout is None:
        timeout = (config.PDF_CONNECT_TIMEOUT_SEC, config.PDF_DOWNLOAD_TIMEOUT_SEC)
    try:
        r = requests.get(
            pdf_url,
            headers={**_HEADERS, "Referer": _referer(pdf_url)},
            timeout=timeout,
            stream=True,
        )
    except Exception as e:
        _log("download failed {}: {}".format(pdf_url[-60:], e))
        return None

    if r.status_code != 200:
        _log("download {}: HTTP {}".format(pdf_url[-60:], r.status_code))
        return None

    # Guard against saving an HTML error page as a .pdf — that would later
    # surface as the confusing "No /Root object!" parse failure.
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" in ctype:
        _log("download {}: got HTML, not a PDF".format(pdf_url[-60:]))
        return None

    os.makedirs(config.PDF_CACHE_DIR, exist_ok=True)
    tmp = dest + ".part"
    size = 0
    try:
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > config.PDF_MAX_BYTES:
                    raise ValueError("exceeds PDF_MAX_BYTES ({})".format(config.PDF_MAX_BYTES))
                fh.write(chunk)
        if size == 0:
            raise ValueError("empty body")
        os.replace(tmp, dest)
    except Exception as e:
        _log("download {}: {}".format(pdf_url[-60:], e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    return dest


def prune_cache(max_age_hours=None):
    """
    Delete cached PDFs older than the retention window.

    The cache is a working set: once a filing is analysed its verdict lives in
    the database, and the PDF is only needed again if someone opens it from the
    dashboard — which re-downloads. Without this the disk grows without bound.
    """
    max_age = (max_age_hours or config.PDF_CACHE_HOURS) * 3600
    if not os.path.isdir(config.PDF_CACHE_DIR):
        return 0

    cutoff = time.time() - max_age
    removed = 0
    for name in os.listdir(config.PDF_CACHE_DIR):
        path = os.path.join(config.PDF_CACHE_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        _log("pruned {} cached PDF(s) older than {}h".format(
            removed, max_age_hours or config.PDF_CACHE_HOURS))
    return removed

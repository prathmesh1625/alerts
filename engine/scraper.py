"""
scraper.py — the standalone feed poller.

Only used when STANDALONE=true. It polls the NSE and BSE announcement feeds and
inserts one `announcements` row per filing — and nothing else. It does not
download PDFs; the agent does that on demand for the ~12% of filings that
survive the title screen and the prefilter (see pdf_fetch.py).

That division is the whole reason this can run beside a production scraper
without endangering it:

    production scraper   feed every 20s, 3 pages, downloads EVERY PDF
    this one             feed every 180s, 2 pages, downloads nothing

roughly a thirteenth of the feed traffic, from the same server IP, which is
what keeps NSE's rate limiting out of the picture.

Run:  python scraper.py            (loop)
      python scraper.py --once     (one poll, then exit)
"""
import argparse
import sys
import time
import traceback

import config
import db
import feeds


def run_cycle() -> tuple:
    """One poll of both feeds. Returns (seen, inserted)."""
    rows = feeds.fetch_all()
    if not rows:
        return 0, 0

    inserted = 0
    for row in rows:
        try:
            # ON CONFLICT (pdf_url) DO NOTHING is the dedup: re-reading the same
            # page of the feed every cycle is free, and a restart that re-scans
            # the day cannot create duplicates.
            if db.save_announcement(row):
                inserted += 1
        except Exception as e:
            print("[scraper] insert failed for {}: {}".format(
                row.get("company_symbol"), e), file=sys.stderr)

    return len(rows), inserted


def main() -> None:
    ap = argparse.ArgumentParser(description="Standalone NSE/BSE feed poller")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    args = ap.parse_args()

    print("=" * 66, flush=True)
    print("Feed Scraper starting", flush=True)
    print("  database : {}@{}:{}/{}".format(
        config.DB_USER, config.DB_HOST, config.DB_PORT, config.DB_NAME), flush=True)
    print("  interval : {}s".format(config.SCRAPE_INTERVAL_SEC), flush=True)
    print("  pages    : NSE {} | BSE {}".format(
        config.NSE_FEED_PAGES, config.BSE_FEED_PAGES), flush=True)
    print("  PDFs     : not downloaded here - fetched on demand by the agent",
          flush=True)
    print("=" * 66, flush=True)

    db.ensure_announcements_schema()

    while True:
        started = time.time()
        try:
            seen, inserted = run_cycle()
            print("[scraper] {} filing(s) seen, {} new ({:.1f}s)".format(
                seen, inserted, time.time() - started), flush=True)
        except Exception as e:
            print("[scraper] cycle error: {}".format(e), file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        if args.once:
            return
        time.sleep(config.SCRAPE_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[scraper] stopped", flush=True)

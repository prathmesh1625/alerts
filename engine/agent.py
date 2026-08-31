"""
agent.py — the alert worker.

Polls the scraper's `announcements` table for freshly downloaded PDFs, reads
each one, applies the formula, and writes qualifying filings into
`stock_alerts` for the dashboard.

Per filing:

    resolve path -> extract text -> prefilter -> gpt-4o-mini -> score -> store

Every filing ends up with a `filing_analyses` row whatever happens (ANALYZED /
SKIPPED / FAILED), which is what makes the loop idempotent: restart the worker
as often as you like and it will not re-read, re-bill, or re-alert a PDF it has
already settled.

Run:  python agent.py            (loop forever)
      python agent.py --once     (one pass, then exit — useful in cron/CI)
"""
import argparse
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
import extractor
import marketcap
import pdf_fetch
import scoring
from pdf_text import document_fingerprint, extract_text_from_pdf_file
from prefilter import should_analyze, should_open_pdf


# -----------------------------------------------------------------------------
#  Locating the PDF on disk
# -----------------------------------------------------------------------------

def resolve_pdf_path(local_path):
    """
    Turn the scraper's `local_path` into a path we can actually open.

    The column holds a path relative to the scraper's working directory
    ("storage/pdf/TCS_2024-01-01.pdf"), so it needs SCRAPER_BASE_PATH in front
    of it. Absolute paths are passed through, and as a last resort we look the
    basename up directly in PDF_STORAGE_PATH — which is what saves us when the
    scraper and this service disagree about their base directory (the usual
    cause of "the file is right there but the worker can't find it").
    """
    if not local_path:
        return None

    raw = str(local_path).strip().replace("\\", "/")
    if not raw:
        return None

    candidates = []
    if os.path.isabs(raw):
        candidates.append(raw)
    else:
        candidates.append(os.path.join(config.SCRAPER_BASE_PATH, raw))
        candidates.append(raw)

    if config.PDF_STORAGE_PATH:
        candidates.append(os.path.join(config.PDF_STORAGE_PATH, os.path.basename(raw)))

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# -----------------------------------------------------------------------------
#  Permanent vs transient failure
# -----------------------------------------------------------------------------

# A truncated or corrupt PDF will never parse, no matter how often it is tried.
# Retrying it burns a slot in every cycle forever (observed on real data: two
# filings whose bytes are not a PDF at all came back "No /Root object!" on each
# pass). Those are recorded as SKIPPED — a settled, explained outcome — while a
# 429 or a dropped connection stays FAILED and is retried.
_PERMANENT_MARKERS = (
    "no /root object",
    "is this really a pdf",
    "eof marker not found",
    "cannot read an empty file",
    "file has not been decrypted",
    "invalid elementary object",
    "stream has ended unexpectedly",
)


def is_permanent_failure(e: Exception) -> bool:
    """True when re-reading this document could not possibly succeed."""
    name = type(e).__name__.lower()
    if any(k in name for k in ("pdfread", "pdfminer", "emptyfile", "deprecationerror")):
        return True
    msg = str(e).lower()
    return any(m in msg for m in _PERMANENT_MARKERS)


# -----------------------------------------------------------------------------
#  One filing
# -----------------------------------------------------------------------------

def process_filing(filing: dict) -> str:
    """
    Analyse one filing end to end. Returns a short status string for logging.

    Never raises: the DB write that records what happened is the whole point of
    this function, so a failure has to be caught and recorded rather than
    propagated into the pool.
    """
    ann_id = filing["announcement_id"]
    symbol = filing["company_symbol"]
    title = filing.get("title") or ""
    local_path = filing.get("local_path")
    # In standalone mode there is no local_path; the URL's basename is the
    # stable identifier for the document instead.
    file_key = os.path.basename(str(local_path or filing.get("pdf_url") or "")) or None

    try:
        # Cheapest possible check first, and it now comes before we even LOOK
        # for the file: a regulation-defined caption like "Trading Window
        # closure" can never carry a results statement or an order win, so
        # there is no reason to parse the PDF — or, in standalone mode, to
        # download it at all. This is the difference between handling every
        # filing on the feed and handling only the plausible ones.
        open_it, why = should_open_pdf(title)
        if not open_it:
            db.record_analysis(ann_id, symbol, file_key, "SKIPPED", skip_reason=why)
            return "{} SKIP  {}".format(symbol, why)

        path = resolve_pdf_path(local_path)

        if not path:
            # No local copy. With a scraper sharing its volume that means the
            # file is genuinely missing; standing alone it is the normal case,
            # because nothing has downloaded it yet.
            path = pdf_fetch.download(filing.get("pdf_url"))

        if not path:
            # A download that failed is usually NOT terminal. BSE lists a filing
            # seconds before its PDF reaches the CDN — bse-scraper retries this
            # 8 times for exactly that reason — so recording it as SKIPPED would
            # permanently discard filings that appear a minute later.
            #
            # Retryable when we were meant to fetch it ourselves; genuinely
            # missing when a scraper claimed to have downloaded it and the file
            # is not there, which no amount of waiting will fix.
            if filing.get("pdf_url") and not local_path:
                db.record_analysis(
                    ann_id, symbol, file_key, "FAILED",
                    error="PDF not retrievable yet ({})".format(
                        (filing.get("pdf_url") or "")[-70:]),
                    bump_retry=True,
                )
                return "{} WAIT  PDF not on the CDN yet".format(symbol)

            db.record_analysis(
                ann_id, symbol, file_key, "SKIPPED",
                skip_reason="PDF missing on disk (local_path={})".format(local_path),
            )
            return "{} SKIP  PDF missing".format(symbol)

        text = extract_text_from_pdf_file(path)

        ok, reason = should_analyze(title, text)
        if not ok:
            db.record_analysis(ann_id, symbol, file_key, "SKIPPED", skip_reason=reason)
            return "{} SKIP  {}".format(symbol, reason)

        # NSE and BSE both publish the same filing, under different pdf_urls, so
        # the scraper holds two rows for one event. Claim the document before
        # paying for it: whoever claims it first analyses it, the other copy
        # stops here rather than billing us twice and putting a duplicate card
        # on the dashboard. A scanned filing yields no fingerprint and is simply
        # not deduped.
        fingerprint = document_fingerprint(text)
        if fingerprint:
            owner = db.claim_document(symbol, fingerprint, ann_id,
                                      filing.get("announced_at"))
            if owner != ann_id:
                db.record_analysis(
                    ann_id, symbol, file_key, "SKIPPED",
                    fingerprint=fingerprint,
                    skip_reason="duplicate of announcement {} (same document from "
                                "the other exchange)".format(owner),
                )
                return "{} DUP   same document as #{}".format(symbol, owner)

        signals = extractor.extract_signals(text, symbol=symbol, title=title)
        result = scoring.score_filing(signals, text, title)

        db.record_analysis(
            ann_id, symbol, file_key, "ANALYZED",
            document_type=signals.document_type,
            score=result["score"],
            raw_signals=extractor.signals_to_json(signals),
            fingerprint=fingerprint or None,
        )

        # Safety net. The prefilter catches presentations and transcripts by
        # title and by document shape, but a deck with an uninformative title
        # and few slide markers can still get this far - and it would alert on
        # figures the screen already reported when the results were filed.
        # The model has read the whole document by now, so its own
        # classification is the better judge at this point.
        if (signals.document_type or "").upper() in ("PRESENTATION", "TRANSCRIPT"):
            return "{} ---   {} restates published results".format(
                symbol, signals.document_type.lower())

        # Size floor. A filing says nothing about how big the business behind
        # it is: a shell company reporting 100% profit growth on Rs 1 crore of
        # revenue clears rule 1 exactly as a real one does. Checked only for
        # filings that would otherwise ALERT, so it costs one lookup on a
        # handful of symbols a day rather than on every filing.
        if result["qualifies"]:
            big_enough, cap_cr, why = marketcap.passes_floor(symbol)
            if not big_enough:
                db.record_analysis(
                    ann_id, symbol, file_key, "ANALYZED",
                    document_type=signals.document_type,
                    score=result["score"],
                    raw_signals=extractor.signals_to_json(signals),
                    fingerprint=fingerprint or None,
                    skip_reason=why,
                )
                return "{} ---   {} (score {:.1f})".format(symbol, why, result["score"])
            result["market_cap_cr"] = cap_cr

        if not result["qualifies"]:
            return "{} ---   score {:.1f} (below {:.0f})".format(
                symbol, result["score"], config.ALERT_MIN_SCORE
            )

        db.save_alert({
            "announcement_id": ann_id,
            "company_symbol": symbol,
            "company_name": signals.company_name or symbol,
            "title": title,
            "pdf_url": filing.get("pdf_url"),
            "local_path": local_path,
            "announced_at": filing.get("announced_at"),
            "document_type": signals.document_type,
            "reporting_period": signals.reporting_period,
            "basis": signals.basis,
            "score": result["score"],
            "conviction": result["conviction"],
            "rules_hit": result["rules_hit"],
            "profit_growth_pct": result["profit_growth_pct"],
            "revenue_growth_pct": result["revenue_growth_pct"],
            "order_value_cr": result["order_value_cr"],
            "headline": result["headline"],
            "breakdown": result["breakdown"],
            "evidence": signals.evidence,
            "exchange": filing.get("exchange") or "NSE",
            "market_cap_cr": result.get("market_cap_cr"),
        })
        return "{} ALERT {:.1f} {} - {}".format(
            symbol, result["score"], result["conviction"], result["headline"]
        )

    except Exception as e:
        permanent = is_permanent_failure(e)
        if not permanent:
            traceback.print_exc(file=sys.stderr)
        try:
            if permanent:
                db.record_analysis(
                    ann_id, symbol, file_key, "SKIPPED",
                    skip_reason="unreadable PDF ({}: {})".format(
                        type(e).__name__, e)[:500],
                )
            else:
                db.record_analysis(
                    ann_id, symbol, file_key, "FAILED",
                    error="{}: {}".format(type(e).__name__, e)[:1000],
                    bump_retry=True,
                )
        except Exception:
            pass  # DB itself is down; the next cycle will pick this filing up again
        return "{} {}  {}".format(symbol, "BAD  " if permanent else "FAIL ", e)


# -----------------------------------------------------------------------------
#  The loop
# -----------------------------------------------------------------------------

def run_cycle() -> int:
    """One pass over the pending queue. Returns how many filings were handled."""
    filings = db.fetch_unanalyzed(config.BATCH_SIZE)
    if not filings:
        return 0

    print("[agent] {} filing(s) to analyse".format(len(filings)))

    handled = 0
    with ThreadPoolExecutor(max_workers=config.WORKER_THREADS) as pool:
        futures = {pool.submit(process_filing, f): f for f in filings}
        for fut in as_completed(futures):
            try:
                print("  " + fut.result())
            except Exception as e:
                print("  worker crashed: {}".format(e), file=sys.stderr)
            handled += 1

    return handled


def main() -> None:
    parser = argparse.ArgumentParser(description="NSE filing alert agent")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    args = parser.parse_args()

    print("=" * 66)
    print("Alert Agent starting")
    print("  model     : {}".format(config.OPENAI_MODEL))
    print("  database  : {}@{}:{}/{}".format(
        config.DB_USER, config.DB_HOST, config.DB_PORT, config.DB_NAME))
    print("  pdf base  : {}".format(config.SCRAPER_BASE_PATH))
    print("  formula   : profit >= {:.0f}% YoY | revenue >= {:.0f}% YoY | orders >= Rs {:.0f} Cr".format(
        config.PROFIT_GROWTH_MIN_PCT, config.REVENUE_GROWTH_MIN_PCT, config.ORDER_MIN_CR))
    print("  alert at  : score >= {:.0f}".format(config.ALERT_MIN_SCORE))
    print("  prefilter : {}".format("on" if config.PREFILTER_ENABLED else "off"))
    print("=" * 66)

    db.ensure_schema()

    if args.once:
        n = run_cycle()
        print("[agent] cycle complete - {} filing(s) handled".format(n))
        return

    while True:
        started = time.time()
        n = 0
        try:
            n = run_cycle()
            if n:
                print("[agent] cycle complete - {} filing(s) in {:.1f}s".format(
                    n, time.time() - started))
        except Exception as e:
            print("[agent] cycle error: {}".format(e), file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            time.sleep(config.POLL_INTERVAL_SEC)
            continue

        # Keep the on-demand download cache bounded. Cheap, and a no-op when
        # nothing has been downloaded (i.e. when a scraper shares its volume).
        try:
            pdf_fetch.prune_cache()
        except Exception as e:
            print("[agent] cache prune failed: {}".format(e), file=sys.stderr)

        # A full batch means there is probably more waiting. Results days arrive
        # in bursts — dozens of companies file within minutes of each other — and
        # sleeping a full interval after every full batch would let that backlog
        # grow faster than we drain it. Go straight round again instead, and
        # only idle once the queue is actually short.
        if n >= config.BATCH_SIZE:
            continue
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[agent] stopped")

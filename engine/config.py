"""
config.py — every tunable for the alert engine in one place.

Anything that differs between dev and prod is an env var; anything that
encodes the *formula itself* is a constant here so the scoring rules are
reviewable in one screen rather than scattered across the worker.
"""
import os

from dotenv import load_dotenv

# override=True so the .env file WINS over whatever is already in the shell.
#
# Without it, a stale OPENAI_API_KEY exported in the machine's environment
# silently beats the one in .env — you paste a new key, nothing changes, and the
# calls keep going out on the old one. If you edited the file, you meant it.
#
# This is safe for Docker only because .env never reaches the image: see the
# .dockerignore at the build-context root. In the container, configuration comes
# from the compose `environment:` block, and there is no .env to override it.
load_dotenv(override=True)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "")


# ─────────────────────────────────────────────────────────────────────────────
#  Source database — the scraper's ingestion DB (read-only for us, except for
#  our own tables). Same connection the bot uses; see shares/docker-compose.yml.
# ─────────────────────────────────────────────────────────────────────────────
DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = _int("DB_PORT", 5433)
DB_NAME     = os.getenv("DB_NAME", "nse_ingestion")
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# The scraper's announcements table and its column names. These mirror
# bot/config.py — if the scraper ever renames a column, change it here too.
FILINGS_TABLE      = "announcements"
COL_ID             = "id"
COL_COMPANY_SYMBOL = "company_symbol"
COL_FILE_PATH      = "local_path"
COL_TITLE          = "title"
COL_PDF_URL        = "pdf_url"
COL_ANNOUNCED_AT   = "announcement_time"

# `local_path` is stored RELATIVE (e.g. "storage/pdf/TCS_2024-01-01.pdf").
# Join it onto this base to get a readable path. In Docker this is /app,
# with the scraper's shared_storage volume mounted at /app/storage.
SCRAPER_BASE_PATH = os.getenv("SCRAPER_BASE_PATH", r"D:\prathmesh\shares")

# Fallback: if local_path is null/stale but the PDF exists in the storage dir,
# look it up by basename here.
PDF_STORAGE_PATH = os.getenv("PDF_STORAGE_PATH", "")


# ─────────────────────────────────────────────────────────────────────────────
#  Standalone mode — our own scraper and our own PDF fetching
#
#  Set STANDALONE=true to run without the production scrapers: feeds.py polls
#  NSE and BSE directly, and PDFs are downloaded on demand from `pdf_url`
#  instead of read from a shared volume. That is what makes this stack
#  deployable on its own, with no coupling to the service your users depend on.
# ─────────────────────────────────────────────────────────────────────────────
STANDALONE = _bool("STANDALONE", False)

# How many pages of each global feed to pull per cycle. One page is ~50 filings
# on NSE, newest first.
NSE_FEED_PAGES = _int("NSE_FEED_PAGES", 2)
BSE_FEED_PAGES = _int("BSE_FEED_PAGES", 2)

# Seconds between feed polls. Deliberately far slower than the production
# scraper's 20s: that one races to deliver WhatsApp pushes, where speed is the
# product. A dashboard alert is not latency-critical, and polling gently keeps
# a second scraper on the same server IP well clear of NSE's rate limiting.
SCRAPE_INTERVAL_SEC = _int("SCRAPE_INTERVAL_SEC", 180)

# Where on-demand PDF downloads are cached. A working set, not an archive:
# pruned by age, because a filing's verdict lives in the database once analysed.
PDF_CACHE_DIR   = os.getenv("PDF_CACHE_DIR", "/tmp/alert_pdf_cache")
PDF_CACHE_HOURS = _int("PDF_CACHE_HOURS", 48)
PDF_DOWNLOAD_TIMEOUT_SEC = _int("PDF_DOWNLOAD_TIMEOUT_SEC", 60)
# Refuse absurdly large attachments rather than filling the disk with one file.
PDF_MAX_BYTES = _int("PDF_MAX_BYTES", 60 * 1024 * 1024)


# ─────────────────────────────────────────────────────────────────────────────
#  OpenAI
# ─────────────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_SEC = _int("LLM_TIMEOUT_SEC", 60)
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 3)

# Hard cap on PDF text sent to the model. gpt-4o-mini has a 128k context, but
# filings are front-loaded — the results table and the order paragraph are
# always in the first few pages — and capping keeps per-filing cost predictable.
MAX_PDF_CHARS = _int("MAX_PDF_CHARS", 60000)


# ─────────────────────────────────────────────────────────────────────────────
#  Cross-exchange duplicate detection (see pdf_text.document_fingerprint)
#
#  NSE and BSE publish the same filing under different pdf_urls, so the scraper
#  holds two rows for one event. We identify the DOCUMENT by hashing its text.
# ─────────────────────────────────────────────────────────────────────────────
# Minimum normalised (letters+digits only) characters needed to fingerprint.
# The bot uses 400, but it falls back to a subject-line key when a document is
# too short and we have no such fallback — and a measured, typical one-page
# order-win intimation flattens to ~360 characters, i.e. exactly the filings
# that would silently stop being deduped at 400.
FINGERPRINT_MIN_CHARS = _int("FINGERPRINT_MIN_CHARS", 200)

# Two announcements are only ever treated as the same document if they were
# filed within this many hours of each other. Cross-exchange copies arrive
# minutes apart, so this is generous — it exists to bound the damage if two
# genuinely different filings ever hash alike (possible when a PDF's text layer
# is nothing but a boilerplate cover letter), so a collision can never suppress
# an unrelated alert weeks later.
DEDUP_WINDOW_HOURS = _int("DEDUP_WINDOW_HOURS", 72)


# ─────────────────────────────────────────────────────────────────────────────
#  OCR fallback (see pdf_text.py)
#
#  Only reached when a filing's text layer is missing or mojibake — scanned
#  newspaper result cuttings and signed board-meeting outcomes. A normal
#  text-layer filing pays nothing for this.
#
#  This is the single most CPU-hungry thing the service can do: rasterising a
#  page at 300 DPI and running tesseract over it. On a small VPS, lower OCR_DPI
#  and OCR_MAX_PAGES before touching anything else, or set OCR_ENABLED=false to
#  give up scanned filings entirely in exchange for near-zero CPU spikes.
# ─────────────────────────────────────────────────────────────────────────────
OCR_ENABLED         = _bool("OCR_ENABLED", True)
OCR_DPI             = _int("OCR_DPI", 300)      # tesseract's sweet spot for print
OCR_LANGS           = os.getenv("OCR_LANGS", "eng")
OCR_MAX_PAGES       = _int("OCR_MAX_PAGES", 10)
OCR_TIME_BUDGET_SEC = _int("OCR_TIME_BUDGET_SEC", 45)


# ─────────────────────────────────────────────────────────────────────────────
#  THE FORMULA
#
#  Three independent rules. Each one that fires contributes
#      weight * (BASE_CREDIT + (1 - BASE_CREDIT) * strength)
#  where `strength` (0..1) is how far past its threshold the value landed.
#  So a filing that just scrapes the threshold banks BASE_CREDIT of the
#  weight, and a blowout banks all of it. Weights sum to 100.
# ─────────────────────────────────────────────────────────────────────────────

# Rule 1 — profit growth (PAT, year-over-year)
PROFIT_GROWTH_MIN_PCT = _float("PROFIT_GROWTH_MIN_PCT", 25.0)
PROFIT_WEIGHT         = _float("PROFIT_WEIGHT", 35.0)
# Growth at which this rule is considered maxed out.
PROFIT_GROWTH_FULL_PCT = _float("PROFIT_GROWTH_FULL_PCT", 100.0)

# Rule 2 — revenue growth (year-over-year)
REVENUE_GROWTH_MIN_PCT  = _float("REVENUE_GROWTH_MIN_PCT", 50.0)
REVENUE_WEIGHT          = _float("REVENUE_WEIGHT", 35.0)
REVENUE_GROWTH_FULL_PCT = _float("REVENUE_GROWTH_FULL_PCT", 200.0)

# Rule 3 — order wins. "Orders for crores" reads literally: anything quantified
# in crore counts. Strength is logarithmic, so ₹5 Cr and ₹5,000 Cr don't score
# the same just because both cleared ₹1 Cr.
ORDER_MIN_CR   = _float("ORDER_MIN_CR", 1.0)
ORDER_WEIGHT   = _float("ORDER_WEIGHT", 30.0)
ORDER_FULL_CR  = _float("ORDER_FULL_CR", 1000.0)

# Fraction of a rule's weight banked simply for clearing the threshold.
BASE_CREDIT = _float("BASE_CREDIT", 0.70)

# A filing becomes a dashboard alert at or above this score. The default (20)
# is below every single rule's minimum contribution (30 * 0.7 = 21), so any
# one rule firing is enough — matching the "scored, any hit surfaces, ranked
# by conviction" model.
ALERT_MIN_SCORE = _float("ALERT_MIN_SCORE", 20.0)

# ── Rule 4 — sudden volume spike (volume.py) ─────────────────────────────────
#
# Scored SEPARATELY from the three filing rules above and written to its own
# table. It is not folded into a filing's score, because doing so would have
# meant re-weighting rules 1-3 and changing every score they already produce.
#
# Baselines come from NSE's daily Bhavcopy (all ~2,600 EQ-series stocks), so a
# stock that is quiet for a month and then spikes is still covered - the live
# movers feed alone would never have shown it before the spike.

# How many times the trailing MEDIAN volume counts as a spike.
VOLUME_SPIKE_MIN_X = _float("VOLUME_SPIKE_MIN_X", 5.0)
# Ratio at which the rule is maxed out. Log-scaled between the two.
VOLUME_SPIKE_FULL_X = _float("VOLUME_SPIKE_FULL_X", 20.0)
VOLUME_WEIGHT = _float("VOLUME_WEIGHT", 30.0)

# Trailing sessions the median and max are taken over (~1 trading month).
VOLUME_LOOKBACK_SESSIONS = _int("VOLUME_LOOKBACK_SESSIONS", 20)
# Refuse to judge a stock with less history than this. Better silent than wrong.
VOLUME_MIN_SESSIONS = _int("VOLUME_MIN_SESSIONS", 10)

# Today must also be the highest volume in the window. This is what makes the
# rule fire on a SUDDEN break rather than every day of a busy fortnight - the
# difference the whole rule turns on.
VOLUME_REQUIRE_NEW_HIGH = _bool("VOLUME_REQUIRE_NEW_HIGH", True)

# Sessions of silence after a stock has been flagged. The new-high test alone
# does not stop a sustained run - on day two the median is still low and volume
# that edges above yesterday counts as a fresh high, so it fires again. "This
# stock suddenly got busy" is an event, not a state: report it once.
VOLUME_COOLDOWN_SESSIONS = _int("VOLUME_COOLDOWN_SESSIONS", 5)

# Liquidity floors. These remove most false positives: without them an illiquid
# microcap going from 40 shares to 900 reads as a 22x spike.
#
# Measured on a real session (2,864 stocks): 3x + Rs 5 Cr gave 49 alerts,
# 5x + Rs 25 Cr gives about 30. Lower them to widen the net.
VOLUME_MIN_TURNOVER_CR = _float("VOLUME_MIN_TURNOVER_CR", 25.0)
VOLUME_MIN_BASELINE_SHARES = _float("VOLUME_MIN_BASELINE_SHARES", 20000.0)

# A spike on a falling price is real information but not a "could go up"
# signal. Set negative (e.g. -100) to alert on heavy volume in either direction.
VOLUME_MIN_PRICE_CHANGE_PCT = _float("VOLUME_MIN_PRICE_CHANGE_PCT", 0.0)

# How many sessions of Bhavcopy to pull on a cold start.
BHAVCOPY_BACKFILL_SESSIONS = _int("BHAVCOPY_BACKFILL_SESSIONS", 25)


# Conviction bands used for the dashboard's badge colours.
BAND_STRONG   = _float("BAND_STRONG", 70.0)
BAND_MODERATE = _float("BAND_MODERATE", 45.0)


# ─────────────────────────────────────────────────────────────────────────────
#  Worker
# ─────────────────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = _int("POLL_INTERVAL_SEC", 60)
BATCH_SIZE        = _int("BATCH_SIZE", 25)
WORKER_THREADS    = _int("WORKER_THREADS", 4)
MAX_ANALYSIS_RETRIES = _int("MAX_ANALYSIS_RETRIES", 3)

# Only look at filings from the last N days on a cold start, so the first run
# doesn't try to analyse the entire back-catalogue at once.
BACKFILL_DAYS = _int("BACKFILL_DAYS", 7)

# The cheap keyword gate in prefilter.py. Turn off to send EVERY filing to the
# model (accurate, much more expensive).
PREFILTER_ENABLED = _bool("PREFILTER_ENABLED", True)

# Screen on the announcement TITLE before opening the PDF at all.
#
# This is the main CPU control. Parsing PDFs dominates this service's CPU use,
# and most filings the scraper ingests are routine compliance documents whose
# captions are fixed by regulation ("Trading Window", "Shareholding Pattern").
# Skipping those unopened avoids the parse entirely. Set false to always read
# the document — more thorough, several times the CPU.
SKIP_BY_TITLE = _bool("SKIP_BY_TITLE", True)

# Alerts older than this stop showing on the dashboard's default view.
ALERT_TTL_DAYS = _int("ALERT_TTL_DAYS", 5)


# ─────────────────────────────────────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────────────────────────────────────
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = _int("API_PORT", 8000)
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5174,http://localhost:5173").split(",")
    if o.strip()
]

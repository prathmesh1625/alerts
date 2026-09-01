"""
config.py — every tunable for the alert engine in one place.

Anything that differs between dev and prod is an env var; anything that
encodes the *formula itself* is a constant here so the scoring rules are
reviewable in one screen rather than scattered across the worker.
"""
import math
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
    # A blank value means "not set". Compose writes an empty string for an
    # unset ${VAR}, and int("") raises - so without this, removing a variable
    # from the environment crashes the container instead of taking the default.
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else default


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

# Ceiling on the BSE full-day sweep. BSE paginates properly - unlike NSE, which
# accepts pageNo and ignores it - so the sweep just asks until a page comes back
# empty. This only stops a runaway if BSE ever returns rows forever. 250 filings
# across 5 pages is an ordinary day; 20 pages is 1,000 rows.
BSE_SWEEP_MAX_PAGES = _int("BSE_SWEEP_MAX_PAGES", 20)

# Seconds between feed polls.
#
# This is the single biggest component of end-to-end latency: at 180s a filing
# waited an average of 90 seconds just to be NOTICED. 20s matches the cadence
# the production NSE scraper has been running at without trouble, so it is a
# rate NSE demonstrably tolerates from this IP.
#
# It does roughly DOUBLE the feed traffic coming from this server, since that
# scraper is already polling at 20s. If NSE ever starts throttling, this is the
# first number to raise.
SCRAPE_INTERVAL_SEC = _int("SCRAPE_INTERVAL_SEC", 20)

# How often to sweep NSE's WHOLE day by date range, rather than just reading
# the newest filings.
#
# NSE's `pageNo` parameter is accepted and then ignored - pages 1 to 4 all
# return the same newest 20 rows - so the plain feed can never see past the
# 20 most recent filings. On a quiet Sunday that is the entire day; on a
# results day NSE publishes hundreds, and anything landing beyond those 20
# between two polls was lost permanently, because nothing looked back.
#
# The sweep is the completeness guarantee and the plain feed is the latency
# one, which is why both run. Sweeping every cycle would mean pulling the
# full day - several hundred rows - every 20 seconds, for filings we already
# have.
NSE_SWEEP_INTERVAL_SEC = _int("NSE_SWEEP_INTERVAL_SEC", 300)

# Where on-demand PDF downloads are cached. A working set, not an archive:
# pruned by age, because a filing's verdict lives in the database once analysed.
PDF_CACHE_DIR   = os.getenv("PDF_CACHE_DIR", "/tmp/alert_pdf_cache")
PDF_CACHE_HOURS = _int("PDF_CACHE_HOURS", 48)
# Split into connect and read, because one number cannot serve both jobs here.
#
# BSE lists a filing before its PDF reaches the CDN. When the CDN answers 404
# that costs nothing, but when it HANGS the old single 60s timeout was paid in
# full on every retry — 4 retries is 4.3 minutes, 6 is 6.5, which is exactly
# the delay seen on WhatsApp.
#
# requests applies the read timeout BETWEEN BYTES, not to the whole transfer,
# so a large PDF streaming steadily is unaffected by a low value here. This
# shortens dead connections only.
PDF_CONNECT_TIMEOUT_SEC = _float("PDF_CONNECT_TIMEOUT_SEC", 5.0)
PDF_READ_TIMEOUT_SEC = _float("PDF_READ_TIMEOUT_SEC", 15.0)
# Kept so an existing override still means something: if set, it becomes the
# read timeout.
PDF_DOWNLOAD_TIMEOUT_SEC = _float("PDF_DOWNLOAD_TIMEOUT_SEC", PDF_READ_TIMEOUT_SEC)
# Refuse absurdly large attachments rather than filling the disk with one file.
PDF_MAX_BYTES = _int("PDF_MAX_BYTES", 60 * 1024 * 1024)

# Above this size a document is read with pypdf ONLY - no pdfplumber, no OCR.
#
# Those two are the memory-hungry paths and they have no ceiling of their own.
# pdfminer (under pdfplumber) builds a full object graph for the whole file and
# can reach gigabytes on a large one; OCR rasterises pages and runs tesseract,
# once per worker thread. In a 768 MB container either can be OOM-killed by the
# kernel, which Python cannot catch - the process simply disappears mid-cycle
# and Docker restarts it.
#
# pypdf streams and stays cheap, so a big document still yields whatever text
# layer it has; it just does not get the expensive fallbacks.
PDF_HEAVY_PARSE_MAX_BYTES = _int("PDF_HEAVY_PARSE_MAX_BYTES", 12 * 1024 * 1024)

# Hard ceiling on pages handed to pdfplumber, for a document that is small on
# disk but enormous once expanded.
PDFPLUMBER_MAX_PAGES = _int("PDFPLUMBER_MAX_PAGES", 60)


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
#  Currency
#
#  Rupees per unit. Order values are quoted in foreign currency more often than
#  is comfortable: STLTECH's long-term supply contract was "Approximately USD
#  288 Million", which is about Rs 2,400 Cr and was recorded as Rs 28.8 Cr
#  because the currency was ignored and only the denomination read.
#
#  Approximate on purpose. The order value itself is approximate, the score is
#  logarithmic in it, and a rate 10% stale moves a score by a fraction of a
#  point. An 80x error is what actually matters, and any rate at all fixes it.
#  Override with FX_USD_INR etc. when they drift enough to care.
# ─────────────────────────────────────────────────────────────────────────────
FX_RATES = {
    "INR": 1.0,
    "USD": _float("FX_USD_INR", 88.0),
    "EUR": _float("FX_EUR_INR", 96.0),
    "GBP": _float("FX_GBP_INR", 112.0),
    "JPY": _float("FX_JPY_INR", 0.58),
    "AED": _float("FX_AED_INR", 24.0),
}


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

# ── WHICH RULES ARE LIVE ─────────────────────────────────────────────────────
#
# Only the order-win rule is on. Profit and revenue growth are kept in full —
# code, tests and thresholds — and switched back on by flipping these, once the
# order rule has been made precise enough to trust.
#
# A disabled rule contributes nothing and does not appear in `rules_hit`, and
# `max_possible` follows the enabled set, so the score means the same thing
# whatever combination is live.
PROFIT_RULE_ENABLED  = _bool("PROFIT_RULE_ENABLED", False)
REVENUE_RULE_ENABLED = _bool("REVENUE_RULE_ENABLED", False)
ORDER_RULE_ENABLED   = _bool("ORDER_RULE_ENABLED", True)


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

# --- intraday detection -----------------------------------------------------
#
# Rule 4 also runs DURING the session, off the live movers feed, so a stock that
# spikes on Monday morning appears on the dashboard on Monday morning rather
# than after the close.
#
# It compares volume-so-far against the FULL-DAY median, with no scaling for how
# much of the session has elapsed. That is deliberate. Intraday volume is
# U-shaped - heavy at the open, thin at midday, heavy at the close - so scaling
# a daily baseline by "fraction of session elapsed" reports every normal stock
# as a 2-3x spike in the first fifteen minutes. Requiring volume-so-far to
# ALREADY exceed N x a whole normal day needs no curve, cannot false-positive at
# the open, and is simply harder to trigger early in the day - which is correct,
# because early in the day there is less evidence.
VOLUME_INTRADAY_ENABLED = _bool("VOLUME_INTRADAY_ENABLED", True)
# How often to re-check during market hours. The market snapshot is itself
# cached, so this costs one NSE request per interval no matter how many stocks.
VOLUME_INTRADAY_INTERVAL_SEC = _int("VOLUME_INTRADAY_INTERVAL_SEC", 300)

# How many sessions of Bhavcopy to pull on a cold start.
BHAVCOPY_BACKFILL_SESSIONS = _int("BHAVCOPY_BACKFILL_SESSIONS", 25)


# ── Size floor (marketcap.py) ────────────────────────────────────────────────
#
# Applies to ALL FOUR rules. A filing says nothing about the size of the
# business behind it: a shell company reporting 100% profit growth on Rs 1 crore
# of revenue clears rule 1 exactly as a real business does.
#
# A company whose market cap CANNOT be determined is allowed through, not
# blocked - losing a real alert to a data gap is worse than letting one small
# company past, and the reason is recorded on the filing either way.
# Set to 0 to disable the floor entirely.
MIN_MARKET_CAP_CR = _float("MIN_MARKET_CAP_CR", 100.0)

# How long a cached market cap stays usable. It drifts with the share price,
# which is fine for a floor: it decides "bigger than Rs 100 crore?", not what to
# display.
MARKET_CAP_TTL_DAYS = _int("MARKET_CAP_TTL_DAYS", 7)


# Conviction bands used for the dashboard's badge colours.
#
# Tuned for the ORDER-ONLY set-up. With one rule live the score spans 21 to 30:
# clearing the threshold banks BASE_CREDIT of the weight, and the rest is the
# log-scaled size of the order. So the bands map to order size directly:
#
#     Rs     1 Cr -> 21     Rs   100 Cr -> 27
#     Rs    10 Cr -> 24     Rs 1,000 Cr -> 30
#
# giving WATCH below ~Rs 10 Cr, MODERATE to ~Rs 100 Cr, STRONG above it.
# Re-enabling profit and revenue restores a 0-100 range, so raise these back to
# 70 / 45 at the same time.
BAND_STRONG   = _float("BAND_STRONG", 27.0)
BAND_MODERATE = _float("BAND_MODERATE", 24.0)


# ─────────────────────────────────────────────────────────────────────────────
#  Worker
# ─────────────────────────────────────────────────────────────────────────────
# How often the agent looks for filings the scraper has inserted. This is a
# single indexed query against a local database, so polling hard costs
# essentially nothing and removes another ~30s of average delay.
POLL_INTERVAL_SEC = _int("POLL_INTERVAL_SEC", 5)
BATCH_SIZE        = _int("BATCH_SIZE", 25)
WORKER_THREADS    = _int("WORKER_THREADS", 4)
# How long we keep re-checking for a PDF the exchange has not published yet.
#
# Stated as SECONDS OF PATIENCE, with the retry count DERIVED from it, because
# a hand-set count makes this window an emergent property of three separate
# constants - and that has now caught us twice. Cutting the poll from 60s to 5s
# silently took the window from 3 minutes to 15 seconds; a hand-fixed 20
# retries put it back to only 1.7 minutes, still under the ~2 minutes BSE
# routinely takes to get a PDF onto its CDN. A filing landing at 1m50s was
# being thrown away.
#
# The asymmetry decides the value: a retry costs one indexed query and one fast
# 404, and waiting longer risks nothing but a slightly later alert - while
# giving up early loses the alert entirely.
PDF_WAIT_BUDGET_SEC = _int("PDF_WAIT_BUDGET_SEC", 300)

# Derived, not chosen. A retry costs about one poll interval when the CDN
# answers quickly, which is the common case. Setting MAX_ANALYSIS_RETRIES
# explicitly still overrides this.
MAX_ANALYSIS_RETRIES = _int(
    "MAX_ANALYSIS_RETRIES",
    max(3, int(math.ceil(PDF_WAIT_BUDGET_SEC / float(max(POLL_INTERVAL_SEC, 1))))),
)

# Only look at filings from the last N days on a cold start, so the first run
# doesn't try to analyse the entire back-catalogue at once.
BACKFILL_DAYS = _int("BACKFILL_DAYS", 7)

# Re-scoring filings judged under an older version of the formula.
#
# Every rule fix so far applied only to what arrived next: E2E Networks'
# Rs 1,000 Cr order was still sitting at score 0.0 after the rule that had
# rejected it was corrected, because nothing revisited a filing already marked
# ANALYZED. The signals are already on disk, so re-judging one costs no PDF
# re-read and no model call - just the formula, run again.
#
# Bounded per cycle so it drains gradually instead of stalling the queue that
# new filings depend on.
RESCORE_DAYS  = _int("RESCORE_DAYS", 7)
RESCORE_BATCH = _int("RESCORE_BATCH", 10)

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


# ─────────────────────────────────────────────────────────────────────────────
#  WhatsApp delivery
#
#  This sends from the SAME Meta phone number as the NSE bot, which has 130
#  paying users on it. WhatsApp scores quality per phone number, so the
#  defaults here are deliberately timid and every one of them is a brake:
#
#    * OFF unless switched on.
#    * Sends ONLY to numbers named in WHATSAPP_RECIPIENTS. There is no path
#      that reads a subscriber list, and none that reads the NSE bot's
#      database — the two products share a phone number and nothing else.
#    * A daily cap, so a bug in the formula cannot turn into a hundred
#      messages from the number the paying product depends on.
#
#  Widening any of these is a deliberate act, not a default.
# ─────────────────────────────────────────────────────────────────────────────
WHATSAPP_ENABLED = _bool("WHATSAPP_ENABLED", False)
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")

# Explicit allowlist, comma-separated, in full international form (91XXXXXXXXXX).
# Empty means send to nobody, and the notifier refuses to run rather than
# guessing at an audience.
WHATSAPP_RECIPIENTS = [
    n.strip() for n in os.getenv("WHATSAPP_RECIPIENTS", "").split(",") if n.strip()
]

# The approved template used when the 24-hour window has closed. "nse_bot" is
# already approved on this WABA and takes 5 body variables
# ({{1}} headline, {{2}} company, {{3}} event + time, {{4}} detail, {{5}} link),
# which the alert fills. Set empty to disable the fallback, in which case an
# out-of-window alert raises rather than being silently dropped.
WHATSAPP_TEMPLATE_NAME = os.getenv("WHATSAPP_TEMPLATE_NAME", "nse_bot")
WHATSAPP_TEMPLATE_LANG = os.getenv("WHATSAPP_TEMPLATE_LANG", "en")

# Only send alerts at or above this score, independent of what the dashboard
# shows. A dashboard row is glanced at; a WhatsApp message interrupts.
WHATSAPP_MIN_SCORE = _float("WHATSAPP_MIN_SCORE", 0.0)

# Hard ceiling per calendar day, counted from the notified_alerts table so it
# survives a restart. ~4 order alerts/day is normal; 25 means something is
# wrong and the cap is what stops it reaching the number.
WHATSAPP_MAX_PER_DAY = _int("WHATSAPP_MAX_PER_DAY", 25)

# Alerts older than this are never sent, so a first run against a populated
# database does not replay history to someone's phone.
WHATSAPP_MAX_AGE_MIN = _int("WHATSAPP_MAX_AGE_MIN", 180)

# How often the notifier looks for alerts it has not sent yet. One indexed
# query against a local database, so this costs essentially nothing and is
# pure end-to-end latency - it was the single largest avoidable delay between
# an alert being scored and the message arriving.
WHATSAPP_POLL_SEC = _int("WHATSAPP_POLL_SEC", 5)

# Public base for the PDF link in a message. The API serves the filing at
# /api/alerts/{id}/pdf, so this is the dashboard's own origin.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://alerts.equityalerts.in")

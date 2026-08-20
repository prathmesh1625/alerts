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
#  OCR fallback (see pdf_text.py)
#
#  Only reached when a filing's text layer is missing or mojibake — scanned
#  newspaper result cuttings and signed board-meeting outcomes. A normal
#  text-layer filing pays nothing for this.
# ─────────────────────────────────────────────────────────────────────────────
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

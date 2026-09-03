"""
db.py — Postgres access + our own schema.

We live inside the scraper's `nse_ingestion` database rather than a database of
our own, because every query we make joins against `announcements`. We only
ever READ that table; the two tables we create here are ours alone.
"""
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

_pool = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(
                    minconn=1,
                    maxconn=max(4, config.WORKER_THREADS + 4),
                    host=config.DB_HOST,
                    port=config.DB_PORT,
                    dbname=config.DB_NAME,
                    user=config.DB_USER,
                    password=config.DB_PASSWORD,
                )
    return _pool


@contextmanager
def get_conn():
    """Borrow a pooled connection; commits on success, rolls back on error."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(dict_rows: bool = True):
    with get_conn() as conn:
        factory = psycopg2.extras.RealDictCursor if dict_rows else None
        cur = conn.cursor(cursor_factory=factory)
        try:
            yield cur
        finally:
            cur.close()


# -----------------------------------------------------------------------------
#  Schema
# -----------------------------------------------------------------------------

SCHEMA_SQL = """
-- One row per filing we have looked at, whether or not it produced an alert.
-- This is the worker's ledger: it is what stops us paying OpenAI twice for the
-- same PDF, and what lets us tell "no alert because the numbers didn't qualify"
-- apart from "no alert because extraction crashed".
CREATE TABLE IF NOT EXISTS filing_analyses (
    id               SERIAL PRIMARY KEY,
    announcement_id  INTEGER     NOT NULL UNIQUE,
    company_symbol   VARCHAR(20) NOT NULL,
    file_key         TEXT,
    status           VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    skip_reason      TEXT,
    error            TEXT,
    retries          INTEGER     NOT NULL DEFAULT 0,
    document_type    VARCHAR(30),
    score            NUMERIC(6,2),
    raw_signals      JSONB,
    analyzed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_filing_analyses_status
    ON filing_analyses(status);

-- Which announcement "owns" a given document, per company.
--
-- NSE and BSE publish the same filing under different pdf_urls, so the scraper
-- stores two `announcements` rows for one event. The first row to claim a
-- (symbol, fingerprint) pair here is the one that gets analysed; the other is
-- recorded as a duplicate and never reaches the model. The primary key makes
-- the claim atomic, so the two copies racing in the same cycle cannot both win.
CREATE TABLE IF NOT EXISTS document_claims (
    company_symbol  VARCHAR(20) NOT NULL,
    fingerprint     VARCHAR(32) NOT NULL,
    announcement_id INTEGER     NOT NULL,
    announced_at    TIMESTAMP,
    claimed_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (company_symbol, fingerprint)
);

-- One row per filing that CLEARED the formula. The dashboard reads only this.
CREATE TABLE IF NOT EXISTS stock_alerts (
    id                 SERIAL PRIMARY KEY,
    announcement_id    INTEGER     NOT NULL UNIQUE,
    company_symbol     VARCHAR(20) NOT NULL,
    company_name       TEXT,
    title              TEXT,
    pdf_url            TEXT,
    local_path         TEXT,
    announced_at       TIMESTAMP,
    document_type      VARCHAR(30),
    reporting_period   TEXT,
    basis              VARCHAR(20),
    score              NUMERIC(6,2) NOT NULL,
    conviction         VARCHAR(10)  NOT NULL,
    rules_hit          TEXT[],
    profit_growth_pct  NUMERIC(12,2),
    revenue_growth_pct NUMERIC(12,2),
    order_value_cr     NUMERIC(16,2),
    headline           TEXT,
    breakdown          JSONB,
    evidence           JSONB,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_alerts_score
    ON stock_alerts(score DESC);
CREATE INDEX IF NOT EXISTS idx_stock_alerts_announced_at
    ON stock_alerts(announced_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_alerts_symbol
    ON stock_alerts(company_symbol);

-- Rule 4's baseline: one row per stock per session, from NSE's Bhavcopy.
-- Deliberately its own table, and rule 4's alerts are deliberately their own
-- table too — folding a volume spike into `stock_alerts` would have meant
-- re-weighting rules 1-3 and changing every score they have already produced.
CREATE TABLE IF NOT EXISTS daily_volume (
    symbol       VARCHAR(20) NOT NULL,
    session_date DATE        NOT NULL,
    close        NUMERIC(16,4),
    prev_close   NUMERIC(16,4),
    volume       BIGINT,
    turnover_cr  NUMERIC(16,4),
    trades       INTEGER,
    PRIMARY KEY (symbol, session_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_volume_symbol_date
    ON daily_volume(symbol, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_volume_date
    ON daily_volume(session_date DESC);

CREATE TABLE IF NOT EXISTS volume_alerts (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(20) NOT NULL,
    session_date  DATE        NOT NULL,
    volume        BIGINT,
    baseline_median BIGINT,
    baseline_max    BIGINT,
    baseline_sessions INTEGER,
    ratio         NUMERIC(10,2),
    turnover_cr   NUMERIC(16,2),
    close         NUMERIC(16,4),
    pct_change    NUMERIC(10,2),
    score         NUMERIC(6,2) NOT NULL,
    conviction    VARCHAR(10)  NOT NULL,
    headline      TEXT,
    reason        TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (symbol, session_date)
);

CREATE INDEX IF NOT EXISTS idx_volume_alerts_date
    ON volume_alerts(session_date DESC, score DESC);

-- ---------------------------------------------------------------------------
--  Column top-ups, for clusters created by an older version of this file.
--
--  These MUST come after every CREATE TABLE above: ALTER TABLE has no
--  IF EXISTS for the table itself, so an ALTER placed before its table's
--  CREATE fails outright on a fresh database — which is the only kind of
--  database a first deploy has.
-- ---------------------------------------------------------------------------
ALTER TABLE document_claims
    ADD COLUMN IF NOT EXISTS announced_at TIMESTAMP;
ALTER TABLE filing_analyses
    ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(32);
ALTER TABLE stock_alerts
    ADD COLUMN IF NOT EXISTS exchange VARCHAR(8);
-- Which version of the formula produced this verdict. A filing analysed under
-- rules that have since changed is re-scored from its stored raw_signals, so a
-- rule fix repairs the filings it already got wrong instead of only applying
-- to whatever arrives next. See scoring.formula_version().
ALTER TABLE filing_analyses
    ADD COLUMN IF NOT EXISTS formula_version VARCHAR(16);
-- Where the share price was when the alert fired, and where it was six months
-- earlier. Stored on the alert rather than looked up on read, so the dashboard
-- shows what was true AT THE TIME rather than a figure that drifts afterwards.
ALTER TABLE stock_alerts
    ADD COLUMN IF NOT EXISTS price_now NUMERIC(16,4);
ALTER TABLE stock_alerts
    ADD COLUMN IF NOT EXISTS price_6m_ago NUMERIC(16,4);
ALTER TABLE stock_alerts
    ADD COLUMN IF NOT EXISTS price_change_6m_pct NUMERIC(10,2);
-- The open is as important as the close for judging an alert: a filing
-- published after hours is repriced at the OPEN, so the overnight gap is the
-- part nobody acting on the alert could capture.
ALTER TABLE daily_volume ADD COLUMN IF NOT EXISTS open NUMERIC(16,4);
ALTER TABLE daily_volume ADD COLUMN IF NOT EXISTS high NUMERIC(16,4);
ALTER TABLE daily_volume ADD COLUMN IF NOT EXISTS low  NUMERIC(16,4);
-- What the stock was doing BEFORE the alert, and what the move actually
-- looked like after it. Filled in two passes: momentum at decision time,
-- outcome once the next session has been published.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS momentum_20d_pct NUMERIC(10,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS momentum_5d_pct  NUMERIC(10,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS range_position   NUMERIC(6,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS outcome_date     DATE;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS gap_pct          NUMERIC(10,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS capturable_pct   NUMERIC(10,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS total_move_pct   NUMERIC(10,2);
CREATE INDEX IF NOT EXISTS idx_filing_analyses_formula
    ON filing_analyses(formula_version) WHERE status = 'ANALYZED';
-- Rule 4 fires twice for the same session: once intraday off the live feed the
-- moment volume-so-far clears the bar, and again after the close from Bhavcopy
-- with the final figure. UNIQUE(symbol, session_date) means the second UPDATES
-- the first rather than duplicating it, and this column says which you are
-- looking at.
ALTER TABLE volume_alerts
    ADD COLUMN IF NOT EXISTS is_intraday BOOLEAN DEFAULT FALSE;
ALTER TABLE volume_alerts
    ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ DEFAULT NOW();

-- One row per (recipient, alert) actually delivered over WhatsApp.
--
-- The composite primary key IS the dedup: a restart, a second worker, or a
-- re-scored alert cannot resend a message someone has already received. That
-- matters more here than on the dashboard, where a duplicate row is untidy
-- but a duplicate WhatsApp message is a complaint against a phone number
-- shared with the paying product.
CREATE TABLE IF NOT EXISTS notified_alerts (
    phone           VARCHAR(20)  NOT NULL,
    announcement_id INTEGER      NOT NULL,
    channel         VARCHAR(10),
    wamid           TEXT,
    sent_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (phone, announcement_id)
);

CREATE INDEX IF NOT EXISTS idx_notified_alerts_sent_at
    ON notified_alerts(sent_at DESC);

-- What the trading gate WOULD have done. Shadow only: nothing in this repo
-- places an order, and this table is the evidence for deciding whether
-- anything ever should.
CREATE TABLE IF NOT EXISTS paper_trades (
    id                 SERIAL PRIMARY KEY,
    announcement_id    INTEGER     NOT NULL UNIQUE,
    company_symbol     VARCHAR(20) NOT NULL,
    would_trade        BOOLEAN     NOT NULL,
    reason             TEXT,
    session_state      VARCHAR(16),
    session_note       TEXT,
    conviction         VARCHAR(10),
    order_cr           NUMERIC(16,2),
    market_cap_cr      NUMERIC(18,2),
    order_to_mcap_pct  NUMERIC(10,2),
    reference_price    NUMERIC(16,4),
    quantity           INTEGER,
    intended_value_inr NUMERIC(16,2),
    decided_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_decided
    ON paper_trades(decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol
    ON paper_trades(company_symbol, decided_at DESC);

-- Company size, in rupees crore, cached per symbol. Fetched lazily for the
-- handful of symbols about to alert rather than for the whole market, and
-- refreshed on a TTL. See marketcap.py for why BSE is the source.
CREATE TABLE IF NOT EXISTS market_caps (
    symbol         VARCHAR(20) PRIMARY KEY,
    market_cap_cr  NUMERIC(18,2),
    scrip_code     VARCHAR(12),
    fetched_at     TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE stock_alerts   ADD COLUMN IF NOT EXISTS market_cap_cr NUMERIC(18,2);
ALTER TABLE volume_alerts  ADD COLUMN IF NOT EXISTS market_cap_cr NUMERIC(18,2);
"""

# `announcements.exchange` belongs to the bse-scraper, which creates it in its
# own ensureSchema. We only READ it — but this service can start before that one
# has ever run, and a missing column would break every read. Kept separate from
# SCHEMA_SQL because, unlike our own tables, the table itself may legitimately
# not exist yet (a fresh database with no scraper attached), and that must be a
# warning rather than a crash.
ANNOUNCEMENTS_PATCH_SQL = """
ALTER TABLE announcements
    ADD COLUMN IF NOT EXISTS exchange VARCHAR(8);
ALTER TABLE announcements
    ADD COLUMN IF NOT EXISTS company_name TEXT;
"""


# In STANDALONE mode nobody else owns `announcements`, so we create it — the
# same shape the production scraper uses (scraper/db/ensureSchema.js), so the
# two modes are interchangeable and the agent's queries do not branch.
ANNOUNCEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS announcements (
  id                SERIAL PRIMARY KEY,
  company_symbol    VARCHAR(20) NOT NULL,
  company_name      TEXT,
  title             TEXT,
  pdf_url           TEXT NOT NULL UNIQUE,
  local_path        TEXT,
  announcement_time TIMESTAMP,
  download_status   VARCHAR(20) DEFAULT 'PENDING',
  exchange          VARCHAR(8),
  created_at        TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_announcements_company_symbol
    ON announcements(company_symbol);
CREATE INDEX IF NOT EXISTS idx_announcements_time
    ON announcements(announcement_time DESC);
"""


def ensure_announcements_schema() -> None:
    """Create the announcements table. STANDALONE mode only."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(ANNOUNCEMENTS_DDL)
    print("[db] announcements table verified", flush=True)


def save_announcement(row: dict) -> bool:
    """
    Insert one filing. True if it was new.

    `download_status` is set to DOWNLOADED even though no file exists yet:
    that column means "ready to analyse" to the agent, and in standalone mode
    the PDF is fetched on demand from `pdf_url`. `local_path` stays NULL, which
    is exactly what tells resolve_pdf_path to download.
    """
    sql = """
        INSERT INTO announcements
            (company_symbol, company_name, title, pdf_url,
             announcement_time, download_status, exchange)
        VALUES (%s, %s, %s, %s, %s, 'DOWNLOADED', %s)
        ON CONFLICT (pdf_url) DO NOTHING
        RETURNING id
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, (
            row["company_symbol"], row.get("company_name"), row.get("title"),
            row["pdf_url"], row.get("announced_at"), row.get("exchange"),
        ))
        return cur.fetchone() is not None


# One arbitrary but FIXED key, so every process in this stack contends for the
# same lock. Any constant works; it only has to be identical across services.
_SCHEMA_LOCK_KEY = 8_15_2026_01


def ensure_schema() -> None:
    """
    Create our tables, serialised across services.

    All four services start the moment the database reports healthy and every
    one of them runs this, so without the lock they execute the same DDL
    concurrently. `CREATE TABLE IF NOT EXISTS` is NOT safe under concurrency:
    two backends can both find the table absent and then race to insert into
    pg_type, and the loser dies with

        duplicate key value violates unique constraint
        "pg_type_typname_nsp_index"

    The API caught that and degraded, but the workers crashed and were
    restarted by Docker — which is where the "2x restarts" on a fresh deploy
    came from. Harmless in the end, since the retry found the tables already
    made, but it made every deploy look like something had failed.

    A transaction-scoped advisory lock makes the others wait rather than race.
    It is released automatically when the transaction ends, including on error,
    so a crash here cannot wedge the next deploy.
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
        cur.execute(SCHEMA_SQL)

    # Separate transaction: if `announcements` does not exist yet (a fresh
    # database with no scraper pointed at it), that is a situation to report,
    # not to die on — and it must not roll back the tables we just created.
    try:
        with get_cursor(dict_rows=False) as cur:
            cur.execute(ANNOUNCEMENTS_PATCH_SQL)
    except Exception as e:
        print("[db] note: could not patch `announcements` ({}). "
              "Is the scraper pointed at this database?".format(
                  str(e).strip().splitlines()[0][:100]), flush=True)

    print("[db] schema verified", flush=True)


# -----------------------------------------------------------------------------
#  Date windows
#
#  `days` means CALENDAR DAYS INCLUDING TODAY, not a rolling N x 24 hours.
#  "Today" must mean today's session: once the date rolls over, this morning's
#  alerts leave "Today" and appear under "3 days".
#
#  A rolling window did not do that. At 19:09 IST, NOW() - 1 day reached back to
#  19:09 the previous evening, so a filing made at 22:03 yesterday was still
#  being served as "Today".
#
#  The timezone is explicit and deliberate. `announcement_time` holds IST values
#  (that is what both exchange feeds publish), while a container's NOW() is
#  normally UTC - a 5h30m skew that silently moved filings across the midnight
#  boundary. Every window below is anchored to the Indian trading day.
# -----------------------------------------------------------------------------

MARKET_TZ = "Asia/Kolkata"

# Midnight IST at the start of the window, as a naive timestamp.
_WINDOW_START = ("(timezone('{tz}', now())::date - ((%s - 1) * INTERVAL '1 day'))"
                 .format(tz=MARKET_TZ))


def _day_window(column):
    """For naive TIMESTAMP columns that already hold IST (announcement_time)."""
    return "{} >= {}".format(column, _WINDOW_START)


def _day_window_tz(column):
    """For TIMESTAMPTZ columns (created_at) - anchor the boundary to IST."""
    return "{} >= ({} AT TIME ZONE '{}')".format(column, _WINDOW_START, MARKET_TZ)


def _day_window_date(column):
    """For DATE columns (volume_alerts.session_date)."""
    return "{} >= ({})::date".format(column, _WINDOW_START)


# -----------------------------------------------------------------------------
#  Reads against the scraper's table
# -----------------------------------------------------------------------------

def fetch_unanalyzed(limit: int) -> list:
    """
    Downloaded filings we have not settled yet, newest first.

    A filing is "not settled" when it has no filing_analyses row at all, or has
    one that FAILED and still has retries left. SKIPPED and ANALYZED rows are
    terminal, so a filing is never paid for twice.
    """
    sql = """
        SELECT
            a.{col_id}       AS announcement_id,
            a.{col_symbol}   AS company_symbol,
            a.{col_title}    AS title,
            a.{col_url}      AS pdf_url,
            a.{col_path}     AS local_path,
            a.{col_time}     AS announced_at,
            COALESCE(a.exchange, 'NSE') AS exchange,
            COALESCE(f.retries, 0) AS retries
        FROM {table} a
        LEFT JOIN filing_analyses f
               ON f.announcement_id = a.{col_id}
        WHERE a.download_status = 'DOWNLOADED'
          AND a.{col_time} >= (timezone('Asia/Kolkata', now())::date - ((%s - 1) * INTERVAL '1 day'))
          AND (
                f.id IS NULL
                OR (f.status = 'FAILED' AND f.retries < %s)
              )
        ORDER BY a.{col_time} DESC
        LIMIT %s
    """.format(
        table=config.FILINGS_TABLE,
        col_id=config.COL_ID,
        col_symbol=config.COL_COMPANY_SYMBOL,
        col_title=config.COL_TITLE,
        col_url=config.COL_PDF_URL,
        col_path=config.COL_FILE_PATH,
        col_time=config.COL_ANNOUNCED_AT,
    )
    with get_cursor() as cur:
        cur.execute(sql, (config.BACKFILL_DAYS, config.MAX_ANALYSIS_RETRIES, limit))
        return [dict(r) for r in cur.fetchall()]


# -----------------------------------------------------------------------------
#  Writes to our tables
# -----------------------------------------------------------------------------

def claim_document(company_symbol: str, fingerprint: str, announcement_id: int,
                   announced_at=None) -> int:
    """
    Claim a document for this announcement, returning the id that OWNS it.

    If the returned id is this announcement's own, it is the first copy of the
    document to arrive and should be analysed. Any other id means this is the
    other exchange's copy of a filing already handled, and it must not be sent
    to the model or turned into a second dashboard card.

    INSERT ... ON CONFLICT DO NOTHING makes the claim atomic, so the NSE and
    BSE copies landing in the same cycle cannot both believe they won.

    A match is only honoured inside config.DEDUP_WINDOW_HOURS. Cross-exchange
    copies arrive minutes apart, so that costs nothing — but it means that if
    two genuinely different filings ever hash alike (possible when a PDF's text
    layer is only a boilerplate cover letter), the collision cannot suppress an
    unrelated alert months later. Outside the window, ownership transfers to
    the newer filing so that ITS pair of copies still dedupes against itself.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_claims
                (company_symbol, fingerprint, announcement_id, announced_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (company_symbol, fingerprint) DO NOTHING
            """,
            (company_symbol, fingerprint, announcement_id, announced_at),
        )
        # DO NOTHING returns no row whether we won or lost, so read the owner
        # back explicitly rather than inferring it from the rowcount.
        cur.execute(
            """
            SELECT announcement_id, announced_at FROM document_claims
            WHERE company_symbol = %s AND fingerprint = %s
            """,
            (company_symbol, fingerprint),
        )
        row = cur.fetchone()
        if not row or row["announcement_id"] == announcement_id:
            return announcement_id

        if _within_dedup_window(announced_at, row["announced_at"]):
            return row["announcement_id"]

        # Same text, but far enough apart in time that calling it the same event
        # would be a guess. Take the claim over so this filing's own duplicate
        # still collapses against it.
        cur.execute(
            """
            UPDATE document_claims
               SET announcement_id = %s, announced_at = %s, claimed_at = NOW()
             WHERE company_symbol = %s AND fingerprint = %s
            """,
            (announcement_id, announced_at, company_symbol, fingerprint),
        )
        return announcement_id


def _within_dedup_window(a, b) -> bool:
    """True when two announcement times are close enough to be one event."""
    if a is None or b is None:
        # No time on one side — fall back to treating them as the same document,
        # which is the behaviour the fingerprint alone would give.
        return True
    try:
        delta = abs((a - b).total_seconds())
    except TypeError:
        return True  # mixed naive/aware timestamps — don't guess, just dedupe
    return delta <= config.DEDUP_WINDOW_HOURS * 3600


def record_analysis(
    announcement_id: int,
    company_symbol: str,
    file_key,
    status: str,
    skip_reason=None,
    error=None,
    document_type=None,
    score=None,
    raw_signals=None,
    fingerprint=None,
    bump_retry: bool = False,
    formula_version=None,
) -> None:
    sql = """
        INSERT INTO filing_analyses (
            announcement_id, company_symbol, file_key, status, skip_reason,
            error, document_type, score, raw_signals, fingerprint, retries,
            formula_version, analyzed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (announcement_id) DO UPDATE SET
            status        = EXCLUDED.status,
            skip_reason   = EXCLUDED.skip_reason,
            error         = EXCLUDED.error,
            document_type = EXCLUDED.document_type,
            score         = EXCLUDED.score,
            raw_signals   = EXCLUDED.raw_signals,
            fingerprint   = EXCLUDED.fingerprint,
            retries       = filing_analyses.retries + %s,
            formula_version = EXCLUDED.formula_version,
            analyzed_at   = NOW()
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, (
            announcement_id, company_symbol, file_key, status, skip_reason,
            error, document_type, score,
            psycopg2.extras.Json(raw_signals) if raw_signals is not None else None,
            fingerprint,
            1 if bump_retry else 0,
            formula_version,
            1 if bump_retry else 0,
        ))


def fetch_stale_analyses(current_version: str, days: int, limit: int) -> list:
    """
    Filings whose stored verdict was reached under a formula that has changed.

    Only ANALYZED rows that produced NO alert, because a rule fix can only ever
    turn a rejection into an alert here — an existing alert is re-scored in
    place by the normal path and is not at risk of being silently lost.

    This is what makes a rule fix retrospective. E2E Networks' Rs 1,000 Cr order
    sat at score 0.0 after the rule that rejected it was corrected, because
    nothing revisited a filing already marked ANALYZED. `raw_signals` holds
    everything the model read, so re-scoring costs no PDF read and no model call.
    """
    sql = """
        SELECT f.announcement_id, f.company_symbol, f.raw_signals, f.score,
               f.file_key, f.formula_version,
               a.{col_title}  AS title,
               a.{col_url}    AS pdf_url,
               a.{col_path}   AS local_path,
               a.{col_time}   AS announced_at,
               COALESCE(a.exchange, 'NSE') AS exchange
          FROM filing_analyses f
          JOIN {table} a ON a.{col_id} = f.announcement_id
     LEFT JOIN stock_alerts s ON s.announcement_id = f.announcement_id
         WHERE f.status = 'ANALYZED'
           AND f.raw_signals IS NOT NULL
           AND s.announcement_id IS NULL
           AND (f.formula_version IS DISTINCT FROM %s)
           AND a.{col_time} >= (timezone('Asia/Kolkata', now())::date
                                - ((%s - 1) * INTERVAL '1 day'))
      ORDER BY a.{col_time} DESC
         LIMIT %s
    """.format(
        table=config.FILINGS_TABLE,
        col_id=config.COL_ID,
        col_title=config.COL_TITLE,
        col_url=config.COL_PDF_URL,
        col_path=config.COL_FILE_PATH,
        col_time=config.COL_ANNOUNCED_AT,
    )
    with get_cursor() as cur:
        cur.execute(sql, (current_version, days, limit))
        return [dict(r) for r in cur.fetchall()]


def fetch_stale_alerts(current_version: str, days: int, limit: int) -> list:
    """
    EXISTING alerts whose filing was judged under a formula that has changed.

    The mirror of fetch_stale_analyses. A rule fix has to work in both
    directions: HAPPSTMNDS' promoter share sale alerted as a Rs 1,330 Cr order
    win, and correcting the rule left the wrong alert sitting on the dashboard
    because re-scoring could only ever ADD one.
    """
    sql = """
        SELECT f.announcement_id, f.company_symbol, f.raw_signals,
               s.score AS alert_score, s.headline,
               a.{col_title} AS title,
               a.{col_url}   AS pdf_url,
               a.{col_path}  AS local_path
          FROM filing_analyses f
          JOIN stock_alerts s ON s.announcement_id = f.announcement_id
          JOIN {table} a ON a.{col_id} = f.announcement_id
         WHERE f.raw_signals IS NOT NULL
           AND (f.formula_version IS DISTINCT FROM %s)
           AND a.{col_time} >= (timezone('Asia/Kolkata', now())::date
                                - ((%s - 1) * INTERVAL '1 day'))
      ORDER BY a.{col_time} DESC
         LIMIT %s
    """.format(
        table=config.FILINGS_TABLE,
        col_id=config.COL_ID,
        col_title=config.COL_TITLE,
        col_url=config.COL_PDF_URL,
        col_path=config.COL_FILE_PATH,
        col_time=config.COL_ANNOUNCED_AT,
    )
    with get_cursor() as cur:
        cur.execute(sql, (current_version, days, limit))
        return [dict(r) for r in cur.fetchall()]


def withdraw_alert(announcement_id: int) -> bool:
    """
    Remove an alert the formula no longer stands behind.

    The filing_analyses row is left in place, so the filings browser still shows
    the filing and what was made of it — only the dashboard card goes.
    """
    with get_cursor() as cur:
        cur.execute("DELETE FROM stock_alerts WHERE announcement_id = %s",
                    (announcement_id,))
        return cur.rowcount > 0


def stamp_formula_version(announcement_id: int, version: str) -> None:
    """Mark a filing as judged under this formula, alert or not."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE filing_analyses SET formula_version = %s WHERE announcement_id = %s",
            (version, announcement_id),
        )


def fetch_near_misses(days: int, limit: int = 100) -> list:
    """
    Filings where a real order VALUE was extracted and no alert followed.

    The signature of the failure this exists for: E2E Networks was stored with
    document_type ORDER_WIN and raw_value 1000.0 and score 0.0, and nothing
    anywhere said so. A rejection may well be right — a terminated order or a
    loan belongs here — but it should be visible and checkable rather than
    silent, especially at the top of the value range.
    """
    sql = """
        SELECT f.announcement_id, f.company_symbol, f.document_type, f.score,
               f.skip_reason, f.raw_signals, f.formula_version,
               a.{col_title} AS title,
               a.{col_time}  AS announced_at
          FROM filing_analyses f
          JOIN {table} a ON a.{col_id} = f.announcement_id
     LEFT JOIN stock_alerts s ON s.announcement_id = f.announcement_id
         WHERE f.status = 'ANALYZED'
           AND s.announcement_id IS NULL
           AND f.raw_signals IS NOT NULL
           AND jsonb_array_length(COALESCE(f.raw_signals->'orders', '[]'::jsonb)) > 0
           AND a.{col_time} >= (timezone('Asia/Kolkata', now())::date
                                - ((%s - 1) * INTERVAL '1 day'))
      ORDER BY a.{col_time} DESC
         LIMIT %s
    """.format(
        table=config.FILINGS_TABLE,
        col_id=config.COL_ID,
        col_title=config.COL_TITLE,
        col_time=config.COL_ANNOUNCED_AT,
    )
    with get_cursor() as cur:
        cur.execute(sql, (days, limit))
        return [dict(r) for r in cur.fetchall()]


def save_alert(alert: dict) -> None:
    sql = """
        INSERT INTO stock_alerts (
            announcement_id, company_symbol, company_name, title, pdf_url,
            local_path, announced_at, document_type, reporting_period, basis,
            score, conviction, rules_hit, profit_growth_pct,
            revenue_growth_pct, order_value_cr, headline, breakdown, evidence,
            exchange, market_cap_cr, price_now, price_6m_ago,
            price_change_6m_pct
        )
        VALUES (
            %(announcement_id)s, %(company_symbol)s, %(company_name)s, %(title)s,
            %(pdf_url)s, %(local_path)s, %(announced_at)s, %(document_type)s,
            %(reporting_period)s, %(basis)s, %(score)s, %(conviction)s,
            %(rules_hit)s, %(profit_growth_pct)s, %(revenue_growth_pct)s,
            %(order_value_cr)s, %(headline)s, %(breakdown)s, %(evidence)s,
            %(exchange)s, %(market_cap_cr)s, %(price_now)s,
            %(price_6m_ago)s, %(price_change_6m_pct)s
        )
        ON CONFLICT (announcement_id) DO UPDATE SET
            score              = EXCLUDED.score,
            conviction         = EXCLUDED.conviction,
            rules_hit          = EXCLUDED.rules_hit,
            profit_growth_pct  = EXCLUDED.profit_growth_pct,
            revenue_growth_pct = EXCLUDED.revenue_growth_pct,
            order_value_cr     = EXCLUDED.order_value_cr,
            headline           = EXCLUDED.headline,
            breakdown          = EXCLUDED.breakdown,
            evidence           = EXCLUDED.evidence,
            price_now          = EXCLUDED.price_now,
            price_6m_ago       = EXCLUDED.price_6m_ago,
            price_change_6m_pct = EXCLUDED.price_change_6m_pct
    """
    payload = dict(alert)
    # Optional everywhere they are written from, so a caller that predates
    # these columns still inserts cleanly.
    for k in ("price_now", "price_6m_ago", "price_change_6m_pct",
              "market_cap_cr", "exchange"):
        payload.setdefault(k, None)
    payload["breakdown"] = psycopg2.extras.Json(payload.get("breakdown") or {})
    payload["evidence"] = psycopg2.extras.Json(payload.get("evidence") or [])
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, payload)


# -----------------------------------------------------------------------------
#  Reads for the dashboard API
# -----------------------------------------------------------------------------

def fetch_alerts(days: int, min_score: float, symbol=None, limit: int = 200) -> list:
    clauses = [
        _day_window("announced_at"),
        "score >= %s",
    ]
    params = [days, min_score]
    if symbol:
        clauses.append("company_symbol = %s")
        params.append(symbol.upper())

    sql = (
        "SELECT * FROM stock_alerts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY score DESC, announced_at DESC LIMIT %s"
    )
    params.append(limit)
    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# -----------------------------------------------------------------------------
#  WhatsApp delivery
# -----------------------------------------------------------------------------

def fetch_unnotified_alerts(phone: str, min_score: float, max_age_min: int,
                            limit: int = 20) -> list:
    """
    Alerts this recipient has not been sent yet, oldest first.

    The age bound is not an optimisation. Without it, switching delivery on
    against a populated database would replay every alert ever scored to
    someone's phone in one burst — and the first thing that phone number does
    is get reported.

    BOTH clocks are bounded, because they guard different accidents:

      * `created_at` — recently SCORED. Stops a populated database replaying
        its history the moment delivery is switched on.
      * `announced_at` — recently PUBLISHED. Stops old NEWS being sent as if
        it were fresh. This is the one that matters when the engine
        backfills: a filing from yesterday scored just now has a `created_at`
        of a few seconds ago and would otherwise sail through, arriving as a
        WhatsApp message about a day-old order.

    Oldest first so a batch arrives in the order the market produced it.
    """
    sql = """
        SELECT a.*
          FROM stock_alerts a
     LEFT JOIN notified_alerts n
            ON n.announcement_id = a.announcement_id AND n.phone = %s
         WHERE n.announcement_id IS NULL
           AND a.score >= %s
           AND a.created_at >= NOW() - (%s * INTERVAL '1 minute')
           AND a.announced_at >= (timezone('Asia/Kolkata', now())
                                  - (%s * INTERVAL '1 minute'))
      ORDER BY a.announced_at ASC, a.id ASC
         LIMIT %s
    """
    with get_cursor() as cur:
        cur.execute(sql, (phone, min_score, max_age_min, max_age_min, limit))
        return [dict(r) for r in cur.fetchall()]


def count_notified_today(phone: str) -> int:
    """
    How many messages this recipient has had today, in IST.

    Read from the table rather than counted in memory, so the daily cap holds
    across a restart — which is exactly when a runaway would otherwise reset
    itself and start again.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM notified_alerts "
            "WHERE phone = %s AND " + _day_window_tz("sent_at"),
            (phone, 1),
        )
        return int(cur.fetchone()["n"])


def fetch_untraded_alerts(days: int, limit: int) -> list:
    """Alerts the trading gate has not yet judged, oldest first."""
    sql = """
        SELECT a.*
          FROM stock_alerts a
     LEFT JOIN paper_trades p ON p.announcement_id = a.announcement_id
         WHERE p.announcement_id IS NULL
           AND a.announced_at >= (timezone('Asia/Kolkata', now())
                                  - (%s * INTERVAL '1 day'))
      ORDER BY a.announced_at ASC
         LIMIT %s
    """
    with get_cursor() as cur:
        cur.execute(sql, (days, limit))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for k in ("score", "order_value_cr", "market_cap_cr"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return rows


def traded_recently(symbol: str, days: int) -> bool:
    """True if this symbol already has a would-trade decision in the window."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM paper_trades WHERE company_symbol = %s "
            "AND would_trade AND decided_at >= NOW() - (%s * INTERVAL '1 day') "
            "LIMIT 1", (symbol.upper(), days))
        return cur.fetchone() is not None


def count_trades_today() -> int:
    """How many would-trade decisions have been taken today, in IST."""
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM paper_trades WHERE would_trade "
                    "AND " + _day_window_tz("decided_at"), (1,))
        return int(cur.fetchone()["n"])


def save_paper_trade(row: dict) -> None:
    """Record one decision. Idempotent on announcement_id."""
    sql = """
        INSERT INTO paper_trades (
            announcement_id, company_symbol, would_trade, reason,
            session_state, session_note, conviction, order_cr, market_cap_cr,
            order_to_mcap_pct, reference_price, quantity, intended_value_inr
        ) VALUES (
            %(announcement_id)s, %(company_symbol)s, %(would_trade)s, %(reason)s,
            %(session_state)s, %(session_note)s, %(conviction)s, %(order_cr)s,
            %(market_cap_cr)s, %(order_to_mcap_pct)s, %(reference_price)s,
            %(quantity)s, %(intended_value_inr)s
        ) ON CONFLICT (announcement_id) DO NOTHING
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, row)


def fetch_paper_trades(days: int, limit: int = 200) -> list:
    """Recorded decisions, newest first."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM paper_trades WHERE decided_at >= NOW() - "
            "(%s * INTERVAL '1 day') ORDER BY decided_at DESC LIMIT %s",
            (days, limit))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for k in ("order_cr", "market_cap_cr", "order_to_mcap_pct",
                  "reference_price", "intended_value_inr"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return rows


def fetch_latency(days: int = 2, limit: int = 100) -> dict:
    """
    Where end-to-end time goes, split into the three stages that behave
    differently and are fixed differently.

      exchange_sec  the filing's own timestamp -> the row appearing in our
                    database. This is the EXCHANGE's publish lag plus our feed
                    poll, and the exchange's half is not ours to fix.
      analysis_sec  our row -> the alert written. PDF download and its
                    retries, extraction, the model, scoring. This is the part
                    that is entirely ours.
      queue_sec     the alert -> WhatsApp accepting it. The notifier's poll.

    Measured, not computed. A poll-interval calculation said the typical case
    was ~22s; the median measured against production was 158s, and the
    difference was invisible without this split.
    """
    sql = """
        SELECT s.company_symbol,
               s.announced_at,
               s.created_at AS alerted_at,
               n.sent_at,
               n.channel,
               EXTRACT(EPOCH FROM (a.created_at - s.announced_at))  AS exchange_sec,
               EXTRACT(EPOCH FROM (s.created_at AT TIME ZONE 'Asia/Kolkata'
                                   - a.created_at))                 AS analysis_sec,
               EXTRACT(EPOCH FROM (n.sent_at - s.created_at))       AS queue_sec
          FROM stock_alerts s
          JOIN announcements a ON a.id = s.announcement_id
     LEFT JOIN notified_alerts n ON n.announcement_id = s.announcement_id
         WHERE s.created_at >= NOW() - (%s * INTERVAL '1 day')
      ORDER BY s.created_at DESC
         LIMIT %s
    """
    with get_cursor() as cur:
        cur.execute(sql, (days, limit))
        rows = [dict(r) for r in cur.fetchall()]

    def stats(key):
        # Backfilled filings are re-scored long after the fact and would
        # otherwise dominate every percentile with hours-long "analysis".
        vals = sorted(float(r[key]) for r in rows
                      if r.get(key) is not None and 0 <= float(r[key]) < 3600)
        if not vals:
            return {"p50": None, "p90": None, "max": None, "n": 0}
        return {
            "p50": round(vals[len(vals) // 2], 1),
            "p90": round(vals[min(int(len(vals) * 0.9), len(vals) - 1)], 1),
            "max": round(vals[-1], 1),
            "n": len(vals),
        }

    live = [r for r in rows
            if r.get("exchange_sec") is not None
            and r.get("analysis_sec") is not None
            and 0 <= float(r["exchange_sec"]) < 3600
            and 0 <= float(r["analysis_sec"]) < 3600]
    totals = sorted(float(r["exchange_sec"]) + float(r["analysis_sec"]) for r in live)

    def pct(vals, p):
        return round(vals[min(int(len(vals) * p / 100.0), len(vals) - 1)], 1) if vals else None

    return {
        "sampled": len(rows),
        "live_path": len(live),
        "note": ("exchange_sec is the exchange's own publish lag plus our feed "
                 "poll; analysis_sec is ours. Backfilled re-scores are excluded."),
        "exchange_sec": stats("exchange_sec"),
        "analysis_sec": stats("analysis_sec"),
        "queue_sec": stats("queue_sec"),
        "dashboard_total_sec": {
            "p50": pct(totals, 50), "p90": pct(totals, 90),
            "max": round(totals[-1], 1) if totals else None,
            "under_60s": len([t for t in totals if t <= 60]),
            "n": len(totals),
        },
        "slowest": sorted(
            [{"symbol": r["company_symbol"],
              "exchange_sec": round(float(r["exchange_sec"]), 1),
              "analysis_sec": round(float(r["analysis_sec"]), 1)}
             for r in live],
            key=lambda x: -(x["exchange_sec"] + x["analysis_sec"]))[:10],
    }


def mark_notified(phone: str, announcement_id: int, channel: str,
                  wamid: str = "") -> bool:
    """
    Record a delivery. False if this recipient already had this alert.

    ON CONFLICT DO NOTHING makes the write idempotent, so two workers racing
    on the same alert cannot both count as a send.
    """
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO notified_alerts (phone, announcement_id, channel, wamid) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (phone, announcement_id, channel, wamid or ""),
        )
        return cur.rowcount > 0


# -----------------------------------------------------------------------------
#  The filings browser
#
#  The alerts view answers "what should I look at?". This answers "what came in,
#  and what did the agent make of it?" — including the filings that were skipped
#  and why, which is the part you need when checking the screen's judgement
#  rather than trusting it.
# -----------------------------------------------------------------------------

def fetch_companies(days: int, query=None, limit: int = 500) -> list:
    """Every company with filings in the window, plus what we did with them."""
    clauses = [_day_window("a.announcement_time")]
    params = [days]
    if query:
        clauses.append("(a.company_symbol ILIKE %s OR a.company_name ILIKE %s)")
        params += ["%{}%".format(query), "%{}%".format(query)]

    sql = """
        SELECT
            a.company_symbol,
            MAX(a.company_name)                                   AS company_name,
            COUNT(*)                                              AS filings,
            COUNT(f.id) FILTER (WHERE f.status = 'ANALYZED')       AS analyzed,
            COUNT(f.id) FILTER (WHERE f.status = 'SKIPPED')        AS skipped,
            COUNT(s.id)                                           AS alerts,
            MAX(s.score)                                          AS best_score,
            MAX(a.announcement_time)                              AS latest_filing
        FROM announcements a
        LEFT JOIN filing_analyses f ON f.announcement_id = a.id
        LEFT JOIN stock_alerts    s ON s.announcement_id = a.id
        WHERE {}
        GROUP BY a.company_symbol
        ORDER BY MAX(s.score) DESC NULLS LAST, MAX(a.announcement_time) DESC
        LIMIT %s
    """.format(" AND ".join(clauses))
    params.append(limit)

    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_filings(days: int, symbol=None, status=None, query=None,
                  limit: int = 200, offset: int = 0) -> list:
    """
    Filings in the window with whatever the agent concluded about each.

    `status` filters on the analysis outcome: ANALYZED / SKIPPED / FAILED /
    ALERT (cleared the formula) / PENDING (not reached yet).
    """
    clauses = [_day_window("a.announcement_time")]
    params = [days]

    if symbol:
        clauses.append("a.company_symbol = %s")
        params.append(symbol.upper())
    if query:
        clauses.append("(a.company_symbol ILIKE %s OR a.company_name ILIKE %s "
                       "OR a.title ILIKE %s)")
        params += ["%{}%".format(query)] * 3
    if status:
        st = status.upper()
        if st == "ALERT":
            clauses.append("s.id IS NOT NULL")
        elif st == "PENDING":
            clauses.append("f.id IS NULL")
        else:
            clauses.append("f.status = %s")
            params.append(st)

    sql = """
        SELECT
            a.id                AS announcement_id,
            a.company_symbol,
            a.company_name,
            a.title,
            a.pdf_url,
            a.local_path,
            a.announcement_time AS announced_at,
            COALESCE(a.exchange, 'NSE')          AS exchange,
            COALESCE(f.status, 'PENDING')        AS status,
            f.skip_reason,
            f.error,
            f.document_type,
            f.score,
            f.raw_signals,
            s.id IS NOT NULL    AS is_alert,
            s.conviction,
            s.headline,
            s.rules_hit
        FROM announcements a
        LEFT JOIN filing_analyses f ON f.announcement_id = a.id
        LEFT JOIN stock_alerts    s ON s.announcement_id = a.id
        WHERE {}
        ORDER BY a.announcement_time DESC
        LIMIT %s OFFSET %s
    """.format(" AND ".join(clauses))
    params += [limit, offset]

    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_filing(announcement_id: int):
    """One filing with its analysis, or None."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT a.id AS announcement_id, a.company_symbol, a.company_name,
                   a.title, a.pdf_url, a.local_path,
                   a.announcement_time AS announced_at,
                   COALESCE(a.exchange, 'NSE') AS exchange,
                   COALESCE(f.status, 'PENDING') AS status,
                   f.skip_reason, f.error, f.document_type, f.score, f.raw_signals
            FROM announcements a
            LEFT JOIN filing_analyses f ON f.announcement_id = a.id
            WHERE a.id = %s
            """,
            (announcement_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# -----------------------------------------------------------------------------
#  Rule 4 — volume spikes
# -----------------------------------------------------------------------------

def save_daily_volumes(rows) -> int:
    """Store one Bhavcopy session. Idempotent, so re-running a day is free."""
    if not rows:
        return 0
    sql = """
        INSERT INTO daily_volume
            (symbol, session_date, close, prev_close, volume, turnover_cr,
             trades, open, high, low)
        VALUES %s
        ON CONFLICT (symbol, session_date) DO UPDATE SET
            close = EXCLUDED.close, prev_close = EXCLUDED.prev_close,
            volume = EXCLUDED.volume, turnover_cr = EXCLUDED.turnover_cr,
            trades = EXCLUDED.trades, open = EXCLUDED.open,
            high = EXCLUDED.high, low = EXCLUDED.low
    """
    # .get on the OHLC keys: a caller predating them still writes cleanly.
    values = [(r["symbol"], r["session_date"], r["close"], r["prev_close"],
               r["volume"], r["turnover_cr"], r["trades"],
               r.get("open"), r.get("high"), r.get("low")) for r in rows]
    with get_cursor(dict_rows=False) as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=1000)
    return len(values)


def have_session(session_date) -> bool:
    with get_cursor(dict_rows=False) as cur:
        cur.execute("SELECT 1 FROM daily_volume WHERE session_date = %s LIMIT 1",
                    (session_date,))
        return cur.fetchone() is not None


def latest_session():
    with get_cursor(dict_rows=False) as cur:
        cur.execute("SELECT MAX(session_date) FROM daily_volume")
        row = cur.fetchone()
        return row[0] if row else None


def fetch_volume_candidates(session_date, lookback: int):
    """
    Every stock that traded on `session_date`, with its trailing history.

    The history EXCLUDES the session being judged — comparing a day against a
    window containing itself would raise the median and hide exactly the spikes
    this is looking for.
    """
    sql = """
        WITH todays AS (
            SELECT symbol, volume, turnover_cr, close, prev_close
            FROM daily_volume
            WHERE session_date = %(d)s
        ),
        hist AS (
            SELECT dv.symbol, dv.volume,
                   ROW_NUMBER() OVER (PARTITION BY dv.symbol
                                      ORDER BY dv.session_date DESC) AS rn
            FROM daily_volume dv
            WHERE dv.session_date < %(d)s
        )
        SELECT t.symbol, t.volume, t.turnover_cr, t.close, t.prev_close,
               ARRAY_AGG(h.volume ORDER BY h.rn) FILTER (WHERE h.rn <= %(n)s) AS history,
               (SELECT MAX(va.session_date) FROM volume_alerts va
                 WHERE va.symbol = t.symbol AND va.session_date < %(d)s) AS last_alert
        FROM todays t
        LEFT JOIN hist h ON h.symbol = t.symbol AND h.rn <= %(n)s
        GROUP BY t.symbol, t.volume, t.turnover_cr, t.close, t.prev_close
    """
    with get_cursor() as cur:
        cur.execute(sql, {"d": session_date, "n": lookback})
        return [dict(r) for r in cur.fetchall()]


def sessions_between(symbol, start_date, end_date):
    """Trading sessions between two dates, for the cooldown check."""
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            """SELECT COUNT(DISTINCT session_date) FROM daily_volume
               WHERE session_date > %s AND session_date <= %s""",
            (start_date, end_date))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def fetch_baselines(symbols, before_date):
    """
    Trailing volume baselines for the given symbols, from sessions BEFORE
    `before_date`.

    Used by the intraday pass: today's volume comes from the live feed, but the
    history it is judged against comes from Bhavcopy, which covers the whole
    market. That split is what lets a stock which was quiet all month be caught
    on the morning it explodes - the live movers feed alone would never have
    shown it before today.
    """
    if not symbols:
        return {}
    sql = """
        SELECT symbol,
               ARRAY_AGG(volume ORDER BY session_date DESC) AS history,
               (SELECT MAX(va.session_date) FROM volume_alerts va
                 WHERE va.symbol = dv.symbol AND va.session_date < %(d)s) AS last_alert
        FROM (
            SELECT symbol, volume, session_date,
                   ROW_NUMBER() OVER (PARTITION BY symbol
                                      ORDER BY session_date DESC) AS rn
            FROM daily_volume
            WHERE symbol = ANY(%(syms)s) AND session_date < %(d)s
        ) dv
        WHERE dv.rn <= %(n)s
        GROUP BY symbol
    """
    with get_cursor() as cur:
        cur.execute(sql, {"syms": list(symbols), "d": before_date,
                          "n": config.VOLUME_LOOKBACK_SESSIONS})
        return {r["symbol"]: dict(r) for r in cur.fetchall()}


def save_volume_alert(v, session_date, is_intraday=False) -> bool:
    sql = """
        INSERT INTO volume_alerts
            (symbol, session_date, volume, baseline_median, baseline_max,
             baseline_sessions, ratio, turnover_cr, close, pct_change,
             score, conviction, headline, reason, is_intraday, market_cap_cr,
             detected_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (symbol, session_date) DO UPDATE SET
            score = EXCLUDED.score, ratio = EXCLUDED.ratio,
            volume = EXCLUDED.volume, turnover_cr = EXCLUDED.turnover_cr,
            close = EXCLUDED.close, pct_change = EXCLUDED.pct_change,
            conviction = EXCLUDED.conviction, headline = EXCLUDED.headline,
            -- The end-of-day pass confirms an intraday alert; it must never
            -- flip a confirmed row back to provisional.
            is_intraday = volume_alerts.is_intraday AND EXCLUDED.is_intraday
        RETURNING id
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, (
            v["symbol"], session_date, v["volume"],
            int(v["baseline_median"]) if v.get("baseline_median") else None,
            int(v["baseline_max"]) if v.get("baseline_max") else None,
            v.get("baseline_sessions"), v.get("ratio"), v.get("turnover_cr"),
            v.get("close"), v.get("pct_change"), v["score"],
            v["conviction"], v.get("headline"), v.get("reason"), is_intraday,
            v.get("market_cap_cr"),
        ))
        return cur.fetchone() is not None


def fetch_volume_alerts(days: int, min_score: float = 0.0, symbol=None,
                        limit: int = 200):
    clauses = [_day_window_date("session_date"),
               "score >= %s"]
    params = [days, min_score]
    if symbol:
        clauses.append("symbol = %s")
        params.append(symbol.upper())
    sql = ("SELECT * FROM volume_alerts WHERE " + " AND ".join(clauses)
           + " ORDER BY session_date DESC, score DESC LIMIT %s")
    params.append(limit)
    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def sessions_for(symbol, before_date, limit=25):
    """The last `limit` sessions for a symbol strictly before a date, oldest first."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT session_date, open, high, low, close, prev_close, volume "
            "FROM daily_volume WHERE symbol = %s AND session_date < %s "
            "AND close IS NOT NULL ORDER BY session_date DESC LIMIT %s",
            (symbol.upper(), before_date, limit))
        return list(reversed([dict(r) for r in cur.fetchall()]))


def first_session_on_or_after(symbol, date):
    """The first session for a symbol on or after a date, or None."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT session_date, open, high, low, close, prev_close "
            "FROM daily_volume WHERE symbol = %s AND session_date >= %s "
            "AND close IS NOT NULL ORDER BY session_date ASC LIMIT 1",
            (symbol.upper(), date))
        row = cur.fetchone()
        return dict(row) if row else None


def fetch_trades_awaiting_outcome(days, limit=50):
    """Recorded decisions whose post-alert session has not been measured yet."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT p.*, a.announced_at FROM paper_trades p "
            "JOIN stock_alerts a ON a.announcement_id = p.announcement_id "
            "WHERE p.outcome_date IS NULL "
            "AND p.decided_at >= NOW() - (%s * INTERVAL '1 day') "
            "ORDER BY p.decided_at ASC LIMIT %s", (days, limit))
        return [dict(r) for r in cur.fetchall()]


def update_trade_metrics(trade_id, fields):
    """Write measured columns onto one recorded decision."""
    if not fields:
        return
    sets = ", ".join("{} = %s".format(k) for k in fields)
    with get_cursor() as cur:
        cur.execute("UPDATE paper_trades SET {} WHERE id = %s".format(sets),
                    list(fields.values()) + [trade_id])


def close_on(symbol, session_date):
    """That symbol's close on one past session, or None."""
    with get_cursor() as cur:
        cur.execute("SELECT close FROM daily_volume WHERE symbol = %s AND session_date = %s",
                    (symbol.upper(), session_date))
        row = cur.fetchone()
        return float(row["close"]) if row and row["close"] is not None else None


def latest_close(symbol):
    """The most recent close we hold for a symbol, or None."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT close FROM daily_volume WHERE symbol = %s AND close IS NOT NULL "
            "ORDER BY session_date DESC LIMIT 1", (symbol.upper(),))
        row = cur.fetchone()
        return float(row["close"]) if row else None


def fetch_market_cap(symbol, ttl_days):
    """Cached market cap in Rs crore, or None when absent or stale."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT market_cap_cr FROM market_caps
               WHERE symbol = %s
                 AND fetched_at > NOW() - (%s * INTERVAL '1 day')""",
            (symbol.upper(), ttl_days))
        row = cur.fetchone()
        return float(row["market_cap_cr"]) if row and row["market_cap_cr"] is not None else None


def save_market_cap(symbol, value, scrip_code=None) -> None:
    with get_cursor(dict_rows=False) as cur:
        cur.execute(
            """INSERT INTO market_caps (symbol, market_cap_cr, scrip_code, fetched_at)
               VALUES (%s, %s, %s, NOW())
               ON CONFLICT (symbol) DO UPDATE SET
                   market_cap_cr = EXCLUDED.market_cap_cr,
                   scrip_code    = EXCLUDED.scrip_code,
                   fetched_at    = NOW()""",
            (symbol.upper(), value, scrip_code))


def fetch_stats(days: int) -> dict:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)                                          AS total_alerts,
                COUNT(*) FILTER (WHERE conviction = 'STRONG')     AS strong,
                COUNT(*) FILTER (WHERE conviction = 'MODERATE')   AS moderate,
                COUNT(*) FILTER (WHERE conviction = 'WATCH')      AS watch,
                COUNT(DISTINCT company_symbol)                    AS companies
            FROM stock_alerts
            WHERE announced_at >= (timezone('Asia/Kolkata', now())::date - ((%s - 1) * INTERVAL '1 day'))
            """,
            (days,),
        )
        alerts = dict(cur.fetchone() or {})

        cur.execute(
            """
            SELECT
                COUNT(*)                                        AS analyzed,
                COUNT(*) FILTER (WHERE status = 'SKIPPED')      AS skipped,
                COUNT(*) FILTER (WHERE status = 'FAILED')       AS failed
            FROM filing_analyses
            WHERE created_at >= ((timezone('Asia/Kolkata', now())::date - ((%s - 1) * INTERVAL '1 day')) AT TIME ZONE 'Asia/Kolkata')
            """,
            (days,),
        )
        pipeline = dict(cur.fetchone() or {})

    return {"alerts": alerts, "pipeline": pipeline}

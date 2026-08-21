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


def ensure_schema() -> None:
    with get_cursor(dict_rows=False) as cur:
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
          AND a.{col_time} >= NOW() - (%s * INTERVAL '1 day')
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
) -> None:
    sql = """
        INSERT INTO filing_analyses (
            announcement_id, company_symbol, file_key, status, skip_reason,
            error, document_type, score, raw_signals, fingerprint, retries,
            analyzed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (announcement_id) DO UPDATE SET
            status        = EXCLUDED.status,
            skip_reason   = EXCLUDED.skip_reason,
            error         = EXCLUDED.error,
            document_type = EXCLUDED.document_type,
            score         = EXCLUDED.score,
            raw_signals   = EXCLUDED.raw_signals,
            fingerprint   = EXCLUDED.fingerprint,
            retries       = filing_analyses.retries + %s,
            analyzed_at   = NOW()
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, (
            announcement_id, company_symbol, file_key, status, skip_reason,
            error, document_type, score,
            psycopg2.extras.Json(raw_signals) if raw_signals is not None else None,
            fingerprint,
            1 if bump_retry else 0,
            1 if bump_retry else 0,
        ))


def save_alert(alert: dict) -> None:
    sql = """
        INSERT INTO stock_alerts (
            announcement_id, company_symbol, company_name, title, pdf_url,
            local_path, announced_at, document_type, reporting_period, basis,
            score, conviction, rules_hit, profit_growth_pct,
            revenue_growth_pct, order_value_cr, headline, breakdown, evidence,
            exchange
        )
        VALUES (
            %(announcement_id)s, %(company_symbol)s, %(company_name)s, %(title)s,
            %(pdf_url)s, %(local_path)s, %(announced_at)s, %(document_type)s,
            %(reporting_period)s, %(basis)s, %(score)s, %(conviction)s,
            %(rules_hit)s, %(profit_growth_pct)s, %(revenue_growth_pct)s,
            %(order_value_cr)s, %(headline)s, %(breakdown)s, %(evidence)s,
            %(exchange)s
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
            evidence           = EXCLUDED.evidence
    """
    payload = dict(alert)
    payload["breakdown"] = psycopg2.extras.Json(payload.get("breakdown") or {})
    payload["evidence"] = psycopg2.extras.Json(payload.get("evidence") or [])
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, payload)


# -----------------------------------------------------------------------------
#  Reads for the dashboard API
# -----------------------------------------------------------------------------

def fetch_alerts(days: int, min_score: float, symbol=None, limit: int = 200) -> list:
    clauses = [
        "announced_at >= NOW() - (%s * INTERVAL '1 day')",
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
#  The filings browser
#
#  The alerts view answers "what should I look at?". This answers "what came in,
#  and what did the agent make of it?" — including the filings that were skipped
#  and why, which is the part you need when checking the screen's judgement
#  rather than trusting it.
# -----------------------------------------------------------------------------

def fetch_companies(days: int, query=None, limit: int = 500) -> list:
    """Every company with filings in the window, plus what we did with them."""
    clauses = ["a.announcement_time >= NOW() - (%s * INTERVAL '1 day')"]
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
    clauses = ["a.announcement_time >= NOW() - (%s * INTERVAL '1 day')"]
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
            (symbol, session_date, close, prev_close, volume, turnover_cr, trades)
        VALUES %s
        ON CONFLICT (symbol, session_date) DO UPDATE SET
            close = EXCLUDED.close, prev_close = EXCLUDED.prev_close,
            volume = EXCLUDED.volume, turnover_cr = EXCLUDED.turnover_cr,
            trades = EXCLUDED.trades
    """
    values = [(r["symbol"], r["session_date"], r["close"], r["prev_close"],
               r["volume"], r["turnover_cr"], r["trades"]) for r in rows]
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


def save_volume_alert(v, session_date) -> bool:
    sql = """
        INSERT INTO volume_alerts
            (symbol, session_date, volume, baseline_median, baseline_max,
             baseline_sessions, ratio, turnover_cr, close, pct_change,
             score, conviction, headline, reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (symbol, session_date) DO UPDATE SET
            score = EXCLUDED.score, ratio = EXCLUDED.ratio,
            conviction = EXCLUDED.conviction, headline = EXCLUDED.headline
        RETURNING id
    """
    with get_cursor(dict_rows=False) as cur:
        cur.execute(sql, (
            v["symbol"], session_date, v["volume"],
            int(v["baseline_median"]) if v.get("baseline_median") else None,
            int(v["baseline_max"]) if v.get("baseline_max") else None,
            v.get("baseline_sessions"), v.get("ratio"), v.get("turnover_cr"),
            v.get("close"), v.get("pct_change"), v["score"],
            v["conviction"], v.get("headline"), v.get("reason"),
        ))
        return cur.fetchone() is not None


def fetch_volume_alerts(days: int, min_score: float = 0.0, symbol=None,
                        limit: int = 200):
    clauses = ["session_date >= CURRENT_DATE - (%s * INTERVAL '1 day')",
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
            WHERE announced_at >= NOW() - (%s * INTERVAL '1 day')
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
            WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
            """,
            (days,),
        )
        pipeline = dict(cur.fetchone() or {})

    return {"alerts": alerts, "pipeline": pipeline}

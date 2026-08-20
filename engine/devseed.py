"""
devseed.py — stand up a throwaway `announcements` table from PDFs on disk.

For testing the agent end-to-end without the production database. It creates
the same `announcements` table the scraper owns, then inserts one row per PDF
it finds in a directory — so the agent, the API and the dashboard all run
exactly as they will in production, against real filings.

This writes ONLY to the database named in the config it is pointed at. It is
meant for a disposable local instance; it refuses to touch a database that
already holds announcements unless you pass --force, so it cannot be aimed at
a real one by accident.

    python devseed.py --create-db          # create the database if missing
    python devseed.py --limit 60           # seed 60 filings
    python devseed.py --reset --limit 60   # wipe our tables first, then seed

Filenames carry the symbol and timestamp, in the two shapes the scrapers use:

    ACC_11062026175814.pdf                     NSE: SYMBOL_DDMMYYYYHHMMSS
    BSE_CYIENT_2026-06-09T23_12_52.267.pdf     BSE: BSE_SYMBOL_ISO8601
"""
import argparse
import os
import re
import sys
from datetime import datetime

import psycopg2

import config

# The scraper's table, copied from scraper/db/ensureSchema.js so the agent sees
# exactly the shape it will see in production.
ANNOUNCEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS announcements (
  id                SERIAL PRIMARY KEY,
  company_symbol    VARCHAR(20) NOT NULL,
  title             TEXT,
  pdf_url           TEXT NOT NULL UNIQUE,
  local_path        TEXT,
  announcement_time TIMESTAMP,
  download_status   VARCHAR(20) DEFAULT 'PENDING',
  is_notified       BOOLEAN DEFAULT FALSE,
  exchange          VARCHAR(8),
  created_at        TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_announcements_company_symbol
    ON announcements(company_symbol);
"""

_BSE_RE = re.compile(
    r"^BSE_(?P<sym>[A-Z0-9&\-]+)_(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2})", re.I)
_NSE_RE = re.compile(r"^(?P<sym>[A-Z0-9&\-]+)_(?P<ts>\d{14})", re.I)


def parse_filename(name):
    """(symbol, announced_at, exchange) from a scraper filename, or None."""
    base = os.path.splitext(name)[0]

    m = _BSE_RE.match(base)
    if m:
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H_%M_%S")
        except ValueError:
            return None
        return m.group("sym").upper(), ts, "BSE"

    m = _NSE_RE.match(base)
    if m:
        try:
            # DDMMYYYYHHMMSS
            ts = datetime.strptime(m.group("ts"), "%d%m%Y%H%M%S")
        except ValueError:
            return None
        return m.group("sym").upper(), ts, "NSE"

    return None


def connect(dbname):
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=dbname,
        user=config.DB_USER, password=config.DB_PASSWORD or None,
        connect_timeout=8,
    )


def create_db():
    conn = connect("postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config.DB_NAME,))
    if cur.fetchone():
        print("[devseed] database {} already exists".format(config.DB_NAME))
    else:
        cur.execute('CREATE DATABASE "{}"'.format(config.DB_NAME))
        print("[devseed] created database {}".format(config.DB_NAME))
    cur.close(); conn.close()


def main():
    ap = argparse.ArgumentParser(description="Seed a dev announcements table")
    ap.add_argument("--pdf-dir", default=config.PDF_STORAGE_PATH,
                    help="directory of scraper PDFs")
    ap.add_argument("--limit", type=int, default=0, help="max filings to insert")
    ap.add_argument("--create-db", action="store_true",
                    help="create the target database first if it is missing")
    ap.add_argument("--reset", action="store_true",
                    help="drop our analysis tables and clear announcements first")
    ap.add_argument("--force", action="store_true",
                    help="seed even if announcements already has rows")
    args = ap.parse_args()

    if args.create_db:
        create_db()

    pdf_dir = args.pdf_dir
    if not os.path.isdir(pdf_dir):
        print("No such directory: {}".format(pdf_dir), file=sys.stderr)
        return 1

    conn = connect(config.DB_NAME)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(ANNOUNCEMENTS_DDL)

    if args.reset:
        # Only our own tables plus the seeded announcements — never anything else.
        cur.execute("DROP TABLE IF EXISTS stock_alerts, filing_analyses, document_claims")
        cur.execute("TRUNCATE announcements RESTART IDENTITY")
        print("[devseed] reset: cleared announcements and the engine's tables")

    cur.execute("SELECT COUNT(*) FROM announcements")
    existing = cur.fetchone()[0]
    if existing and not (args.force or args.reset):
        print("[devseed] announcements already has {} row(s). "
              "Refusing to seed without --force or --reset.".format(existing))
        return 1

    files = sorted(f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf"))
    if args.limit:
        # Spread the sample across the whole set rather than taking the first N,
        # which would be alphabetically clustered.
        step = max(1, len(files) // args.limit)
        files = files[::step][:args.limit]

    inserted = skipped = 0
    for name in files:
        parsed = parse_filename(name)
        if not parsed:
            skipped += 1
            continue
        symbol, ts, exchange = parsed

        # Relative, exactly as the scraper stores it — so resolve_pdf_path in
        # agent.py is exercised the same way it will be in production.
        local_path = "storage/pdf/{}".format(name)
        pdf_url = "https://example.invalid/devseed/{}".format(name)

        cur.execute(
            """
            INSERT INTO announcements
                (company_symbol, title, pdf_url, local_path,
                 announcement_time, download_status, exchange)
            VALUES (%s, %s, %s, %s, %s, 'DOWNLOADED', %s)
            ON CONFLICT (pdf_url) DO NOTHING
            """,
            (symbol, None, pdf_url, local_path, ts, exchange),
        )
        inserted += cur.rowcount

    cur.execute("SELECT COUNT(*), MIN(announcement_time), MAX(announcement_time) FROM announcements")
    total, oldest, newest = cur.fetchone()
    cur.execute("SELECT exchange, COUNT(*) FROM announcements GROUP BY exchange ORDER BY 2 DESC")
    by_exchange = cur.fetchall()

    cur.close(); conn.close()

    print("[devseed] inserted {}, skipped {} (unparseable filename)".format(inserted, skipped))
    print("[devseed] announcements now: {} row(s)".format(total))
    print("[devseed]   by exchange: {}".format(dict(by_exchange)))
    print("[devseed]   filed between {} and {}".format(oldest, newest))
    print()
    print("NOTE: these filings are dated {} .. {}. The agent only looks back".format(
        oldest.date() if oldest else "?", newest.date() if newest else "?"))
    print("      BACKFILL_DAYS days, so set that wide enough in .env to reach them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

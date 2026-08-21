"""
test_windows.py — "Today" must mean today's session, not the last 24 hours.

The dashboard's day filters were a rolling window: at 19:09 IST, `NOW() - 1 day`
reached back to 19:09 the previous evening, so a filing made at 22:03 yesterday
was still being served under "Today". Once the date rolls over, yesterday's
alerts must leave "Today" and appear under "3 days".

There is a timezone half to this too. `announcement_time` holds IST — that is
what both exchange feeds publish — while a container's NOW() is normally UTC.
The 5h30m skew moved filings across the midnight boundary on its own.

These tests hit a real database, because the behaviour lives in SQL. They skip
cleanly when none is reachable, so the suite still runs anywhere.

Run: pytest test_windows.py   or   python test_windows.py
"""
import datetime as dt

TEST_PREFIX = "ZZWIN"


def _db():
    """The db module, or None when no database is reachable."""
    try:
        import db
        with db.get_cursor(dict_rows=False) as cur:
            cur.execute("SELECT 1")
        return db
    except Exception:
        return None


def _seed(db, rows):
    with db.get_cursor(dict_rows=False) as cur:
        cur.execute("DELETE FROM stock_alerts WHERE company_symbol LIKE %s",
                    (TEST_PREFIX + "%",))
        for i, (sym, when) in enumerate(rows):
            cur.execute(
                """INSERT INTO stock_alerts
                   (announcement_id, company_symbol, announced_at, score,
                    conviction, headline)
                   VALUES (%s,%s,%s,%s,'WATCH','window test')
                   ON CONFLICT (announcement_id) DO UPDATE
                     SET announced_at = EXCLUDED.announced_at""",
                (990000 + i, sym, when, 25.0))


def _cleanup(db):
    with db.get_cursor(dict_rows=False) as cur:
        cur.execute("DELETE FROM stock_alerts WHERE company_symbol LIKE %s",
                    (TEST_PREFIX + "%",))


def _symbols(db, days):
    return {a["company_symbol"] for a in db.fetch_alerts(days, 0.0, None, 200)
            if a["company_symbol"].startswith(TEST_PREFIX)}


def test_day_windows_are_calendar_days_in_ist():
    db = _db()
    if db is None:
        print("      (skipped: no database reachable)")
        return

    # The IST date is what the market runs on, so anchor the fixtures to it.
    with db.get_cursor() as cur:
        cur.execute("SELECT timezone('Asia/Kolkata', now())::date AS d")
        today = cur.fetchone()["d"]

    morning = dt.datetime.combine(today, dt.time(10, 30))
    late = dt.datetime.combine(today, dt.time(23, 45))
    yesterday = dt.datetime.combine(today - dt.timedelta(days=1), dt.time(22, 3))
    three_ago = dt.datetime.combine(today - dt.timedelta(days=3), dt.time(11, 0))

    _seed(db, [
        (TEST_PREFIX + "TODAY", morning),
        (TEST_PREFIX + "LATE", late),
        (TEST_PREFIX + "YDAY", yesterday),
        (TEST_PREFIX + "OLD", three_ago),
    ])

    try:
        today_only = _symbols(db, 1)
        three_days = _symbols(db, 3)
        a_week = _symbols(db, 7)

        # The whole point: yesterday's alert has moved on.
        assert TEST_PREFIX + "YDAY" not in today_only, \
            "yesterday's alert is still showing under Today"
        assert TEST_PREFIX + "YDAY" in three_days, \
            "yesterday's alert did not appear under 3 days"

        # Today's own alerts stay put, including one filed late at night —
        # a rolling window would have dropped the morning one by evening.
        assert TEST_PREFIX + "TODAY" in today_only
        assert TEST_PREFIX + "LATE" in today_only

        # "3 days" means today plus two, so a filing three days back is outside
        # it but inside a week.
        assert TEST_PREFIX + "OLD" not in three_days
        assert TEST_PREFIX + "OLD" in a_week

        # Windows must nest.
        assert today_only <= three_days <= a_week
    finally:
        _cleanup(db)


def test_stats_use_the_same_window_as_the_alerts():
    """
    The summary strip counts and the cards below it have to agree — they are
    read as one number and one list describing the same thing.
    """
    db = _db()
    if db is None:
        print("      (skipped: no database reachable)")
        return

    with db.get_cursor() as cur:
        cur.execute("SELECT timezone('Asia/Kolkata', now())::date AS d")
        today = cur.fetchone()["d"]

    _seed(db, [
        (TEST_PREFIX + "S1", dt.datetime.combine(today, dt.time(9, 30))),
        (TEST_PREFIX + "S2", dt.datetime.combine(today - dt.timedelta(days=1),
                                                 dt.time(21, 0))),
    ])
    try:
        for days in (1, 3, 7):
            listed = len(db.fetch_alerts(days, 0.0, None, 1000))
            counted = int(db.fetch_stats(days)["alerts"]["total_alerts"])
            assert listed == counted, \
                "days={}: {} alerts listed but stats says {}".format(
                    days, listed, counted)
    finally:
        _cleanup(db)


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  PASS  {}".format(name))
            passed += 1
        except AssertionError as e:
            print("  FAIL  {}  {}".format(name, e))
            failed += 1
        except Exception as e:
            print("  ERROR {}  {}: {}".format(name, type(e).__name__, e))
            failed += 1
    print("\n{} passed, {} failed".format(passed, failed))
    raise SystemExit(1 if failed else 0)

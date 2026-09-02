"""
api.py — read-only HTTP API the dashboard talks to.

Deliberately thin: the agent has already done the expensive work and written
`stock_alerts`, so every endpoint here is a plain indexed SELECT. Nothing in
this process calls OpenAI, so the dashboard can be refreshed freely.

Run:  uvicorn api:app --reload --port 8000
      python api.py                     (same thing, honours API_PORT)
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import config
import db
import market
import pdf_fetch
from agent import resolve_pdf_path


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The agent normally gets here first, but the API has to stand up on its own
    # when it is deployed before the worker (or without one).
    #
    # A failure here must NOT stop the process. If Postgres is briefly down, or
    # DB_PASSWORD has not been filled in yet, the right behaviour is to come up
    # and report unhealthy — a server that refuses to boot tells the operator
    # far less than one answering /health with the reason. /api/config needs no
    # database at all and keeps working either way.
    try:
        db.ensure_schema()
    except Exception as e:
        # flush=True: stdout is block-buffered when redirected to a log file,
        # which is exactly how a server runs — without it this warning is
        # swallowed at the moment an operator most needs to read it.
        print("[api] WARNING: schema setup failed - serving in degraded mode",
              flush=True)
        print("[api]   {}: {}".format(
            type(e).__name__, str(e).strip().splitlines()[0]), flush=True)
        print("[api]   /api/config still works; /api/alerts will return 503",
              flush=True)
        print("[api]   fix: set DB_PASSWORD in engine/.env, then restart",
              flush=True)
    yield


app = FastAPI(
    title="Stock Alert Engine",
    description="Formula-driven alerts extracted from NSE/BSE filings",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _db_call(fn, *args, **kwargs):
    """
    Run a query, turning a database outage into a 503 the dashboard can explain.

    Without this a missing DB_PASSWORD surfaces as a 500 and a psycopg2
    traceback in the browser console; with it the user gets the actual reason.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail="database unavailable: {}".format(
                str(e).strip().splitlines()[0][:200]),
        )


@app.get("/health")
def health():
    """Liveness plus a real DB round-trip, so a broken pool shows up here."""
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="database unreachable: {}".format(e))


@app.get("/api/config")
def get_config():
    """
    The formula as the engine is actually running it.

    The dashboard renders its thresholds from this rather than hard-coding them,
    so retuning a weight in config.py cannot leave the UI describing rules that
    are no longer in force.
    """
    # Only the rules actually in force. The dashboard renders from this, so a
    # rule switched off in config cannot leave the UI describing it as live.
    all_rules = [
            {
                "enabled": config.PROFIT_RULE_ENABLED,
                "key": "PROFIT_GROWTH",
                "label": "Profit growth",
                "threshold": config.PROFIT_GROWTH_MIN_PCT,
                "unit": "% YoY",
                "weight": config.PROFIT_WEIGHT,
                "full_at": config.PROFIT_GROWTH_FULL_PCT,
            },
            {
                "enabled": config.REVENUE_RULE_ENABLED,
                "key": "REVENUE_GROWTH",
                "label": "Revenue growth",
                "threshold": config.REVENUE_GROWTH_MIN_PCT,
                "unit": "% YoY",
                "weight": config.REVENUE_WEIGHT,
                "full_at": config.REVENUE_GROWTH_FULL_PCT,
            },
            {
                "enabled": config.ORDER_RULE_ENABLED,
                "key": "ORDER_WIN",
                "label": "Order win",
                "threshold": config.ORDER_MIN_CR,
                "unit": "Rs Cr",
                "weight": config.ORDER_WEIGHT,
                "full_at": config.ORDER_FULL_CR,
            },
    ]
    return {
        "rules": [r for r in all_rules if r["enabled"]],
        "disabled_rules": [r["label"] for r in all_rules if not r["enabled"]],
        "volume_rule": {
            "key": "VOLUME_SPIKE",
            "label": "Volume spike",
            "threshold": config.VOLUME_SPIKE_MIN_X,
            "unit": "x median",
            "weight": config.VOLUME_WEIGHT,
            "full_at": config.VOLUME_SPIKE_FULL_X,
            "lookback_sessions": config.VOLUME_LOOKBACK_SESSIONS,
            "requires_new_high": config.VOLUME_REQUIRE_NEW_HIGH,
            "min_turnover_cr": config.VOLUME_MIN_TURNOVER_CR,
            "cooldown_sessions": config.VOLUME_COOLDOWN_SESSIONS,
            "scored_separately": True,
        },
        "order_buckets": list(ORDER_BUCKETS),
        "price_history_months": config.PRICE_HISTORY_MONTHS,
        "min_market_cap_cr": config.MIN_MARKET_CAP_CR,
        "alert_min_score": config.ALERT_MIN_SCORE,
        "base_credit": config.BASE_CREDIT,
        "bands": {"strong": config.BAND_STRONG, "moderate": config.BAND_MODERATE},
        "model": config.OPENAI_MODEL,
        "default_window_days": config.ALERT_TTL_DAYS,
    }


# The order-size buckets the dashboard groups by. Boundaries live here so
# "50-100 Cr" means the same thing everywhere.
ORDER_BUCKETS = (
    {"key": "SMALL", "label": "Rs 1-50 Cr", "min": 0.0, "max": 50.0},
    {"key": "MID", "label": "Rs 50-100 Cr", "min": 50.0, "max": 100.0},
    {"key": "LARGE", "label": "Rs 100 Cr+", "min": 100.0, "max": None},
)


def _order_bucket(value):
    """Which size bucket an order value falls in, or None if there is no order."""
    if value is None:
        return None
    for b in ORDER_BUCKETS:
        if value >= b["min"] and (b["max"] is None or value < b["max"]):
            return b["key"]
    return None


@app.get("/api/alerts")
def get_alerts(
    days: int = Query(default=None, ge=1, le=90),
    min_score: float = Query(default=0.0, ge=0, le=100),
    symbol: str = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Qualifying filings, strongest first."""
    window = days if days is not None else config.ALERT_TTL_DAYS
    rows = _db_call(db.fetch_alerts, window, min_score, symbol, limit)
    for r in rows:
        # NUMERIC comes back as Decimal, which json can't serialise.
        for k in ("score", "profit_growth_pct", "revenue_growth_pct",
                  "order_value_cr", "market_cap_cr", "price_now",
                  "price_6m_ago", "price_change_6m_pct"):
            if r.get(k) is not None:
                r[k] = float(r[k])

        # The order as a share of the company. A Rs 200 Cr win against a
        # Rs 2,000 Cr company is 10% and material; the same order against a
        # Rs 50,000 Cr company is rounding. Computed here rather than in the
        # browser so the WhatsApp message and the dashboard cannot disagree.
        order, cap = r.get("order_value_cr"), r.get("market_cap_cr")
        r["order_to_mcap_pct"] = (
            round(order / cap * 100.0, 2) if order and cap and cap > 0 else None)

        # Which size bucket the order falls in, named once here so the
        # dashboard, the API and any future consumer agree on the boundaries.
        r["order_bucket"] = _order_bucket(order)

    return {"window_days": window, "count": len(rows), "alerts": rows}


@app.get("/api/stats")
def get_stats(days: int = Query(default=None, ge=1, le=90)):
    """Headline counts for the dashboard's summary strip."""
    window = days if days is not None else config.ALERT_TTL_DAYS
    stats = _db_call(db.fetch_stats, window)
    stats["window_days"] = window
    return stats


@app.get("/api/companies")
def get_companies(
    days: int = Query(default=30, ge=1, le=365),
    q: str = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
):
    """Companies that filed in the window, with what the agent made of each."""
    rows = _db_call(db.fetch_companies, days, q, limit)
    for r in rows:
        if r.get("best_score") is not None:
            r["best_score"] = float(r["best_score"])
    return {"window_days": days, "count": len(rows), "companies": rows}


@app.get("/api/filings")
def get_filings(
    days: int = Query(default=30, ge=1, le=365),
    symbol: str = Query(default=None),
    status: str = Query(default=None,
                        description="ANALYZED | SKIPPED | FAILED | ALERT | PENDING"),
    q: str = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """
    Every filing in the window, with the agent's verdict — including the ones it
    skipped, and why.

    The alerts endpoint answers "what should I look at?"; this one answers "what
    came in, and was the screen right about it?", which is what you need to
    audit the formula rather than trust it.
    """
    rows = _db_call(db.fetch_filings, days, symbol, status, q, limit, offset)
    for r in rows:
        if r.get("score") is not None:
            r["score"] = float(r["score"])
    return {"window_days": days, "count": len(rows), "offset": offset,
            "filings": rows}


@app.get("/api/volume-alerts")
def get_volume_alerts(
    days: int = Query(default=5, ge=1, le=90),
    min_score: float = Query(default=0.0, ge=0, le=100),
    symbol: str = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """
    Rule 4 — stocks whose traded volume suddenly broke from their own pattern.

    A separate endpoint and a separate table from the filing alerts on purpose:
    a volume spike has no filing behind it, and folding it into the filing score
    would have re-weighted rules 1-3 and changed every score they had already
    produced.
    """
    rows = _db_call(db.fetch_volume_alerts, days, min_score, symbol, limit)
    for r in rows:
        r["is_intraday"] = bool(r.get("is_intraday"))
        for k in ("score", "ratio", "turnover_cr", "close", "pct_change",
                  "market_cap_cr"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return {"window_days": days, "count": len(rows), "alerts": rows}


@app.get("/api/market")
def get_market(days: int = Query(default=30, ge=1, le=365)):
    """
    Live NSE prices, volume and turnover — the movers, refreshed from NSE and
    cached server-side so many viewers cost one upstream request.

    Each row is tagged with `has_alert` when that company also raised a filing
    alert in the window. That cross-reference is the point of showing this here
    at all: NSE already shows prices better than we can, but it cannot tell you
    which of today's movers just filed something the screen liked.
    """
    snap = market.snapshot()

    alerted = {}
    try:
        for a in db.fetch_alerts(days, 0.0, None, 1000):
            sym = a["company_symbol"]
            prev = alerted.get(sym)
            if not prev or float(a["score"]) > prev["score"]:
                alerted[sym] = {"score": float(a["score"]),
                                "conviction": a["conviction"],
                                "headline": a["headline"],
                                "announcement_id": a["announcement_id"]}
    except Exception as e:
        # The market view must still render if the database is unavailable —
        # it does not depend on it for its own data.
        print("[api] market: alert cross-reference unavailable: {}".format(e), flush=True)

    for s in snap["stocks"]:
        s["alert"] = alerted.get(s["symbol"])

    snap["alerted_symbols"] = len([s for s in snap["stocks"] if s["alert"]])
    return snap


@app.get("/api/filings/{announcement_id}/pdf")
def get_filing_pdf(announcement_id: int):
    """
    Serve ANY filing's PDF — not only the ones that raised an alert.

    Resolution order: a local file if a scraper shares its volume, then the
    download cache, then a fresh download. That last step is what makes this
    work in standalone mode, where the vast majority of filings were never
    downloaded because the agent decided they weren't worth reading — which is
    exactly the set you want to spot-check by hand.
    """
    row = _db_call(db.fetch_filing, announcement_id)
    if not row:
        raise HTTPException(status_code=404, detail="filing not found")

    path = resolve_pdf_path(row.get("local_path")) or pdf_fetch.download(row.get("pdf_url"))
    if not path:
        raise HTTPException(
            status_code=404,
            detail="PDF could not be retrieved from the exchange (it may have "
                   "been withdrawn, or not yet published to the CDN)",
        )

    # A readable filename, since these get saved for offline analysis.
    stamp = row.get("announced_at")
    nice = "{}_{}.pdf".format(
        row.get("company_symbol") or "filing",
        stamp.strftime("%Y-%m-%d") if hasattr(stamp, "strftime") else "filing",
    )
    return FileResponse(path, media_type="application/pdf", filename=nice)


@app.get("/api/near-misses")
def get_near_misses(days: int = Query(default=7, ge=1, le=90),
                    limit: int = Query(default=100, ge=1, le=500),
                    min_value_cr: float = Query(default=0.0, ge=0)):
    """
    Filings where an order VALUE was extracted and no alert followed.

    The audit view for the failure this exists because of: E2E Networks' Rs
    1,000 Cr order was stored with document_type ORDER_WIN, raw_value 1000.0
    and score 0.0, and nothing anywhere said so — it looked exactly like a
    quiet day.

    A rejection here is often correct: a terminated order, a loan, a
    related-party ceiling all belong on this list. The point is that the
    decision is visible and checkable rather than silent, and sorted by value
    so the expensive mistakes surface first. `min_value_cr` filters to the
    ones worth arguing about.
    """
    rows = _db_call(db.fetch_near_misses, days, limit)

    out = []
    for r in rows:
        orders = ((r.get("raw_signals") or {}).get("orders") or [])
        biggest = 0.0
        for o in orders:
            try:
                v = float(o.get("raw_value") or 0)
            except (TypeError, ValueError):
                continue
            unit = (o.get("unit") or "").lower()
            mult = {"crore": 1.0, "lakh": 0.01, "million": 0.1,
                    "billion": 100.0, "thousand": 0.0001, "rupee": 1e-7}.get(unit, 1.0)
            biggest = max(biggest, v * mult)
        if biggest < min_value_cr:
            continue
        out.append({
            "announcement_id": r["announcement_id"],
            "company_symbol": r["company_symbol"],
            "title": r.get("title"),
            "announced_at": r.get("announced_at"),
            "document_type": r.get("document_type"),
            "score": float(r["score"]) if r.get("score") is not None else None,
            "largest_order_cr": round(biggest, 2) if biggest else None,
            "why_no_alert": r.get("skip_reason") or "scored below the alert threshold",
            "orders": [{"value": o.get("raw_value"), "unit": o.get("unit"),
                        "status": o.get("status"), "customer": o.get("customer"),
                        "scope": o.get("scope")} for o in orders],
        })

    out.sort(key=lambda x: -(x["largest_order_cr"] or 0))
    return {"window_days": days, "count": len(out), "near_misses": out}


@app.get("/api/latency")
def get_latency(days: int = Query(default=2, ge=1, le=30),
                limit: int = Query(default=100, ge=1, le=1000)):
    """
    End-to-end time, split into the three stages that are fixed differently.

    `exchange_sec` — the filing's own timestamp to the row reaching our
    database. That is the EXCHANGE's publish lag plus our feed poll, and the
    exchange's half is not ours to fix.
    `analysis_sec` — our row to the alert being written: PDF download and any
    retries, extraction, the model, scoring. Entirely ours.
    `queue_sec` — the alert to WhatsApp accepting it.

    `dashboard_total_sec` is the first two, which is what "how long until it
    appears on the dashboard" actually means.

    This exists because a poll-interval CALCULATION said the typical case was
    ~22s while the measured median was 158s. Latency claims here should come
    from this endpoint, not from adding up config values.
    """
    return _db_call(db.fetch_latency, days, limit)


@app.get("/api/alerts/{announcement_id}/pdf")
def get_pdf(announcement_id: int):
    """Kept for the alert cards; same behaviour as the filings endpoint."""
    return get_filing_pdf(announcement_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)

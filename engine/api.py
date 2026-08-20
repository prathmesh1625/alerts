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
    return {
        "rules": [
            {
                "key": "PROFIT_GROWTH",
                "label": "Profit growth",
                "threshold": config.PROFIT_GROWTH_MIN_PCT,
                "unit": "% YoY",
                "weight": config.PROFIT_WEIGHT,
                "full_at": config.PROFIT_GROWTH_FULL_PCT,
            },
            {
                "key": "REVENUE_GROWTH",
                "label": "Revenue growth",
                "threshold": config.REVENUE_GROWTH_MIN_PCT,
                "unit": "% YoY",
                "weight": config.REVENUE_WEIGHT,
                "full_at": config.REVENUE_GROWTH_FULL_PCT,
            },
            {
                "key": "ORDER_WIN",
                "label": "Order win",
                "threshold": config.ORDER_MIN_CR,
                "unit": "Rs Cr",
                "weight": config.ORDER_WEIGHT,
                "full_at": config.ORDER_FULL_CR,
            },
        ],
        "alert_min_score": config.ALERT_MIN_SCORE,
        "base_credit": config.BASE_CREDIT,
        "bands": {"strong": config.BAND_STRONG, "moderate": config.BAND_MODERATE},
        "model": config.OPENAI_MODEL,
        "default_window_days": config.ALERT_TTL_DAYS,
    }


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
        for k in ("score", "profit_growth_pct", "revenue_growth_pct", "order_value_cr"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return {"window_days": window, "count": len(rows), "alerts": rows}


@app.get("/api/stats")
def get_stats(days: int = Query(default=None, ge=1, le=90)):
    """Headline counts for the dashboard's summary strip."""
    window = days if days is not None else config.ALERT_TTL_DAYS
    stats = _db_call(db.fetch_stats, window)
    stats["window_days"] = window
    return stats


@app.get("/api/alerts/{announcement_id}/pdf")
def get_pdf(announcement_id: int):
    """Serve the source filing, so 'view PDF' on a card actually opens it."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT local_path, company_symbol FROM stock_alerts WHERE announcement_id = %s",
            (announcement_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="alert not found")

    path = resolve_pdf_path(row["local_path"])
    if not path:
        raise HTTPException(status_code=404, detail="PDF not available on this host")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)

# Momentum Alerts

An AI agent that reads every PDF the NSE/BSE scraper downloads, extracts the
financials with `gpt-4o-mini`, applies a screening formula, and surfaces the
companies that clear it on a dashboard.

```
 shares/scraper ──▶ Postgres: announcements ──┐
 shares/bse-scraper ──▶ storage/pdf/*.pdf ────┤
                                              │
                              ┌───────────────▼────────────────┐
                              │  engine/agent.py               │
                              │   1. extract text (+OCR)       │
                              │   2. prefilter (free)          │
                              │   3. dedup NSE/BSE copies      │
                              │   4. gpt-4o-mini → signals     │
                              │   5. apply the formula → score │
                              └───────────────┬────────────────┘
                                              │
         Postgres: filing_analyses + document_claims + stock_alerts
                                              │
                              engine/api.py ──▶ dashboard/ (React)
```

The agent never writes to the scraper's tables — it only reads `announcements`
and owns three new tables of its own in the same database.

---

## The formula

| # | Rule | Threshold | Weight | Full credit at |
|---|------|-----------|--------|----------------|
| 1 | Profit (PAT) growth, YoY | ≥ **25%** | 35 | +100% |
| 2 | Revenue growth, YoY | ≥ **50%** | 35 | +200% |
| 3 | Orders received | ≥ **₹1 Cr** | 30 | ₹1,000 Cr |

Each rule is scored **independently**. A rule that fires contributes

```
weight × (0.70 + 0.30 × strength)
```

where `strength` is 0 at the threshold and 1 at the full-credit mark. So
clearing a bar banks 70% of that rule's weight and the last 30% is earned by the
size of the beat — which is what makes the dashboard ranking meaningful instead
of just an alphabetical list of everything that qualified.

A filing becomes an alert at **score ≥ 20**, which is below the smallest single
rule contribution (30 × 0.7 = 21) — so **any one rule firing surfaces the
stock**, and the score orders them.

**Conviction bands:** Strong ≥ 70 · Moderate ≥ 45 · Watch ≥ 20

Growth is measured **year-over-year** — against the *corresponding quarter of
the previous year*, not the preceding quarter. The prompt is explicit about
this because the YoY column is the third numeric column in a standard Indian
results table and reading the second one instead is the single easiest mistake
to make.

Everything above is tunable in `engine/.env` — see `engine/.env.example`.

### Two deliberate judgment calls

**Growth is recomputed in Python, not trusted from the model.** The model is
asked to *read* the current and year-ago figures; `signals.yoy_growth` does the
division. LLMs are reliable at pulling a number out of a table and unreliable at
dividing two of them, so the arithmetic that decides an alert is arithmetic we
can reproduce. (`shares/bot/output.py` reaches the same conclusion with its
`recompute_changes`.)

**A loss-to-profit turnaround fires rule 1 without a percentage.** Going from
−₹10 Cr to +₹5 Cr is not "+150% growth" — the percentage is meaningless when the
denominator is negative — but it is exactly the kind of result the screen
exists to catch. It fires at threshold credit and the card says *why*, rather
than being silently dropped as "no growth figure".

---

## Coverage

The agent is **symbol-agnostic**. It processes every row in `announcements`
with `download_status = 'DOWNLOADED'`, with no symbol list of its own — so it
automatically covers exactly the companies the scraper is currently watching.

That set is subscription-driven (`scraper/services/symbolProvider.js` →
`subscribedCompanyRepository`: every symbol with at least one ACTIVE
subscription, refreshed every ~20s). **A company added by a new subscriber is
picked up on the agent's next cycle with no configuration here.**

Both feeds are covered. The BSE scraper resolves numeric scrip codes back to
the NSE ticker before inserting (`bse-scraper/services/scripMap.js`), so
`company_symbol` means the same thing on every row and one company's NSE and
BSE filings land under one symbol on the dashboard.

### Cross-exchange duplicates

Because both scrapers write to the same `announcements` table under different
`pdf_url`s, **one filing usually produces two rows**. Left alone that would
bill OpenAI twice and put two identical cards on the dashboard.

The agent identifies the *document* rather than the announcement: it hashes the
normalised extracted text and claims a `(company_symbol, fingerprint)` pair in
`document_claims`. The first copy to claim it is analysed; the second is
recorded as `SKIPPED — duplicate of announcement N`. The claim is an
`INSERT ... ON CONFLICT DO NOTHING` against a primary key, so two copies racing
in the same cycle cannot both win.

The title cannot do this job — the exchanges describe one filing from their own
taxonomies, and a match loose enough to unite them also unites filings that are
genuinely different. (Same conclusion, and the same normalisation, as
`_document_fingerprint` in `bot/db_watcher.py`.)

Two guards keep the dedup from over-reaching:

- A document with fewer than `FINGERPRINT_MIN_CHARS` (200) normalised
  characters gets no fingerprint and is simply not deduped. **This is set lower
  than the bot's 400 deliberately** — the bot falls back to a subject-line key
  when a document is too short and we have no such fallback, and a measured
  typical one-page order-win intimation flattens to ~360 characters, i.e.
  exactly the filings that would silently stop being deduped at 400.
- A fingerprint match only counts inside `DEDUP_WINDOW_HOURS` (72). Real
  cross-exchange copies arrive minutes apart, so this costs nothing — it bounds
  the damage if two different filings ever hash alike (possible when a PDF's
  text layer is nothing but a boilerplate cover letter) so a collision can never
  suppress an unrelated alert weeks later.

## Cost control

The scraper ingests **every** filing for every subscribed company, and most of
them (trading-window closures, shareholding patterns, AGM notices, compliance
certificates) can never satisfy any rule. `engine/prefilter.py` gates the model
call on a free keyword check, which cuts the OpenAI bill by roughly an order of
magnitude.

The gate checks in this order, so a misleading title can never discard a real
filing:

0. title or body is a presentation / call transcript / annual report → **skip**
1. title indicates results or an order win → **analyse**
2. body contains a results statement → **analyse**
3. body describes an order win with a value → **analyse**
4. title is a known-routine compliance filing → **skip**
5. otherwise → **skip**

Step 0 is checked FIRST, ahead of every positive signal, and that ordering is
the point. An investor presentation or an earnings-call transcript contains a
genuine results table, so every test below it passes — the numbers are real.
But they are numbers already published and already alerted on when the results
were filed, so letting one through produces a **duplicate alert days later on
stale data**. Annual reports and BRSRs restate a year already reported at Q4,
so they go the same way.

Measured on live data: **24 of 40 alerts were restatements** — earnings-call
transcripts, investor presentations and Reg. 34(1) annual reports — and 29 of
400 filings were being sent to the model for nothing.

Transcript detection reuses `bot/output.py`'s marker approach, including its
rule that **two** markers must clear their thresholds: results filings often
say "an earnings call will be held on…" once, and one mention must not
disqualify them.

Every decision is written to `filing_analyses` with its reason, so a skip is an
auditable choice rather than a silent drop. Set `PREFILTER_ENABLED=false` to
send everything to the model.

---

## Running it

### 1. Engine

```bash
cd engine
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt

copy .env.example .env          # then fill in OPENAI_API_KEY and DB_PASSWORD
```

Point it at the scraper's database and PDF directory in `.env`:

```ini
DB_HOST=localhost
DB_PORT=5433                      # docker-compose publishes 5432 here
DB_NAME=nse_ingestion
DB_PASSWORD=...
SCRAPER_BASE_PATH=D:\prathmesh\shares
OPENAI_API_KEY=sk-...
```

Then run the two processes:

```bash
python agent.py          # the worker loop — creates its tables on first run
python api.py            # the read API, on :8000
```

`python agent.py --once` runs a single pass and exits, which is what you want
from a scheduled task rather than a long-lived container.

### 2. Dashboard

```bash
cd dashboard
npm install
npm run dev              # http://localhost:5174
```

The Vite dev server proxies `/api/*` to `http://localhost:8000`, so no CORS
setup is needed in development. For a production build set `VITE_API_BASE` to
the engine's public URL.

---

## Testing without a production database

You do not need the scraper's real database — or Docker — to exercise the whole
system. `engine/devseed.py` builds a throwaway `announcements` table from PDFs
already on disk, so the agent, API and dashboard all run exactly as they will in
production, against real filings.

**1. Start an isolated Postgres** using the binaries a normal PostgreSQL install
already ships. No admin rights, no Docker, and it cannot touch any existing
instance — it is a separate cluster on its own port, loopback-only:

```bash
PGBIN="C:/Program Files/PostgreSQL/16/bin"
DEVDB="$TEMP/alerts_devdb"

"$PGBIN/initdb.exe" -D "$DEVDB" -U postgres -A trust -E UTF8 --locale=C
"$PGBIN/pg_ctl.exe" -D "$DEVDB" -o "-p 5440" -l "$DEVDB.log" start
```

Note `initdb` needs a directory it can set ACLs on — a path under `%TEMP%`
works; some paths on secondary drives fail with "Permission denied".

**2. Point `engine/.env` at it** — `DB_PORT=5440`, `DB_PASSWORD=` (empty; the
cluster uses trust auth on loopback).

**3. Seed and run:**

```bash
cd engine
python devseed.py --create-db --limit 150     # one row per PDF found
python agent.py --once                        # repeat until the queue drains
python api.py
```

`devseed.py` reads the symbol, timestamp and exchange out of the scraper's own
filenames (`ACC_11062026175814.pdf`, `BSE_CYIENT_2026-06-09T23_12_52.267.pdf`)
and stores `local_path` **relative**, exactly as the scraper does — so
`resolve_pdf_path` is exercised the same way it will be in production. It
refuses to seed a database that already holds announcements unless you pass
`--force` or `--reset`, so it cannot be aimed at a real one by accident.

Set `BACKFILL_DAYS` wide enough to reach the seeded filings — historical PDFs
are older than a live feed's, and the agent only looks back that far.

`testdata/` holds two synthetic filings (symbol `HELIOSTEST`) that are known to
clear the formula — one results filing, one order win. They give the dashboard
guaranteed content while you check the UI, and they are obviously not real
companies. Seed them with an absolute `local_path`.

### What a real run looks like

Against 193 genuine filings from the scraper's storage:

| | |
|---|---|
| Skipped free by the prefilter | 170 (88%) |
| Sent to gpt-4o-mini | 23 (12%) |
| Unreadable PDFs (settled, not retried) | 2 |
| Alerts raised | 1 real (`ASIANPAINT`, PAT +69.2% Q4 FY26) |

One alert from 193 filings is the formula working as specified, not a fault:
≥25% profit *and* ≥50% revenue growth are high bars, and most filings are
governance documents rather than results. Expect quiet days.

## Checking a single filing

The fastest way to answer "why did / didn't this alert?" — no worker cycle, no
database:

```bash
cd engine
python run_once.py "D:\prathmesh\shares\storage\pdf\SUZLON_2025-10-30.pdf" \
    --symbol SUZLON --title "Outcome of Board Meeting"
```

It prints the extraction diagnostics (pages, OCR'd pages, character counts),
the prefilter decision and its reason, the raw signals the model read, and the
full per-rule scoring breakdown.

Add `--no-llm` to stop before the model call (useful for debugging extraction
without an API key), or `--dump-text out.txt` to inspect exactly what the model
would have been sent.

---

## Tests

```bash
cd engine
python test_scoring.py      # 18 — the formula
python test_prefilter.py    # 13 — the cost gate
python test_agent.py        # 13 — end-to-end, real PDF on disk
```

`pytest` works too. None of them need a database, an API key, or a network —
`test_agent.py` builds a real PDF in a temp directory and runs the whole
pipeline over it with only the OpenAI call and the DB writes stubbed. The DB
stub asserts the shape of the alert dict, so drift between what `agent.py`
builds and what `db.save_alert` writes fails a test rather than production.

---

## Deploying on Coolify (standalone — recommended)

`docker-compose.yml` at the repo root is a **fully independent stack**: its own
Postgres, its own scraper, its own disk. It shares nothing with the service
your users depend on, so no mistake in this repo can take that down.

```
alert-scraper ──▶ alert-db (own Postgres) ──▶ alert-agent ──▶ alert-api ──▶ alert-dashboard
  polls NSE/BSE      announcements +              downloads      read-only     alerts.equityalerts.in
  feeds only         alerts tables               only the PDFs
                                                 worth reading
```

**Steps**

1. Coolify → **New Resource** → **Docker Compose** → this repo, branch `main`.
2. Environment variables:

   | Variable | Value |
   |---|---|
   | `OPENAI_API_KEY` | your key |
   | `ALERT_DB_PASSWORD` | any strong password — this database is new |
   | `ALERT_BACKFILL_DAYS` | `1` for the first deploy |

3. Domain: the compose file declares
   `SERVICE_FQDN_ALERTDASHBOARD_80=https://alerts.equityalerts.in`. Add an A
   record for `alerts` → your Coolify host.
4. Deploy. Every table is created on first boot; there is nothing to migrate.

Only `alert-dashboard` is exposed. The database is not published to the host,
and the API is reachable only through nginx on the same origin.

### The one way this could still affect production

A second scraper hits NSE and BSE **from the same server IP**, and NSE rate
limits aggressively. That is why this scraper is deliberately small:

| | your production scraper | this one |
|---|---|---|
| Feed poll | every 20s, 3 pages | every 180s, 2 pages |
| PDFs downloaded | **every one** | only the ~12% worth reading |

Roughly a thirteenth of the feed traffic. The asymmetry is possible because the
production scraper races to deliver WhatsApp pushes — speed is the product
there — whereas a dashboard alert is not latency-critical. If you ever see NSE
throttling, raise `ALERT_SCRAPE_INTERVAL_SEC` before anything else.

### Why the PDFs are not stored

The production scraper downloads every PDF because the bot delivers them to
users. This engine reads roughly an eighth of them — the title screen and
prefilter discard the rest — so it fetches on demand from `pdf_url` into a
cache pruned after `PDF_CACHE_HOURS`. That is what removes the need for a
shared volume, and it is what makes the standalone deployment possible at all.

A download that fails is treated as **retryable**, not terminal: BSE routinely
lists a filing seconds before its attachment reaches the CDN (`bse-scraper`
retries this 8 times for the same reason), so discarding it would lose filings
that become readable a minute later.

## Deploying into the existing stack (alternative)

**This must go into the stack that already runs your scrapers — not as a
separate Coolify application.** The agent does not scrape; it reads the
`announcements` table the scrapers write, and it reads the PDF *files* out of
the `shared_storage` volume. A standalone deployment has neither and would find
nothing to do.

Deployed into that stack, live filings flow through automatically: the scrapers
download a PDF and insert a row, and the agent picks it up on its next poll
(≤60s later).

### Do NOT create a new Coolify resource

A new resource gets its own Docker network and its own volume namespace, and
both break this immediately:

* `DB_HOST=db` will not resolve — `db` exists only inside the existing stack's
  network;
* `shared_storage` would be created **fresh and empty**, so the agent would
  find no PDFs at all.

These services must be part of the same compose project. That means editing the
`docker-compose.yml` in the repo Coolify already deploys
(`prathmesh1625/NSE-subscription-website`, branch `main`) — there is no clicking
"New Resource" in this flow.

### Steps

**1. Vendor the code into the deployed repo.** Coolify's builder only ever
checks out `NSE-subscription-website`, so the source has to be inside it:

```bash
cd /path/to/NSE-subscription-website
git rm -r --cached alerts 2>/dev/null; rm -rf alerts
cp -r /path/to/message_alerts alerts
rm -rf alerts/.git alerts/engine/.env alerts/testdata alerts/.devdb
```

Confirm `alerts/engine/.env` is gone before committing — it holds a live
OpenAI key. The repo's own `.gitignore` will not protect you here, because you
are committing into a *different* repo.

**2. Paste `deploy/coolify-compose.yml` into that repo's
`docker-compose.yml`**, under `services:`, alongside `scraper` and
`bse-scraper`. It defines `alert-agent`, `alert-api` and `alert-dashboard`, and
reuses the `db` service and `shared_storage` volume already declared there.
Nothing else in the file changes.

**3. Commit and push.** Coolify watches this repo; pushing is what triggers the
build.

**4. Add the environment variables** — in Coolify, open the **existing**
resource → *Environment Variables*:

| Variable | Value | Why |
|---|---|---|
| `OPENAI_API_KEY` | your key | Already set for the bot — reuse it |
| `ALERT_BACKFILL_DAYS` | `1` | First boot analyses everything in this window |
| `ALERT_CPU_LIMIT` | `0.75` | Hard ceiling on the agent |

Everything else has a working default.

**5. Point the domain at the dashboard.** The compose file already declares
`SERVICE_FQDN_ALERTDASHBOARD_80=https://alerts.equityalerts.in`, matching the
`SERVICE_FQDN_*` convention your `adminer` and `metabase` services use. Add an
A record for `alerts` → your Coolify host; Coolify issues the certificate.

**6. Deploy** from the Coolify UI. The agent creates its own three tables
(`filing_analyses`, `document_claims`, `stock_alerts`) on first boot, and only
ever *reads* `announcements`.

**7. Watch the `alert-agent` logs.** Expect `SKIP … not opened`, then
`ANALYZED`, and eventually `ALERT`. Two failure signatures worth knowing:

* `PDF not found on disk` — the volume mount is wrong; check
  `SCRAPER_BASE_PATH=/app` against where your scrapers actually write.
* `could not patch announcements` — the agent reached a database the scrapers
  do not write to. Check `DB_NAME=nse_ingestion`.

### Keeping the two repos in sync

Vendoring means the code exists twice. The GitHub repo stays canonical; re-copy
`engine/` and `dashboard/` into `alerts/` when you change them. If that becomes
annoying, the cleaner fix is a GitHub Actions workflow that builds and pushes
images to GHCR, after which compose references `image:` instead of `build:` and
the duplication disappears.

Note the dashboard and API share **one origin**: nginx serves the UI and
proxies `/api/` to `alert-api` internally (`dashboard/nginx.conf`). So the API
is not separately exposed, there is no CORS to configure, and `VITE_API_BASE`
stays empty in the production build.

### Keeping CPU low

PDF parsing dominates this service — **measured at ~0.34 s of CPU per filing**
on 2 MB documents. Everything else is rounding error. The controls, in order of
effect:

| Setting | Default | Why |
|---|---|---|
| `SKIP_BY_TITLE` | `true` | Screens on the announcement title **before opening the PDF**. Routine captions (Trading Window, Shareholding Pattern) are skipped unparsed — a title check is ~10⁶× cheaper than a parse. Biggest single saving; leave it on. |
| `WORKER_THREADS` | `2` | Analysed filings spend most of their time waiting on OpenAI, so throughput barely moves — but parsing is no longer 4-wide. |
| `OCR_DPI` / `OCR_MAX_PAGES` | `200` / `3` | OCR (rasterise + tesseract) is the heaviest path there is. It only runs on scanned filings, but it spikes hard. `OCR_ENABLED=false` removes it entirely, at the cost of giving up scanned filings. |
| `BATCH_SIZE` | `10` | Spreads load over more, shorter cycles instead of one long burst. |
| `ALERT_BACKFILL_DAYS` | `1` | On first boot the agent analyses everything in this window. A large value is a large one-off spike in both CPU **and** OpenAI spend. |
| `deploy.resources.limits.cpus` | `0.75` | A hard ceiling, so a results-day burst can never starve the bot or backend your users depend on. This is background work and should get the leftovers. |

Between cycles the agent is asleep; the API and dashboard idle at effectively
zero. The load is bursty by nature — quiet most of the day, busy for the hour
after results are filed — which is exactly what the CPU limit is there to cap.

## Adding it to the docker-compose stack

Add to `D:\prathmesh\shares\docker-compose.yml` (both services share the
scraper's `shared_storage` volume so they can read the PDFs):

```yaml
  alert-agent:
    build:
      context: ../message_alerts
      dockerfile: engine/Dockerfile
    container_name: nse_alert_agent
    restart: always
    environment:
      - DB_HOST=db
      - DB_PORT=5432
      - DB_NAME=nse_ingestion
      - DB_USER=${DB_USER:-postgres}
      - DB_PASSWORD=${DB_PASSWORD:?DB_PASSWORD must be set}
      - OPENAI_API_KEY=${OPENAI_API_KEY:?OPENAI_API_KEY must be set}
      - SCRAPER_BASE_PATH=/app
      - PDF_STORAGE_PATH=/app/storage/pdf
    volumes:
      - shared_storage:/app/storage
    depends_on:
      db:
        condition: service_healthy

  alert-api:
    build:
      context: ../message_alerts
      dockerfile: engine/Dockerfile
    container_name: nse_alert_api
    restart: always
    command: uvicorn api:app --host 0.0.0.0 --port 8000
    environment:
      - DB_HOST=db
      - DB_PORT=5432
      - DB_NAME=nse_ingestion
      - DB_USER=${DB_USER:-postgres}
      - DB_PASSWORD=${DB_PASSWORD:?DB_PASSWORD must be set}
      - SCRAPER_BASE_PATH=/app
      - PDF_STORAGE_PATH=/app/storage/pdf
      - CORS_ORIGINS=${ALERT_CORS_ORIGINS:-http://localhost:5174}
    ports:
      - "8000:8000"
    volumes:
      - shared_storage:/app/storage
    depends_on:
      db:
        condition: service_healthy
```

The engine `Dockerfile` installs `tesseract-ocr`, which the OCR fallback needs.
Without it, scanned filings (newspaper result cuttings, signed board-meeting
outcomes) degrade to "no text" and are skipped rather than crashing — you get a
`SKIPPED` row explaining it.

---

## Schema

Both tables live in the scraper's `nse_ingestion` database and are created
automatically on first run.

**`document_claims`** — which announcement owns a given document, per company.
The primary key on `(company_symbol, fingerprint)` is what makes the
cross-exchange dedup atomic.

**`filing_analyses`** — one row per filing looked at, whatever the outcome.
This is the worker's ledger: it makes the loop idempotent (restart as often as
you like, no PDF is re-read or re-billed) and it distinguishes "no alert because
the numbers didn't qualify" from "no alert because extraction failed".

| column | meaning |
|---|---|
| `announcement_id` | unique — the dedup key, FK-in-spirit to `announcements.id` |
| `status` | `ANALYZED` · `SKIPPED` · `FAILED` |
| `skip_reason` / `error` | why, in words |
| `retries` | `FAILED` rows are retried up to `MAX_ANALYSIS_RETRIES` |
| `fingerprint` | document hash, for cross-exchange dedup |
| `raw_signals` | JSONB — everything the model read |

**`stock_alerts`** — one row per filing that cleared the formula; the only table
the dashboard reads. Carries the score, conviction band, which rules fired, the
three headline numbers, a `breakdown` JSONB with the per-rule arithmetic, and
`evidence` quotes pulled from the PDF.

---

## The dashboard has three views

**`/` — Alerts.** What cleared the formula, ranked by conviction. The answer to
"what should I look at today?"

**`/filings` — All filings.** Every filing the scraper collected, browsable by
company, with the agent's verdict on each — *including the ones it skipped and
the reason why*. The answer to "what came in, and was the screen right?"

**`/market` — Live.** NSE last price, volume and turnover (₹ Cr), refreshed
every 20s while you watch, with the day's movers sorted by turnover. Companies
that also cleared the filing formula carry an **alert** badge — which is the
only thing this view offers that NSE's own screen does not.

Two limits, both NSE's rather than ours, and worth knowing before you rely on
it. `equity-stockIndices`, the endpoint behind NSE's full ~250-row table,
returns **404** since their Next.js rewrite, and `quote-equity` returns **403**
(bot-protected). So this covers the stocks NSE reports movement in — around 90
on a normal session — not the full listed universe, and an alerted company only
shows a live price if it happens to be among them.

Responses are cached server-side for 20s (10 minutes when the market is shut),
so a hundred open tabs still cost NSE one request per cycle rather than a
hundred. That matters: the requests come from the same server IP as the
scraper.

The second view exists because a screen you cannot audit is a screen you have to
take on faith. It shows the skip reason, the raw signals the model extracted,
and a **PDF** button on every row.

That button works even for filings that were never downloaded — which is most of
them, since the agent only fetches the ~12% it decides to read. The API falls
back to pulling the document from the exchange on demand, so the first open of
an older filing takes a moment. Filter by company, by outcome
(alerts / read / skipped), by date window, or search filing titles.

## API

| endpoint | purpose |
|---|---|
| `GET /health` | liveness + a real DB round-trip |
| `GET /api/config` | the formula as the engine is actually running it |
| `GET /api/alerts?days=&min_score=&symbol=&limit=` | alerts, strongest first |
| `GET /api/stats?days=` | counts for the summary strip |
| `GET /api/companies?days=&q=` | companies that filed, with per-company counts |
| `GET /api/filings?days=&symbol=&status=&q=&limit=&offset=` | every filing + its verdict |
| `GET /api/market` | live NSE prices/volume/turnover, cached, alert-tagged |
| `GET /api/filings/{id}/pdf` | any filing's PDF (local → cache → download) |
| `GET /api/alerts/{id}/pdf` | same, kept for the alert cards |
| `GET /docs` | OpenAPI browser |

The dashboard renders its formula panel from `/api/config` rather than
hard-coding thresholds, so retuning a weight in `config.py` can't leave the UI
describing rules that are no longer in force.

---

## A caveat worth stating plainly

Figures are extracted from PDFs by a language model. The prompt is specific
about the traps that matter on Indian filings — the units line in the table
header (`Rs. in Lakhs` changes every number by 100×), consolidated vs
standalone, the YoY column, order *wins* vs order *book* — and growth is
recomputed in Python rather than trusted from the model. It is still extraction
from unstructured documents, and it will occasionally be wrong.

Each alert card carries its evidence quotes and a link to the source PDF for
exactly this reason. Treat the dashboard as a **screen that tells you where to
look**, not as a verified data feed, and not as a prediction that a stock will
rise.

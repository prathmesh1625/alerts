"""
test_agent.py — end-to-end test of process_filing against a REAL PDF on disk.

Everything in the path is the real thing — the PDF is written to a temp file
and parsed by pypdf, the prefilter and the scorer are the shipping ones — except
the two edges that cost money or need a server: the OpenAI call and the
database. Those are stubbed, and the stub for the database ASSERTS the shape of
what it is handed, so a drift between the alert dict agent.py builds and the
columns db.save_alert writes fails here rather than in production.

Run: pytest test_agent.py   or   python test_agent.py
"""
import io
import os
import tempfile

import agent
import db as db_module
import extractor as extractor_module
from signals import FilingSignals, MetricYoY, OrderWin, PeriodFigure

# Columns save_alert's INSERT names — the contract agent.py has to satisfy.
REQUIRED_ALERT_KEYS = {
    "announcement_id", "company_symbol", "company_name", "title", "pdf_url",
    "local_path", "announced_at", "document_type", "reporting_period", "basis",
    "score", "conviction", "rules_hit", "profit_growth_pct",
    "revenue_growth_pct", "order_value_cr", "headline", "breakdown", "evidence",
    "exchange",
}


# -----------------------------------------------------------------------------
#  A real, minimal PDF
# -----------------------------------------------------------------------------

def write_pdf(path, lines):
    """Hand-build a valid single-page PDF with a text layer pypdf can read."""
    content = "BT /F1 10 Tf 40 750 Td 12 TL\n"
    for ln in lines:
        content += "({}) Tj T*\n".format(ln.replace("(", r"\(").replace(")", r"\)"))
    content += "ET"
    cs = content.encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(cs)).encode() + b" >>\nstream\n" + cs + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write("{} 0 obj\n".format(i).encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write("xref\n0 {}\n".format(len(objs) + 1).encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write("{:010d} 00000 n \n".format(off).encode())
    out.write("trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n".format(
        len(objs) + 1, xref).encode())

    with open(path, "wb") as fh:
        fh.write(out.getvalue())
    return path


RESULTS_LINES = [
    "STATEMENT OF UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED 30 SEP 2025",
    "Particulars                Quarter ended   Corresponding quarter",
    "Revenue from operations        45,231.00           29,870.00",
    "Total income                   46,043.00           30,460.00",
    "Total expenses                 39,120.00           27,010.00",
    "Profit before tax               6,923.00            3,450.00",
    "Profit after tax                5,180.00            2,580.00",
    "Earnings per share                  4.15                2.06",
    "Notes: The above unaudited financial results were reviewed by the Audit",
    "Committee and approved by the Board of Directors at their meeting held on",
    "30 September 2025. The statutory auditors have carried out a limited review",
    "of these results in terms of Regulation 33 of the SEBI Listing Obligations",
    "and Disclosure Requirements Regulations 2015 as amended from time to time.",
]

COMPLIANCE_LINES = [
    "Pursuant to Regulation 7(3) of the SEBI Listing Obligations and Disclosure",
    "Requirements Regulations 2015 we enclose the compliance certificate for the",
    "half year ended 30 September 2025 signed by the Compliance Officer and the",
    "Registrar and Share Transfer Agent of the Company for your records.",
]


# -----------------------------------------------------------------------------
#  Stubs
# -----------------------------------------------------------------------------

class Recorder:
    """
    Captures what agent.py tried to write, and checks its shape.

    `claim_document` stands in for the real table's PRIMARY KEY: first caller
    for a (symbol, fingerprint) pair wins, everyone after gets the winner's id.
    """

    def __init__(self):
        self.analyses = []
        self.alerts = []
        self.claims = {}

    def record_analysis(self, announcement_id, company_symbol, file_key, status, **kw):
        assert status in ("ANALYZED", "SKIPPED", "FAILED"), status
        self.analyses.append(dict(
            announcement_id=announcement_id, company_symbol=company_symbol,
            file_key=file_key, status=status, **kw))

    def claim_document(self, company_symbol, fingerprint, announcement_id,
                       announced_at=None):
        assert fingerprint, "claim_document called with an empty fingerprint"
        return self.claims.setdefault((company_symbol, fingerprint), announcement_id)

    def save_alert(self, alert):
        missing = REQUIRED_ALERT_KEYS - set(alert)
        extra = set(alert) - REQUIRED_ALERT_KEYS
        assert not missing, "alert dict missing keys save_alert needs: {}".format(sorted(missing))
        assert not extra, "alert dict has keys save_alert does not write: {}".format(sorted(extra))
        assert isinstance(alert["rules_hit"], list)
        assert isinstance(alert["breakdown"], dict)
        self.alerts.append(alert)


def stub_signals(**kw):
    def _extract(text, symbol="", title=""):
        return FilingSignals(**kw)
    return _extract


def make_filing(path, ann_id=101, symbol="TESTCO", title=""):
    return {
        "announcement_id": ann_id,
        "company_symbol": symbol,
        "title": title,
        "pdf_url": "https://nsearchives.nseindia.com/corporate/TESTCO.pdf",
        "local_path": path,
        "announced_at": "2026-08-19T10:30:00",
        "retries": 0,
    }


def run_case(lines, signals_kwargs, title="", symbol="TESTCO"):
    """Write a PDF, run process_filing against it, return (recorder, message)."""
    rec = Recorder()
    orig = (db_module.record_analysis, db_module.save_alert,
            db_module.claim_document,
            extractor_module.extract_signals, extractor_module.signals_to_json)
    agent.db.record_analysis = rec.record_analysis
    agent.db.save_alert = rec.save_alert
    agent.db.claim_document = rec.claim_document
    if signals_kwargs is not None:
        agent.extractor.extract_signals = stub_signals(**signals_kwargs)
    agent.extractor.signals_to_json = lambda s: {"stubbed": True}

    tmpdir = tempfile.mkdtemp()
    pdf = write_pdf(os.path.join(tmpdir, "{}_2025-09-30.pdf".format(symbol)), lines)
    try:
        msg = agent.process_filing(make_filing(pdf, symbol=symbol, title=title))
    finally:
        (db_module.record_analysis, db_module.save_alert,
         db_module.claim_document,
         extractor_module.extract_signals, extractor_module.signals_to_json) = orig
    return rec, msg


def metric(cur, prior):
    return MetricYoY(
        current=PeriodFigure(period_label="Q2 FY26", raw_value=cur, unit="crore"),
        year_ago=PeriodFigure(period_label="Q2 FY25", raw_value=prior, unit="crore"),
    )


# -----------------------------------------------------------------------------
#  Tests
# -----------------------------------------------------------------------------

def test_qualifying_results_filing_produces_an_alert():
    rec, msg = run_case(
        RESULTS_LINES,
        dict(
            company_name="Test Company Limited",
            document_type="RESULTS",
            reporting_period="Q2 FY26",
            basis="consolidated",
            revenue=metric(452.31, 298.70),   # +51.4%  -> clears 50%
            profit=metric(51.80, 25.80),      # +100.8% -> clears 25%
            evidence=["Profit after tax 5,180.00"],
        ),
        title="Outcome of Board Meeting",
    )

    assert len(rec.alerts) == 1, "expected one alert, got {}".format(len(rec.alerts))
    alert = rec.alerts[0]
    assert alert["company_symbol"] == "TESTCO"
    assert alert["company_name"] == "Test Company Limited"
    assert set(alert["rules_hit"]) == {"PROFIT_GROWTH", "REVENUE_GROWTH"}
    assert alert["score"] > 45
    assert alert["conviction"] in ("MODERATE", "STRONG")

    assert rec.analyses[-1]["status"] == "ANALYZED"
    assert "ALERT" in msg


def test_order_win_produces_an_alert():
    rec, msg = run_case(
        ["The Company has received a Letter of Award from NTPC Limited.",
         "The value of the order is Rs. 412.50 crore excluding GST."],
        dict(
            company_name="Test Company Limited",
            document_type="ORDER_WIN",
            orders=[OrderWin(unit="crore", raw_value=412.5, customer="NTPC Limited",
                             scope="Supply of solar modules")],
            evidence=["The value of the order is Rs. 412.50 crore"],
        ),
        title="Receipt of Order",
    )
    assert len(rec.alerts) == 1
    assert rec.alerts[0]["rules_hit"] == ["ORDER_WIN"]
    assert rec.alerts[0]["order_value_cr"] == 412.5
    assert "ALERT" in msg


def test_weak_results_are_analyzed_but_raise_no_alert():
    rec, msg = run_case(
        RESULTS_LINES,
        dict(
            document_type="RESULTS",
            revenue=metric(110.0, 100.0),  # +10%
            profit=metric(105.0, 100.0),   # +5%
        ),
        title="Outcome of Board Meeting",
    )
    assert rec.alerts == []
    assert rec.analyses[-1]["status"] == "ANALYZED"
    assert rec.analyses[-1]["score"] == 0.0


def test_compliance_filing_is_skipped_before_the_model_call():
    called = {"n": 0}

    def _never(text, symbol="", title=""):
        called["n"] += 1
        raise AssertionError("the model must not be called for a compliance filing")

    rec = Recorder()
    orig_extract = extractor_module.extract_signals
    agent.db.record_analysis = rec.record_analysis
    agent.db.save_alert = rec.save_alert
    agent.db.claim_document = rec.claim_document
    agent.extractor.extract_signals = _never

    tmpdir = tempfile.mkdtemp()
    pdf = write_pdf(os.path.join(tmpdir, "TESTCO_compliance.pdf"), COMPLIANCE_LINES)
    try:
        msg = agent.process_filing(
            make_filing(pdf, title="Certificate under Regulation 7(3)"))
    finally:
        agent.extractor.extract_signals = orig_extract

    assert called["n"] == 0
    assert rec.alerts == []
    assert rec.analyses[-1]["status"] == "SKIPPED"
    assert "SKIP" in msg


def test_same_document_from_both_exchanges_alerts_only_once():
    """
    The real NSE/BSE duplicate case: one filing, two announcements rows with
    different pdf_urls and different titles, the SAME document behind them.

    It must produce exactly one alert and exactly one model call — the second
    copy is money spent for a card the user would have to dismiss.
    """
    rec = Recorder()
    calls = {"n": 0}

    def _counting_extract(text, symbol="", title=""):
        calls["n"] += 1
        return FilingSignals(
            company_name="Test Company Limited",
            document_type="RESULTS",
            reporting_period="Q2 FY26",
            revenue=metric(452.31, 298.70),
            profit=metric(51.80, 25.80),
        )

    orig = (extractor_module.extract_signals, extractor_module.signals_to_json)
    agent.db.record_analysis = rec.record_analysis
    agent.db.save_alert = rec.save_alert
    agent.db.claim_document = rec.claim_document
    agent.extractor.extract_signals = _counting_extract
    agent.extractor.signals_to_json = lambda s: {"stubbed": True}

    tmpdir = tempfile.mkdtemp()
    # Same content, two files — which is exactly how the two feeds store it.
    nse_pdf = write_pdf(os.path.join(tmpdir, "TESTCO_2025-09-30.pdf"), RESULTS_LINES)
    bse_pdf = write_pdf(os.path.join(tmpdir, "BSE_TESTCO_2025-09-30_abc.pdf"), RESULTS_LINES)

    try:
        nse = make_filing(nse_pdf, ann_id=201, title="Financial Results")
        nse["exchange"] = "NSE"
        bse = make_filing(bse_pdf, ann_id=202,
                          title="Announcement under Regulation 30 (LODR)-Financial Result")
        bse["exchange"] = "BSE"
        bse["pdf_url"] = "https://www.bseindia.com/xml-data/corpfiling/TESTCO.pdf"

        msg_bse = agent.process_filing(bse)   # BSE usually lands first
        msg_nse = agent.process_filing(nse)
    finally:
        (extractor_module.extract_signals, extractor_module.signals_to_json) = orig

    assert calls["n"] == 1, "the model was called {} times for one document".format(calls["n"])
    assert len(rec.alerts) == 1, "expected one alert, got {}".format(len(rec.alerts))
    assert rec.alerts[0]["announcement_id"] == 202
    assert rec.alerts[0]["exchange"] == "BSE"

    dup = [a for a in rec.analyses if a["status"] == "SKIPPED"]
    assert len(dup) == 1
    assert "duplicate of announcement 202" in dup[0]["skip_reason"]
    assert "DUP" in msg_nse and "ALERT" in msg_bse


def test_different_filings_are_not_merged():
    """The fingerprint must not collapse two genuinely different documents."""
    rec = Recorder()
    tmpdir = tempfile.mkdtemp()

    a = write_pdf(os.path.join(tmpdir, "a.pdf"), RESULTS_LINES)
    b = write_pdf(os.path.join(tmpdir, "b.pdf"),
                  RESULTS_LINES[:-1] + ["Earnings per share  9.99  1.11"])

    from pdf_text import document_fingerprint, extract_text_from_pdf_file
    fa = document_fingerprint(extract_text_from_pdf_file(a))
    fb = document_fingerprint(extract_text_from_pdf_file(b))
    assert fa and fb, "both documents should fingerprint"
    assert fa != fb, "different documents produced the same fingerprint"

    # And the same document read twice must fingerprint identically.
    assert document_fingerprint(extract_text_from_pdf_file(a)) == fa


def test_short_document_gets_no_fingerprint_and_is_not_deduped():
    """
    A scan that OCR barely recovered has too little text to identify safely.
    Better to analyse it twice than to merge two unrelated filings.
    """
    from pdf_text import document_fingerprint
    assert document_fingerprint("too short to identify") == ""
    assert document_fingerprint("") == ""


def test_dedup_window_bounds_a_fingerprint_match():
    """
    A fingerprint match only counts as "the same filing" if the two copies were
    filed close together. Cross-exchange copies are minutes apart; an identical
    hash months later is a collision, not a duplicate, and must not suppress.
    """
    import datetime as dt

    import config
    from db import _within_dedup_window

    base = dt.datetime(2026, 8, 19, 10, 30)

    # The real case: BSE publishes, NSE follows a few minutes later.
    assert _within_dedup_window(base, base + dt.timedelta(minutes=7))
    # A filing that straddles a weekend still counts.
    assert _within_dedup_window(base, base + dt.timedelta(hours=48))
    # Well beyond the window — treat as a distinct event.
    assert not _within_dedup_window(base, base + dt.timedelta(days=30))
    # Order must not matter.
    assert not _within_dedup_window(base + dt.timedelta(days=30), base)
    # Exactly at the boundary is still a duplicate.
    assert _within_dedup_window(base, base + dt.timedelta(hours=config.DEDUP_WINDOW_HOURS))
    # A missing timestamp falls back to fingerprint-only behaviour.
    assert _within_dedup_window(None, base)
    assert _within_dedup_window(base, None)


def test_corrupt_pdf_is_settled_not_retried_forever():
    """
    A file whose bytes are not a PDF can never parse. Observed on real data:
    two filings returned "No /Root object!" on every cycle, taking a retry slot
    each time. That must settle as SKIPPED, not FAILED-with-retry.
    """
    rec = Recorder()
    agent.db.record_analysis = rec.record_analysis
    agent.db.save_alert = rec.save_alert
    agent.db.claim_document = rec.claim_document

    tmpdir = tempfile.mkdtemp()
    bad = os.path.join(tmpdir, "TESTCO_corrupt.pdf")
    with open(bad, "wb") as fh:
        fh.write(b"this is an HTML error page, not a PDF at all")

    msg = agent.process_filing(make_filing(bad, title="Financial Results"))

    last = rec.analyses[-1]
    assert last["status"] == "SKIPPED", "corrupt PDF should settle, got {}".format(last["status"])
    assert "unreadable" in last["skip_reason"]
    assert "bump_retry" not in last or not last.get("bump_retry")
    assert "BAD" in msg


def test_transient_errors_are_still_retried():
    """A 429 must NOT be mistaken for a permanent failure."""
    assert not agent.is_permanent_failure(RuntimeError("openai 429 rate limit"))
    assert not agent.is_permanent_failure(ConnectionError("connection reset by peer"))
    assert not agent.is_permanent_failure(TimeoutError("read timed out"))


def test_corrupt_pdf_signatures_are_recognised():
    assert agent.is_permanent_failure(Exception("No /Root object! - Is this really a PDF?"))
    assert agent.is_permanent_failure(Exception("EOF marker not found"))
    assert agent.is_permanent_failure(Exception("Cannot read an empty file"))

    class PdfReadError(Exception):
        pass
    assert agent.is_permanent_failure(PdfReadError("bad xref"))


def test_missing_pdf_is_recorded_not_crashed():
    rec = Recorder()
    agent.db.record_analysis = rec.record_analysis
    agent.db.save_alert = rec.save_alert
    agent.db.claim_document = rec.claim_document

    msg = agent.process_filing(make_filing("does/not/exist.pdf"))

    assert rec.analyses[-1]["status"] == "SKIPPED"
    assert "not found" in rec.analyses[-1]["skip_reason"]
    assert "SKIP" in msg


def test_model_failure_is_recorded_as_failed_and_retried():
    rec = Recorder()
    orig = extractor_module.extract_signals

    def _boom(text, symbol="", title=""):
        raise RuntimeError("openai 429 rate limit")

    agent.db.record_analysis = rec.record_analysis
    agent.db.save_alert = rec.save_alert
    agent.db.claim_document = rec.claim_document
    agent.extractor.extract_signals = _boom

    tmpdir = tempfile.mkdtemp()
    pdf = write_pdf(os.path.join(tmpdir, "TESTCO_results.pdf"), RESULTS_LINES)
    try:
        msg = agent.process_filing(make_filing(pdf, title="Financial Results"))
    finally:
        agent.extractor.extract_signals = orig

    assert rec.alerts == []
    last = rec.analyses[-1]
    assert last["status"] == "FAILED"
    assert "429" in last["error"]
    # bump_retry is what lets fetch_unanalyzed pick this filing up again.
    assert last["bump_retry"] is True
    assert "FAIL" in msg


def test_path_resolution_handles_a_relative_scraper_path():
    import config
    tmpdir = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmpdir, "storage", "pdf"), exist_ok=True)
    rel = "storage/pdf/RELTEST.pdf"
    write_pdf(os.path.join(tmpdir, rel), RESULTS_LINES)

    old = config.SCRAPER_BASE_PATH
    config.SCRAPER_BASE_PATH = tmpdir
    try:
        resolved = agent.resolve_pdf_path(rel)
        assert resolved is not None, "relative local_path did not resolve"
        assert os.path.isfile(resolved)
        # Backslashes are what actually arrives from a Windows-side scraper.
        assert agent.resolve_pdf_path("storage\\pdf\\RELTEST.pdf") is not None
    finally:
        config.SCRAPER_BASE_PATH = old


def test_path_resolution_falls_back_to_storage_dir_by_basename():
    import config
    tmpdir = tempfile.mkdtemp()
    write_pdf(os.path.join(tmpdir, "FALLBACK.pdf"), RESULTS_LINES)

    old_base, old_store = config.SCRAPER_BASE_PATH, config.PDF_STORAGE_PATH
    config.SCRAPER_BASE_PATH = "/nowhere/at/all"
    config.PDF_STORAGE_PATH = tmpdir
    try:
        # A stale path from a different host still resolves by basename.
        assert agent.resolve_pdf_path("/some/other/host/path/FALLBACK.pdf") is not None
    finally:
        config.SCRAPER_BASE_PATH, config.PDF_STORAGE_PATH = old_base, old_store


def test_resolve_handles_empty_path():
    assert agent.resolve_pdf_path(None) is None
    assert agent.resolve_pdf_path("") is None
    assert agent.resolve_pdf_path("   ") is None


# -----------------------------------------------------------------------------

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
            print("  FAIL  {}\n        {}".format(name, e))
            failed += 1
        except Exception as e:
            print("  ERROR {}  {}: {}".format(name, type(e).__name__, e))
            failed += 1
    print("\n{} passed, {} failed".format(passed, failed))
    raise SystemExit(1 if failed else 0)

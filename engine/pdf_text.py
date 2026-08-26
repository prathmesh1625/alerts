"""
pdf_text.py — get readable text out of an NSE/BSE filing PDF.

This is a focused port of the extraction half of `shares/bot/output.py`, which
has already been tuned against real filings. It is copied rather than imported
so this service can be built and deployed on its own, without the bot's image
on the path — but the two should be kept in step if that one is retuned.

The pipeline, cheapest first:

    pypdf  ->  pdfplumber  ->  OCR (PyMuPDF raster + tesseract)

with OCR paid for only when the text layer genuinely cannot do the job: a
broken font encoding that decodes to mojibake, almost no text at all, or a
document that plainly isn't the results table we were promised. A normal
text-layer filing never touches OCR.
"""
import hashlib
import io
import os
import re
import sys
import time

import config

# --- tunables (env-overridable, see config.py) ------------------------------
OCR_ENABLED = config.OCR_ENABLED
OCR_DPI = config.OCR_DPI
OCR_LANGS = config.OCR_LANGS
OCR_MAX_PAGES = config.OCR_MAX_PAGES
OCR_TIME_BUDGET_SEC = config.OCR_TIME_BUDGET_SEC

# A page with less real text than this contributes nothing and is a scan.
_MIN_PAGE_CHARS = 40
# Below this much usable text in the WHOLE document, treat it as scanned.
_MIN_USABLE_CHARS = 600

_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_VOWEL_RE = re.compile(r"[aeiouyAEIOUY]")
# Thresholds calibrated in bot/output.py on 109 real filing pages: the one page
# with a broken font encoding scored 0.38 on the vowel ratio and 0.112 on
# control characters; all 108 legitimate pages scored >= 0.75 and exactly 0.0.
_GARBLE_MIN_TOKENS = 20
_GARBLE_VOWEL_RATIO = 0.60
_GARBLE_CTRL_RATIO = 0.01

_ocr_unavailable = False  # set once, so a missing tesseract isn't retried


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# -----------------------------------------------------------------------------
#  Per-page extraction
# -----------------------------------------------------------------------------

def _extract_pages_pypdf(pdf_path: str):
    """Per-page text via pypdf (fast, doesn't stall on vector diagrams), or None."""
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:
        _log("[pdf] pypdf extraction failed: {} - falling back to pdfplumber".format(e))
        return None


def _extract_pages_pdfplumber(pdf_path: str):
    """
    Per-page text via pdfplumber, skipping the slow extract_tables() geometry.

    Page-capped: pdfminer holds the whole document in memory, so a file that is
    modest on disk but hundreds of pages once expanded is exactly the shape that
    gets a container OOM-killed.
    """
    import pdfplumber
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= config.PDFPLUMBER_MAX_PAGES:
                _log("[pdf] stopping pdfplumber at {} pages".format(
                    config.PDFPLUMBER_MAX_PAGES))
                break
            out.append(page.extract_text() or "")
    return out


def text_looks_garbled(text: str) -> bool:
    """
    True when a page HAS text but it decoded to mojibake — a PDF whose embedded
    font carries a broken encoding, which is how scanned newspaper cuttings
    reach us. Such text is worse than no text: it passes every "is there text?"
    check and then poisons the extraction.
    """
    if not text:
        return False
    ctrl = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r")
    if ctrl / len(text) > _GARBLE_CTRL_RATIO:
        return True
    tokens = _WORD_RE.findall(text)
    if len(tokens) < _GARBLE_MIN_TOKENS:
        return False  # too little to judge — thin, not garbled
    voweled = sum(1 for w in tokens if _VOWEL_RE.search(w))
    return (voweled / len(tokens)) < _GARBLE_VOWEL_RATIO


def page_text_is_usable(text: str) -> bool:
    """True when a page's extracted text is worth feeding to the model."""
    return len((text or "").strip()) >= _MIN_PAGE_CHARS and not text_looks_garbled(text)


# -----------------------------------------------------------------------------
#  OCR fallback
# -----------------------------------------------------------------------------

def _ocr_pages(pdf_path: str, page_indexes, require_image: bool = True) -> dict:
    """
    OCR the given 0-based page indexes and return {index: text}.

    Any missing dependency (no PyMuPDF wheel, no tesseract binary in the image)
    is logged once and degrades to "no OCR" — never an exception, because a
    scanned filing should still produce a partial result rather than a crash.
    """
    global _ocr_unavailable
    if _ocr_unavailable or not page_indexes:
        return {}
    try:
        try:
            import pymupdf  # PyMuPDF >= 1.24
        except ImportError:
            import fitz as pymupdf  # older name
        import pytesseract
        from PIL import Image
    except Exception as e:
        _ocr_unavailable = True
        _log("[pdf] OCR unavailable ({}) - scanned pages will be skipped".format(e))
        return {}

    out = {}
    started = time.monotonic()
    doc = None
    try:
        doc = pymupdf.open(pdf_path)
        for idx in list(page_indexes)[:OCR_MAX_PAGES]:
            if time.monotonic() - started > OCR_TIME_BUDGET_SEC:
                _log("[pdf] OCR budget ({}s) reached after {} page(s)".format(
                    OCR_TIME_BUDGET_SEC, len(out)))
                break
            try:
                page = doc[idx]
                if require_image and not page.get_images():
                    continue  # nothing scanned on this page
                pix = page.get_pixmap(dpi=OCR_DPI)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                txt = pytesseract.image_to_string(img, lang=OCR_LANGS) or ""
                if txt.strip():
                    out[idx] = txt
            except pytesseract.TesseractNotFoundError:
                _ocr_unavailable = True
                _log("[pdf] tesseract binary not found - install tesseract-ocr")
                break
            except Exception as e:
                _log("[pdf] OCR failed on page {}: {}".format(idx + 1, e))
    except Exception as e:
        _log("[pdf] OCR could not open {}: {}".format(os.path.basename(pdf_path), e))
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    if out:
        _log("[pdf] OCR recovered {} chars from {} scanned page(s)".format(
            sum(len(t) for t in out.values()), len(out)))
    return out


# -----------------------------------------------------------------------------
#  "Is this even a results table?" — also used by prefilter.py
# -----------------------------------------------------------------------------

_METRIC_TERMS = (
    "revenue from operations", "total income", "profit before tax",
    "profit after tax", "net profit", "earnings per share", "ebitda",
    "total expenses", "other income", "finance costs", "basic and diluted",
    "quarter ended", "half year ended", "year ended",
)


def count_metric_terms(text: str) -> int:
    low = (text or "").lower()
    return sum(1 for t in _METRIC_TERMS if t in low)


def looks_like_financial_results(text: str) -> bool:
    """Cheap check that a document actually carries a results statement."""
    return count_metric_terms(text) >= 3


# -----------------------------------------------------------------------------
#  Public entry point
# -----------------------------------------------------------------------------

def extract_text_from_pdf_file(pdf_path: str, report=None) -> str:
    """
    All text from a PDF: the embedded text layer, with OCR filling in pages that
    have none or whose layer decoded to mojibake.

    Pass `report` (a dict) to receive extraction diagnostics without parsing
    stderr — the /debug endpoint and run_once.py use this.
    """
    # A big document skips BOTH expensive fallbacks. They have no memory
    # ceiling of their own, and being OOM-killed is not an error we can catch —
    # the process just disappears and the container restarts.
    try:
        size = os.path.getsize(pdf_path)
    except OSError:
        size = 0
    heavy = size > config.PDF_HEAVY_PARSE_MAX_BYTES
    if heavy:
        _log("[pdf] {} is {:.1f} MB - pypdf only, no pdfplumber or OCR".format(
            os.path.basename(pdf_path), size / 1e6))

    pages = _extract_pages_pypdf(pdf_path)
    if pages is None:
        if heavy:
            return ""
        pages = _extract_pages_pdfplumber(pdf_path)
    elif not heavy and len("".join(pages).strip()) <= 100:
        # pypdf found next to nothing — pdfplumber sometimes does better on the
        # same file, so try it before paying for OCR.
        try:
            plumbed = _extract_pages_pdfplumber(pdf_path)
            if len("".join(plumbed).strip()) > len("".join(pages).strip()):
                pages = plumbed
        except Exception as e:
            _log("[pdf] pdfplumber extraction failed: {}".format(e))

    usable = [t for t in pages if page_text_is_usable(t)]
    usable_text = "\n".join(usable)
    bad_indexes = [i for i, t in enumerate(pages) if not page_text_is_usable(t)]
    garbled_seen = any(text_looks_garbled(t) for t in pages)

    if report is not None:
        report.update({
            "pages": len(pages),
            "usable_pages": len(usable),
            "garbled_pages": [i + 1 for i, t in enumerate(pages) if text_looks_garbled(t)],
            "text_layer_chars": len(usable_text.strip()),
            "ocr_pages": [],
            "ocr_chars": 0,
        })

    needs_ocr = bad_indexes and OCR_ENABLED and not heavy and (
        garbled_seen
        or len(usable_text.strip()) < _MIN_USABLE_CHARS
        or not looks_like_financial_results(usable_text)
    )
    if not needs_ocr:
        return usable_text if usable else "\n".join(pages)

    ocr_text = _ocr_pages(pdf_path, bad_indexes, require_image=bool(usable))
    if report is not None:
        report["ocr_pages"] = [i + 1 for i in sorted(ocr_text)]
        report["ocr_chars"] = sum(len(t) for t in ocr_text.values())

    # Rebuild in page order: OCR replaces an unusable page, a usable text layer
    # always wins over OCR (it is exact, where OCR guesses glyphs).
    merged = []
    for i, t in enumerate(pages):
        if page_text_is_usable(t):
            merged.append(t)
        elif i in ocr_text:
            merged.append(ocr_text[i])

    if not merged:
        _log("[pdf] no usable text in {} ({} page(s)) and OCR recovered nothing".format(
            os.path.basename(pdf_path), len(pages)))
    return "\n".join(merged)


# -----------------------------------------------------------------------------
#  Document fingerprint — cross-exchange identity
# -----------------------------------------------------------------------------

_FLATTEN = re.compile(r"[^a-z0-9]+")


def document_fingerprint(text: str) -> str:
    """
    A hash of the document's own text, identifying the DOCUMENT rather than the
    words an exchange chose to file it under. Returns "" when it can't be
    computed.

    NSE and BSE publish the same filing under different pdf_urls, so the
    scraper stores TWO `announcements` rows for one event. Without this, the
    agent would pay OpenAI twice for the same PDF and put two identical cards
    on the dashboard.

    The title cannot do this job — the two exchanges describe one filing from
    their own taxonomies, and a match loose enough to unite them also unites
    filings that are genuinely different. The documents behind them are the
    same document, so hash the document. (Same reasoning, and the same
    normalisation, as `_document_fingerprint` in bot/db_watcher.py.)

    A document with too little text to identify safely gets no fingerprint and
    is simply not deduped, rather than being wrongly merged with something else.
    Callers additionally bound a match by time — see config.DEDUP_WINDOW_HOURS.
    """
    flat = _FLATTEN.sub("", (text or "").lower())
    if len(flat) < config.FINGERPRINT_MIN_CHARS:
        return ""
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16]


# -----------------------------------------------------------------------------
#  Consolidated-first reordering
# -----------------------------------------------------------------------------

_CONSOLIDATED_RE = re.compile(
    r"^.{0,80}\bconsolidated\b.{0,120}$", re.IGNORECASE | re.MULTILINE
)


def consolidated_first(text: str) -> str:
    """
    Indian filings print the STANDALONE statement first and the CONSOLIDATED
    (group) one after it. We report consolidated where it exists, but on a long
    filing that table would be cut off by the character cap — so move it to the
    front and let standalone be the part that gets truncated.
    """
    if not text:
        return text
    m = _CONSOLIDATED_RE.search(text)
    if not m or m.start() < 2000:
        return text  # no consolidated section, or it's already near the top
    return text[m.start():] + "\n\n" + text[:m.start()]

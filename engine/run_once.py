"""
run_once.py — run the whole pipeline against a single PDF and print what happened.

This is the tool for answering "why did/didn't this filing alert?" without
waiting for a worker cycle or touching the database. It prints the extraction
diagnostics, the prefilter decision, the raw signals the model read, and the
full per-rule scoring breakdown.

    python run_once.py "D:/prathmesh/shares/storage/pdf/SUZLON_2025-10-30.pdf"
    python run_once.py path/to.pdf --title "Outcome of Board Meeting"
    python run_once.py path/to.pdf --no-llm      # extraction + prefilter only
"""
import argparse
import json
import os
import sys

import config
import scoring
from pdf_text import extract_text_from_pdf_file
from prefilter import should_analyze
from units import detect_statement_unit


def _rule_line(r: dict) -> str:
    mark = "HIT " if r["hit"] else "miss"
    line = "  [{}] {:<26} {:>14}   {:>6.2f} / {:.0f} pts".format(
        mark, r["label"], r["display"], r["points"], r["weight"]
    )
    if r["note"]:
        line += "\n         note: {}".format(r["note"])
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyse one filing PDF")
    ap.add_argument("pdf", help="path to the PDF")
    ap.add_argument("--symbol", default="", help="company symbol, for the prompt")
    ap.add_argument("--title", default="", help="announcement title, for the prefilter")
    ap.add_argument("--no-llm", action="store_true",
                    help="stop after extraction and the prefilter decision")
    ap.add_argument("--dump-text", metavar="FILE",
                    help="write the extracted text here for inspection")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf):
        print("No such file: {}".format(args.pdf), file=sys.stderr)
        return 1

    print("=" * 70)
    print("FILE   : {}".format(args.pdf))
    print("SYMBOL : {}".format(args.symbol or "(none)"))
    print("TITLE  : {}".format(args.title or "(none)"))
    print("=" * 70)

    report = {}
    text = extract_text_from_pdf_file(args.pdf, report=report)

    print("\n--- EXTRACTION ---")
    print("  pages           : {}".format(report.get("pages")))
    print("  usable pages    : {}".format(report.get("usable_pages")))
    print("  garbled pages   : {}".format(report.get("garbled_pages")))
    print("  text-layer chars: {:,}".format(report.get("text_layer_chars", 0)))
    print("  OCR'd pages     : {} ({:,} chars)".format(
        report.get("ocr_pages"), report.get("ocr_chars", 0)))
    print("  total chars     : {:,}".format(len(text)))

    if args.dump_text:
        with open(args.dump_text, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("  text written to : {}".format(args.dump_text))

    ok, reason = should_analyze(args.title, text)
    print("\n--- PREFILTER ---")
    print("  send to model   : {}".format("YES" if ok else "NO"))
    print("  reason          : {}".format(reason))

    if not ok:
        print("\nRESULT: SKIPPED (no model call, no alert)")
        return 0

    if args.no_llm:
        print("\nStopped before the model call (--no-llm).")
        return 0

    import extractor  # imported late so --no-llm works without an API key

    print("\n--- MODEL ({}) ---".format(config.OPENAI_MODEL))
    signals = extractor.extract_signals(text, symbol=args.symbol, title=args.title)
    print(json.dumps(extractor.signals_to_json(signals), indent=2)[:4000])

    result = scoring.score_filing(signals, text)

    print("\n--- UNITS ---")
    print("  model reported  : {}".format(signals.statement_unit or "(none)"))
    print("  detected in doc : {}".format(detect_statement_unit(text) or "(none)"))
    print("  USED            : {}   <- the document's own heading wins".format(
        result["breakdown"].get("statement_unit") or "(none detected)"))

    print("\n--- FORMULA ---")
    for r in result["breakdown"]["rules"]:
        print(_rule_line(r))

    print("\n  TOTAL SCORE   : {:.2f} / {:.0f}".format(
        result["score"], result["breakdown"]["max_possible"]))
    print("  CONVICTION    : {}".format(result["conviction"]))
    print("  RULES HIT     : {}".format(", ".join(result["rules_hit"]) or "none"))
    print("  HEADLINE      : {}".format(result["headline"]))
    print("\nRESULT: {}".format(
        "ALERT - this would appear on the dashboard" if result["qualifies"]
        else "no alert (score below {:.0f})".format(config.ALERT_MIN_SCORE)
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
notifier.py — deliver dashboard alerts over WhatsApp.

Runs as its own loop, separate from the agent. That separation is the point:
scoring a filing and telling someone about it fail for unrelated reasons, and a
WhatsApp outage, a closed 24-hour window or a revoked token must not stop
filings being analysed or the dashboard being written.

Run:  python notifier.py
      python notifier.py --once      one pass, then exit
      python notifier.py --dry-run   print what would be sent, send nothing

This sends from the same Meta phone number as the NSE bot, which carries 130
paying users. See config.py for the brakes that exist because of that.
"""
import sys
import time

import config
import db
import whatsapp

MARKER = {"STRONG": "🟢", "MODERATE": "🟡", "WATCH": "⚪"}


def _fmt_cr(v) -> str:
    if v is None:
        return ""
    v = float(v)
    if v >= 1000:
        return "Rs {:,.0f} Cr".format(v)
    return "Rs {:,.2f} Cr".format(v).replace(".00 Cr", " Cr")


def _pdf_link(alert: dict) -> str:
    base = (config.PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return alert.get("pdf_url") or ""
    return "{}/api/alerts/{}/pdf".format(base, alert["announcement_id"])


def build_message(alert: dict) -> str:
    """
    The text a person actually reads.

    Written to be judged in the notification preview, where perhaps eighty
    characters are visible: symbol and what happened come first, the reasoning
    after. Everything here is already in the alert row — the notifier does no
    scoring of its own, so a message can never disagree with the dashboard.
    """
    mark = MARKER.get(alert.get("conviction"), "•")
    lines = ["{} {} · {}".format(mark, alert.get("company_symbol", "?"),
                                 alert.get("conviction", ""))]

    name = alert.get("company_name")
    if name:
        lines.append(name)

    lines.append("")
    lines.append(alert.get("headline") or "Formula rule cleared")

    detail = []
    order = alert.get("order_value_cr")
    headline = alert.get("headline") or ""
    # The headline already reads "Order win Rs 164.79 Cr" for an order alert,
    # so repeating the figure on the next line reads as two separate numbers.
    if order is not None and _fmt_cr(order) not in headline:
        detail.append("Order {}".format(_fmt_cr(order)))
    cap = alert.get("market_cap_cr")
    if cap is not None:
        detail.append("Market cap {}".format(_fmt_cr(cap)))
    if alert.get("score") is not None:
        detail.append("Score {:.1f}".format(float(alert["score"])))
    if detail:
        lines.append(" · ".join(detail))

    when = alert.get("announced_at")
    if hasattr(when, "strftime"):
        lines.append("Filed {} IST".format(when.strftime("%d %b %Y, %H:%M")))

    link = _pdf_link(alert)
    if link:
        lines.append("")
        lines.append("🔗 {}".format(link))
    return "\n".join(lines)


def build_template_params(alert: dict) -> list:
    """
    The same alert as five variables for the approved `nse_bot` template:
    {{1}} headline, {{2}} company, {{3}} event + time, {{4}} detail, {{5}} link.

    Used only when Meta says the 24-hour window has closed.
    """
    when = alert.get("announced_at")
    stamp = when.strftime("%d %b %Y, %H:%M") if hasattr(when, "strftime") else ""
    detail = []
    if alert.get("order_value_cr") is not None:
        detail.append("Order value {}".format(_fmt_cr(alert["order_value_cr"])))
    if alert.get("market_cap_cr") is not None:
        detail.append("Market cap {}".format(_fmt_cr(alert["market_cap_cr"])))
    if alert.get("score") is not None:
        detail.append("Score {:.1f} ({})".format(
            float(alert["score"]), alert.get("conviction", "")))

    return [
        "{} — {}".format(alert.get("company_symbol", "?"),
                         alert.get("headline") or "Formula rule cleared"),
        alert.get("company_name") or alert.get("company_symbol") or "",
        "Filed on exchange: {} IST".format(stamp) if stamp else "Filed on exchange",
        " · ".join(detail) or "See the filing for details",
        _pdf_link(alert),
    ]


def _preflight() -> bool:
    """
    Refuse to run rather than half-run. Each of these is a way of sending to
    the wrong people or not sending at all, and all are silent at runtime.
    """
    if not config.WHATSAPP_ENABLED:
        print("[notifier] WHATSAPP_ENABLED is false - nothing to do", flush=True)
        return False
    problems = []
    if not config.WHATSAPP_TOKEN:
        problems.append("WHATSAPP_TOKEN is empty")
    if not config.WHATSAPP_PHONE_NUMBER_ID:
        problems.append("WHATSAPP_PHONE_NUMBER_ID is empty")
    if not config.WHATSAPP_RECIPIENTS:
        problems.append("WHATSAPP_RECIPIENTS is empty (there is no default "
                        "audience - name the numbers explicitly)")
    if problems:
        for p in problems:
            print("[notifier] REFUSING TO START: {}".format(p), flush=True)
        return False

    print("[notifier] sending to {} recipient(s), score >= {}, cap {}/day"
          .format(len(config.WHATSAPP_RECIPIENTS), config.WHATSAPP_MIN_SCORE,
                  config.WHATSAPP_MAX_PER_DAY), flush=True)
    return True


def deliver_one(phone: str, alert: dict, dry_run: bool = False) -> bool:
    """Send a single alert. True if it went out (or would have)."""
    text = build_message(alert)
    if dry_run:
        print("\n--- would send to {} ---\n{}\n".format(phone, text), flush=True)
        return True

    channel, wamid = whatsapp.send(phone, text, build_template_params(alert))
    # Recorded only AFTER the API confirms, so a failure is retried rather
    # than marked delivered.
    db.mark_notified(phone, alert["announcement_id"], channel, wamid)
    print("[notifier] sent {} to {} via {} ({}){}".format(
        alert.get("company_symbol"), phone, channel, wamid or "no id",
        latency_note(alert)), flush=True)
    return True


def latency_note(alert: dict) -> str:
    """
    Where this message's time went, split at the only boundary that matters.

    "analysis" is everything upstream of us — the scraper noticing the filing,
    the PDF download and its retries, extraction, the model. "queue" is the
    notifier's own poll. Printing both means the next slow message says which
    half was responsible instead of leaving it to be inferred.
    """
    import datetime

    announced = alert.get("announced_at")
    scored = alert.get("created_at")
    if not (hasattr(announced, "timestamp") and hasattr(scored, "timestamp")):
        return ""
    now = datetime.datetime.now(scored.tzinfo) if scored.tzinfo else \
        datetime.datetime.now()
    # announced_at is naive IST; created_at is tz-aware. Compare each against
    # its own clock rather than subtracting one from the other.
    try:
        queue = (now - scored).total_seconds()
        analysis = (scored.replace(tzinfo=None) - announced).total_seconds()
    except Exception:
        return ""
    return "  [analysis {:.0f}s + queue {:.0f}s = {:.0f}s]".format(
        analysis, queue, analysis + queue)


def run_once(dry_run: bool = False) -> int:
    """One pass over every recipient. Returns how many messages went out."""
    sent = 0
    for phone in config.WHATSAPP_RECIPIENTS:
        try:
            already = db.count_notified_today(phone)
        except Exception as e:
            print("[notifier] cannot read today's count for {}: {}".format(phone, e),
                  flush=True)
            continue

        room = config.WHATSAPP_MAX_PER_DAY - already
        if room <= 0:
            print("[notifier] daily cap reached for {} ({})".format(
                phone, config.WHATSAPP_MAX_PER_DAY), flush=True)
            continue

        try:
            pending = db.fetch_unnotified_alerts(
                phone, config.WHATSAPP_MIN_SCORE, config.WHATSAPP_MAX_AGE_MIN,
                limit=room)
        except Exception as e:
            print("[notifier] cannot read pending alerts: {}".format(e), flush=True)
            continue

        for alert in pending:
            try:
                if deliver_one(phone, alert, dry_run):
                    sent += 1
            except whatsapp.WhatsAppError as e:
                # Not recorded, so it is retried next pass. Stop this
                # recipient for now: the next alert would almost certainly
                # hit the same wall.
                print("[notifier] send failed for {} ({}): {}".format(
                    phone, alert.get("company_symbol"), e), flush=True)
                break
            except Exception as e:
                print("[notifier] unexpected failure for {}: {}: {}".format(
                    phone, type(e).__name__, e), flush=True)
                break
    return sent


def main():
    once = "--once" in sys.argv
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        # A dry run must work before anything is switched on, so it skips the
        # credential checks and only needs an audience to address.
        if not config.WHATSAPP_RECIPIENTS:
            print("[notifier] set WHATSAPP_RECIPIENTS to dry-run", flush=True)
            return
    elif not _preflight():
        return

    db.ensure_schema()
    if once or dry_run:
        n = run_once(dry_run)
        print("[notifier] {} message(s)".format(n), flush=True)
        return

    while True:
        try:
            run_once()
        except Exception as e:
            print("[notifier] pass failed: {}: {}".format(type(e).__name__, e),
                  flush=True)
        time.sleep(config.WHATSAPP_POLL_SEC)


if __name__ == "__main__":
    main()

"""
volume.py — rule 4: a sudden spike in traded volume.

Deliberately separate from the three filing rules in scoring.py. Those score a
DOCUMENT; this scores a stock's trading history, has no filing attached, and
must not disturb a scoring model that is already working. Nothing here touches
`stock_alerts` or changes any existing score.

WHAT COUNTS AS "SUDDEN"

The hard part is not spotting high volume — it is refusing to fire on volume
that is merely elevated. A stock whose turnover drifts up and down all month
should stay quiet; only a genuine break from its own pattern should alert. So a
spike must clear THREE independent tests:

  1. MAGNITUDE — at least SPIKE_MIN_X times the MEDIAN of the trailing window.
     Median, not mean: one spike three weeks ago would drag a mean baseline up
     and mask today's, which is precisely the failure this rule exists to avoid.

  2. NOVELTY — today must be the highest volume in the whole window. This is
     what separates "sudden" from "busy lately". A stock that has been running
     hot for a week has a high median AND a high recent max, so it stops
     alerting rather than alerting every day.

  3. LIQUIDITY — floors on today's turnover and on the baseline itself. Without
     these, an illiquid microcap going from 40 shares to 900 is a 22x "spike"
     and pure noise. This is the test that removes most false positives.

All of it is pure arithmetic on a list of daily volumes, so it can be tested
against hand-built histories. See test_volume.py.
"""
import math
import statistics

import config


def _median(values):
    return statistics.median(values) if values else None


def summarise_baseline(history):
    """
    Describe the trailing window a spike is judged against.

    `history` is prior sessions only — most recent first or last does not
    matter, but TODAY must not be in it, or the spike would be compared against
    itself and could never clear the threshold.
    """
    vols = [v for v in history if v is not None and v > 0]
    return {
        "sessions": len(vols),
        "median": _median(vols),
        "max": max(vols) if vols else None,
        "mean": (sum(vols) / len(vols)) if vols else None,
    }


def detect_spike(symbol, today, history, sessions_since_last_alert=None):
    """
    Decide whether `today` is a sudden volume spike for this stock.

    `today` is a dict with volume / turnover_cr / close / prev_close.
    `history` is the list of prior daily volumes, today excluded.
    `sessions_since_last_alert` is None if this stock has never alerted.

    Returns a verdict dict that always explains itself — `reason` says why it
    did NOT fire, which is what makes a quiet day auditable rather than opaque.
    """
    volume = today.get("volume")
    turnover = today.get("turnover_cr")
    close = today.get("close")
    prev_close = today.get("prev_close")

    base = summarise_baseline(history)
    pct_change = None
    if close is not None and prev_close:
        try:
            pct_change = round((close - prev_close) / prev_close * 100.0, 2)
        except ZeroDivisionError:
            pct_change = None

    verdict = {
        "symbol": symbol,
        "volume": volume,
        "turnover_cr": turnover,
        "close": close,
        "pct_change": pct_change,
        "baseline_sessions": base["sessions"],
        "baseline_median": base["median"],
        "baseline_max": base["max"],
        "ratio": None,
        "hit": False,
        "score": 0.0,
        "reason": "",
    }

    # --- enough history to have an opinion at all ---------------------------
    if base["sessions"] < config.VOLUME_MIN_SESSIONS:
        verdict["reason"] = "only {} prior session(s); need {}".format(
            base["sessions"], config.VOLUME_MIN_SESSIONS)
        return verdict

    if not volume or not base["median"]:
        verdict["reason"] = "no usable volume"
        return verdict

    ratio = round(volume / base["median"], 2)
    verdict["ratio"] = ratio

    # --- 3. liquidity floors, checked first because they are the cheapest ----
    if base["median"] < config.VOLUME_MIN_BASELINE_SHARES:
        verdict["reason"] = "baseline too thin ({:,.0f} shares/day)".format(base["median"])
        return verdict

    if turnover is not None and turnover < config.VOLUME_MIN_TURNOVER_CR:
        verdict["reason"] = "turnover Rs {:.2f} Cr below the Rs {:.0f} Cr floor".format(
            turnover, config.VOLUME_MIN_TURNOVER_CR)
        return verdict

    # --- 1. magnitude --------------------------------------------------------
    if ratio < config.VOLUME_SPIKE_MIN_X:
        verdict["reason"] = "{}x median, below {}x".format(ratio, config.VOLUME_SPIKE_MIN_X)
        return verdict

    # --- 2. novelty ----------------------------------------------------------
    if config.VOLUME_REQUIRE_NEW_HIGH and base["max"] and volume <= base["max"]:
        verdict["reason"] = "{}x median but not a {}-session high".format(
            ratio, base["sessions"])
        return verdict

    # --- 2b. cooldown --------------------------------------------------------
    # The new-high test alone does NOT stop a sustained run: on day two the
    # baseline still has a low median, and volume that edges above yesterday's
    # is technically a fresh high, so the rule fires again. Caught by
    # test_a_sustained_run_stops_alerting.
    #
    # The signal being asked for is "this stock SUDDENLY got busy", which is an
    # event, not a state. Once reported, stay quiet for a few sessions rather
    # than repeating it every day the elevated volume persists.
    if (sessions_since_last_alert is not None
            and sessions_since_last_alert < config.VOLUME_COOLDOWN_SESSIONS):
        verdict["reason"] = "already flagged {} session(s) ago".format(
            sessions_since_last_alert)
        return verdict

    # --- direction -----------------------------------------------------------
    # A spike on a collapsing price is real information, but it is not the
    # "this could go up" signal this dashboard exists to surface.
    if pct_change is not None and pct_change < config.VOLUME_MIN_PRICE_CHANGE_PCT:
        verdict["reason"] = "spike on a {:+.1f}% price move".format(pct_change)
        return verdict

    # --- it fired ------------------------------------------------------------
    # Strength is logarithmic: 5x and 50x are both spikes, but not equally
    # remarkable, and a linear ramp would flatten everything above ~10x.
    span = math.log10(max(config.VOLUME_SPIKE_FULL_X, config.VOLUME_SPIKE_MIN_X * 1.01)
                      / config.VOLUME_SPIKE_MIN_X)
    strength = min(1.0, max(0.0, math.log10(ratio / config.VOLUME_SPIKE_MIN_X) / span))

    verdict["hit"] = True
    verdict["score"] = round(
        config.VOLUME_WEIGHT * (config.BASE_CREDIT + (1 - config.BASE_CREDIT) * strength), 2)
    verdict["reason"] = "{}x its {}-session median volume".format(ratio, base["sessions"])
    verdict["headline"] = "Volume {}x normal{}".format(
        ratio, " on {:+.1f}%".format(pct_change) if pct_change is not None else "")
    return verdict


def conviction_band(score):
    """Same bands as the filing alerts, so the two read consistently."""
    if score >= config.BAND_STRONG:
        return "STRONG"
    if score >= config.BAND_MODERATE:
        return "MODERATE"
    return "WATCH"

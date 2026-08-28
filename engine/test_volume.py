"""
test_volume.py — rule 4, the sudden volume spike.

The requirement that makes this hard is not "spot high volume" but "stay quiet
when volume is merely elevated". Most of these cases are about NOT firing.

Run: pytest test_volume.py   or   python test_volume.py
"""
import config

# Rules 1 and 2 are OFF in production while the order rule is hardened. These
# tests exercise the pipeline / rule 4, not which rules are live, so they run
# with all three enabled — otherwise a profit-growth fixture silently stops
# producing the alert the test is about.
config.PROFIT_RULE_ENABLED = True
config.REVENUE_RULE_ENABLED = True
config.ORDER_RULE_ENABLED = True
config.BAND_STRONG = 70.0
config.BAND_MODERATE = 45.0
import volume


def today(vol, turnover=50.0, close=110.0, prev=100.0):
    return {"volume": vol, "turnover_cr": turnover, "close": close, "prev_close": prev}


# A calm stock: volume wanders between 90k and 130k, never breaking out.
CALM = [100_000, 110_000, 95_000, 120_000, 105_000, 98_000, 130_000, 102_000,
        115_000, 99_000, 108_000, 112_000, 94_000, 125_000, 101_000]


# -----------------------------------------------------------------------------
#  It fires on a real spike
# -----------------------------------------------------------------------------

def test_a_genuine_spike_fires():
    v = volume.detect_spike("TEST", today(600_000), CALM)
    assert v["hit"] is True, v["reason"]
    assert v["ratio"] >= 3.0
    assert v["score"] > 0
    assert "median volume" in v["reason"]


def test_bigger_spikes_score_higher():
    # Derived from the configured threshold rather than hardcoded, so retuning
    # VOLUME_SPIKE_MIN_X cannot silently invalidate this test.
    median = volume.summarise_baseline(CALM)["median"]
    just_over = median * (config.VOLUME_SPIKE_MIN_X + 0.5)
    well_over = median * config.VOLUME_SPIKE_FULL_X * 0.5
    enormous = median * config.VOLUME_SPIKE_FULL_X * 5

    small = volume.detect_spike("T", today(just_over), CALM)["score"]
    mid = volume.detect_spike("T", today(well_over), CALM)["score"]
    big = volume.detect_spike("T", today(enormous), CALM)["score"]
    assert 0 < small < mid < big, (small, mid, big)
    assert abs(big - config.VOLUME_WEIGHT) < 0.01   # past FULL_X, maxed out


# -----------------------------------------------------------------------------
#  It stays quiet on "in between" volume — the whole point of the rule
# -----------------------------------------------------------------------------

def test_merely_elevated_volume_does_not_fire():
    # 2.5x median. Busy, but not a break from its own pattern.
    v = volume.detect_spike("TEST", today(265_000), CALM)
    assert v["hit"] is False
    assert "below" in v["reason"]


def test_a_normal_day_does_not_fire():
    assert volume.detect_spike("TEST", today(112_000), CALM)["hit"] is False


def test_a_new_high_that_is_not_3x_does_not_fire():
    # 131k beats the 130k window max, but it is only ~1.2x the median.
    v = volume.detect_spike("TEST", today(131_000), CALM)
    assert v["hit"] is False


def test_3x_median_without_a_new_high_does_not_fire():
    """
    A stock that has ALREADY been running hot. Today clears the magnitude
    threshold comfortably, but the window already contains a bigger day - so it
    is not remarkable FOR THIS STOCK, and firing would alert every day of a busy
    fortnight.
    """
    median = volume.summarise_baseline(CALM)["median"]
    todays = median * (config.VOLUME_SPIKE_MIN_X + 1)     # clears magnitude
    hot = CALM + [todays * 2]                             # but the window is bigger

    v = volume.detect_spike("TEST", today(todays), hot)
    assert v["hit"] is False
    assert "not a" in v["reason"] and "high" in v["reason"], v["reason"]


def test_a_sustained_run_stops_alerting():
    """The second, third and fourth days of a run must go quiet."""
    history = list(CALM)
    first = volume.detect_spike("T", today(600_000), history)
    assert first["hit"] is True

    history.append(600_000)                      # yesterday's spike is now history
    # Day two edges HIGHER, so it is technically a fresh high and the novelty
    # test alone would let it through. The cooldown is what stops it.
    second = volume.detect_spike("T", today(620_000), history,
                                 sessions_since_last_alert=1)
    assert second["hit"] is False, "alerted on a second consecutive heavy day"
    assert "already flagged" in second["reason"]

    # Well after the cooldown, a genuinely new spike is allowed through again.
    later = volume.detect_spike("T", today(2_000_000), history,
                                sessions_since_last_alert=config.VOLUME_COOLDOWN_SESSIONS)
    assert later["hit"] is True


# -----------------------------------------------------------------------------
#  Liquidity floors — where most false positives come from
# -----------------------------------------------------------------------------

def test_illiquid_microcap_does_not_fire():
    """40 shares/day going to 900 is 22x and completely meaningless."""
    thin = [40, 55, 30, 60, 45, 35, 50, 42, 38, 61, 47, 52]
    v = volume.detect_spike("TINY", today(900, turnover=0.4), thin)
    assert v["hit"] is False
    assert "thin" in v["reason"]


def test_low_turnover_does_not_fire():
    # Plenty of shares, but a penny stock — Rs 2 Cr traded is not a signal.
    v = volume.detect_spike("PENNY", today(600_000, turnover=2.0), CALM)
    assert v["hit"] is False
    assert "turnover" in v["reason"]


# -----------------------------------------------------------------------------
#  Not enough history
# -----------------------------------------------------------------------------

def test_too_little_history_is_silent_not_wrong():
    v = volume.detect_spike("NEW", today(600_000), [100_000, 110_000, 95_000])
    assert v["hit"] is False
    assert "prior session" in v["reason"]
    assert v["ratio"] is None


def test_no_history_at_all_is_safe():
    v = volume.detect_spike("NEW", today(600_000), [])
    assert v["hit"] is False


# -----------------------------------------------------------------------------
#  Direction
# -----------------------------------------------------------------------------

def test_spike_on_a_falling_price_does_not_fire_by_default():
    v = volume.detect_spike("T", today(600_000, close=88.0, prev=100.0), CALM)
    assert v["hit"] is False
    assert "price move" in v["reason"]


def test_direction_filter_can_be_turned_off():
    old = config.VOLUME_MIN_PRICE_CHANGE_PCT
    config.VOLUME_MIN_PRICE_CHANGE_PCT = -100.0
    try:
        v = volume.detect_spike("T", today(600_000, close=88.0, prev=100.0), CALM)
        assert v["hit"] is True
    finally:
        config.VOLUME_MIN_PRICE_CHANGE_PCT = old


# -----------------------------------------------------------------------------
#  Median, not mean — the reason a prior spike cannot mask today's
# -----------------------------------------------------------------------------

def test_a_prior_spike_does_not_mask_a_later_one():
    """
    One 5,000,000-share day three weeks ago drags the MEAN to ~430k, which would
    make today's 600k look like 1.4x and hide it. The median is unmoved, so the
    spike is still seen.
    """
    with_old_spike = CALM + [5_000_000]
    base = volume.summarise_baseline(with_old_spike)
    assert base["mean"] > 400_000
    assert base["median"] < 130_000

    # It still cannot fire, because 600k is not a new high against 5,000,000 -
    # correctly so. Lower the old spike and it fires immediately.
    modest_old = CALM + [400_000]
    v = volume.detect_spike("T", today(600_000), modest_old)
    assert v["hit"] is True


def test_baseline_summary_ignores_blank_sessions():
    b = volume.summarise_baseline([100, None, 200, 0, 300])
    assert b["sessions"] == 3
    assert b["median"] == 200


# -----------------------------------------------------------------------------
#  Rule 4 must not disturb rules 1-3
# -----------------------------------------------------------------------------

def test_the_filing_rules_are_untouched():
    """
    Rule 4 is scored separately and written to its own table. The three filing
    weights must still sum to 100, or existing alert scores would shift.
    """
    assert config.PROFIT_WEIGHT + config.REVENUE_WEIGHT + config.ORDER_WEIGHT == 100.0

    import scoring
    from signals import FilingSignals, MetricYoY, PeriodFigure
    s = FilingSignals(
        document_type="RESULTS",
        profit=MetricYoY(current=PeriodFigure(raw_value=200.0, unit="crore"),
                         year_ago=PeriodFigure(raw_value=100.0, unit="crore")),
    )
    r = scoring.score_filing(s)
    # +100% profit growth maxes rule 1 at its full 35 points, exactly as before.
    assert abs(r["score"] - config.PROFIT_WEIGHT) < 0.01
    assert r["rules_hit"] == ["PROFIT_GROWTH"]


# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
#  Intraday detection
#
#  The same detect_spike runs during the session against volume-so-far. There
#  is deliberately NO scaling for how much of the session has elapsed: intraday
#  volume is U-shaped, so scaling a daily baseline by "fraction elapsed" reports
#  every normal stock as a 2-3x spike in the first fifteen minutes.
# -----------------------------------------------------------------------------

def test_partial_day_volume_below_the_bar_stays_quiet():
    """
    Mid-morning, a stock has traded twice its usual FULL day. Heavy, but not yet
    past the 5x bar — and it must not be scaled up to look like one.
    """
    median = volume.summarise_baseline(CALM)["median"]
    v = volume.detect_spike("T", today(median * 2), CALM)
    assert v["hit"] is False
    assert "below" in v["reason"]


def test_partial_day_volume_over_the_bar_fires_immediately():
    """
    By 11am it has already traded 6x a normal WHOLE day. That is unambiguous
    without knowing the time — which is exactly why no curve is needed.
    """
    median = volume.summarise_baseline(CALM)["median"]
    v = volume.detect_spike("T", today(median * 6), CALM)
    assert v["hit"] is True


def test_no_time_of_day_input_is_required():
    """
    detect_spike takes no clock. If it ever grows one, the U-shaped-curve
    problem comes back with it — this test is here to make that a deliberate
    decision rather than an accident.
    """
    import inspect
    params = set(inspect.signature(volume.detect_spike).parameters)
    assert not (params & {"now", "session_fraction", "elapsed", "time_of_day"}), \
        "detect_spike gained a time input; see the U-curve note in config.py"


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

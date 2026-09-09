"""Tests for deviation scoring against hand-computed answers."""

from __future__ import annotations

import numpy as np
import pytest

from dosojos_sat.baseline import (
    CONFIDENCE_MULTIPLIER,
    ObservationScore,
    field_stress_score,
    percentile_of,
    rank_fields,
    robust_z,
    trailing_depressed_run,
    trailing_low_run,
)


def _obs(
    value: float,
    *,
    below: bool = False,
    z: float = -1.0,
    confidence: str = "high",
    day: str = "2026-09-01",
    median: float = 0.5,
) -> ObservationScore:
    """One scored observation with the fields the aggregate cares about."""
    return ObservationScore(
        obs_date=day, doy=244, value=value, baseline_median=median,
        baseline_p10=0.3, percentile=25.0, robust_z=z,
        below_p10=below, confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# Percentile
# --------------------------------------------------------------------------- #


def test_percentile_against_a_known_sample() -> None:
    """Values 0..99 put 30 at the 30th percentile."""
    sample = np.arange(100, dtype=float)
    assert percentile_of(30.0, sample) == pytest.approx(30.5)   # 30 below, one tie
    assert percentile_of(-1.0, sample) == pytest.approx(0.0)
    assert percentile_of(100.0, sample) == pytest.approx(100.0)


def test_percentile_handles_ties_by_splitting_them() -> None:
    """A value equal to half the sample lands at the midpoint, not 0 or 100."""
    assert percentile_of(1.0, np.array([1.0, 1.0, 1.0, 1.0])) == pytest.approx(50.0)


def test_percentile_of_empty_history_is_nan() -> None:
    """No history means no percentile rather than a misleading zero."""
    assert np.isnan(percentile_of(0.5, np.empty(0)))


def test_percentile_ignores_nan_history() -> None:
    """A NaN in the pooled sample must not shift the answer."""
    assert percentile_of(3.0, np.array([1.0, 2.0, np.nan, 4.0])) == pytest.approx(200 / 3)


# --------------------------------------------------------------------------- #
# Robust z
# --------------------------------------------------------------------------- #


def test_robust_z_against_a_hand_computed_value() -> None:
    """An IQR of 0.2 gives a sigma of 0.2/1.349, so -0.1 is about -0.67 sigma."""
    assert robust_z(0.4, 0.5, 0.4, 0.6) == pytest.approx(-0.1 / (0.2 / 1.349), abs=1e-6)


def test_robust_z_is_zero_at_the_median() -> None:
    """A value sitting on the median deviates by nothing."""
    assert robust_z(0.5, 0.5, 0.4, 0.6) == 0.0


def test_robust_z_survives_a_degenerate_spread() -> None:
    """A bin where every historical value is identical must not divide by zero."""
    assert robust_z(0.4, 0.5, 0.5, 0.5) == 0.0


def test_robust_z_of_missing_data_is_nan() -> None:
    """An absent baseline yields no z rather than a fabricated one."""
    assert np.isnan(robust_z(0.4, float("nan"), 0.4, 0.6))


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


def test_low_run_counts_only_the_trailing_stretch() -> None:
    """A run that ended and recovered must not keep a field flagged."""
    assert trailing_low_run([True, True, True, False]) == 0
    assert trailing_low_run([False, True, True]) == 2
    assert trailing_low_run([]) == 0
    assert trailing_low_run([True, True, True, True, True]) == 5


def test_depressed_run_is_wider_than_the_p10_run() -> None:
    """Below-median observations count even when they never breach p10.

    This is the rgv-001 case: depressed for weeks, never crossing p10 because
    the baseline's own p10 is dragged down by a drought year inside it.
    """
    scores = [_obs(0.4, below=False, z=-1.2) for _ in range(6)]
    assert trailing_low_run([s.below_p10 for s in scores]) == 0
    assert trailing_depressed_run(scores) == 6


def test_depressed_run_stops_at_a_normal_observation() -> None:
    """A positive deviation ends the stretch."""
    scores = [_obs(0.4, z=-1.0), _obs(0.6, z=+0.5), _obs(0.4, z=-1.0)]
    assert trailing_depressed_run(scores) == 1


# --------------------------------------------------------------------------- #
# Field score
# --------------------------------------------------------------------------- #


def test_two_consecutive_lows_flag_the_field() -> None:
    """The flag rule is exactly two consecutive observations below p10."""
    ndvi = [_obs(0.2, below=True, z=-2.0), _obs(0.2, below=True, z=-2.0)]
    result = field_stress_score("f1", "Test", "citrus", ndvi, [])
    assert result.flagged is True
    assert result.n_consecutive_low == 2


def test_one_low_observation_is_not_a_flag() -> None:
    """A single dip is noise until it repeats."""
    ndvi = [_obs(0.5, z=+0.2), _obs(0.2, below=True, z=-2.0)]
    result = field_stress_score("f1", "Test", "citrus", ndvi, [])
    assert result.flagged is False
    assert "not yet a trend" in result.note


def test_mild_dip_scores_without_flagging() -> None:
    """A shallow dip ranks above a healthy field but does not raise a flag.

    Severity is not gated on p10, so the score stays informative for fields that
    never breach it; the sustained trigger needs a real shortfall, not any dip.
    """
    mild = [_obs(0.48, below=False, z=-0.3) for _ in range(6)]      # 4% shortfall
    normal = [_obs(0.5, below=False, z=+0.1) for _ in range(6)]
    low = field_stress_score("f1", "Low", "citrus", mild, [])
    fine = field_stress_score("f2", "Fine", "citrus", normal, [])
    assert low.flagged is False
    assert low.trigger == "none"
    assert low.score > fine.score
    assert fine.score == pytest.approx(0.0)


def test_ndmi_corroboration_marks_water_stress() -> None:
    """NDVI down with NDMI down reads as water; NDVI down alone does not."""
    ndvi = [_obs(0.2, below=True, z=-2.0)] * 2
    ndmi_low = [_obs(0.1, below=True, z=-2.0)] * 2
    ndmi_ok = [_obs(0.3, below=False, z=+0.1)] * 2

    assert field_stress_score("f1", "T", "citrus", ndvi, ndmi_low).water_stress is True
    unclear = field_stress_score("f1", "T", "citrus", ndvi, ndmi_ok)
    assert unclear.water_stress is False
    assert "unclear cause" in unclear.note


def test_thin_baseline_discounts_the_score() -> None:
    """The same evidence scores lower where the baseline behind it is weak."""
    strong = [_obs(0.2, below=True, z=-2.0, confidence="high")] * 3
    weak = [_obs(0.2, below=True, z=-2.0, confidence="low")] * 3
    high = field_stress_score("f1", "T", "citrus", strong, [])
    low = field_stress_score("f2", "T", "citrus", weak, [])
    assert low.score == pytest.approx(high.score * CONFIDENCE_MULTIPLIER["low"], abs=0.2)
    assert "baseline here is thin" in low.note


def test_field_with_no_observations_scores_zero() -> None:
    """A field with nothing this season is reported, not skipped or crashed."""
    result = field_stress_score("f1", "T", "citrus", [], [])
    assert result.score == 0.0
    assert result.latest_ndvi is None
    assert result.as_dict()["latest_ndvi"] is None
    assert "no observations" in result.note


def test_score_is_bounded() -> None:
    """Even an extreme deviation stays inside 0..100."""
    extreme = [_obs(0.0, below=True, z=-50.0)] * 10
    assert 0.0 <= field_stress_score("f1", "T", "citrus", extreme, extreme).score <= 100.0


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_flagged_fields_outrank_unflagged_ones() -> None:
    """Triage must never bury a real flag beneath a higher-scoring borderline field."""
    flagged = field_stress_score(
        "flagged", "A", "citrus", [_obs(0.2, below=True, z=-1.0)] * 2, []
    )
    unflagged = field_stress_score(
        "unflagged", "B", "citrus", [_obs(0.49, below=False, z=-0.2)] * 8, []
    )
    assert unflagged.flagged is False
    ranked = rank_fields([unflagged, flagged])
    assert ranked[0]["field_id"] == "flagged"


def test_ranking_puts_the_worst_first() -> None:
    """Among equally flagged fields, the higher score leads."""
    worse = field_stress_score("worse", "A", "c", [_obs(0.1, below=True, z=-3.0)] * 4, [])
    milder = field_stress_score("milder", "B", "c", [_obs(0.3, below=True, z=-1.0)] * 2, [])
    ranked = rank_fields([milder, worse])
    assert [r["field_id"] for r in ranked] == ["worse", "milder"]


def test_output_row_carries_every_requested_field() -> None:
    """The JSON row must contain the keys the triage output promises."""
    row = field_stress_score("f1", "N", "citrus", [_obs(0.2, below=True)] * 2, []).as_dict()
    for key in (
        "field_id", "name", "crop", "score", "n_consecutive_low", "latest_ndvi",
        "baseline_median", "percentile", "last_observation_date", "baseline_confidence",
        "flagged", "water_stress",
    ):
        assert key in row


def test_note_distinguishes_depressed_from_normal() -> None:
    """An unflagged field is not automatically a healthy one.

    rgv-001 sat below its own median for eleven straight observations without
    ever breaching p10; calling that "within its normal range" would mislead.
    """
    depressed = field_stress_score(
        "f1", "T", "citrus", [_obs(0.4, below=False, z=-1.2)] * 6, []
    )
    healthy = field_stress_score(
        "f2", "T", "citrus", [_obs(0.5, below=False, z=+0.2)] * 6, []
    )
    assert depressed.flagged is True
    assert depressed.trigger == "sustained-shortfall"
    assert "without a sharp drop" in depressed.note
    assert healthy.note.startswith("within its normal range")


# --------------------------------------------------------------------------- #
# Sustained shortfall trigger
# --------------------------------------------------------------------------- #


def test_sustained_shortfall_flags_without_a_p10_breach() -> None:
    """A field far below normal for weeks is flagged even if it never hits p10.

    This is the rgv-001 case: the 2023 drought year inside its own baseline
    widens p10 so far that a second bad year never crosses it.
    """
    scores = [_obs(0.40, below=False, z=-0.5, median=0.56)] * 8
    result = field_stress_score("f1", "T", "sugarcane", scores, [])
    assert result.n_consecutive_low == 0          # never breached p10
    assert result.flagged is True
    assert result.trigger == "sustained-shortfall"


def test_sustained_trigger_needs_both_length_and_depth() -> None:
    """Neither a short deep run nor a long shallow one trips the secondary rule."""
    short_deep = [_obs(0.30, below=False, z=-1.5, median=0.56)] * 3
    long_shallow = [_obs(0.545, below=False, z=-0.2, median=0.56)] * 12
    assert field_stress_score("a", "T", "c", short_deep, []).flagged is False
    assert field_stress_score("b", "T", "c", long_shallow, []).flagged is False


def test_p10_breach_still_reports_its_own_trigger() -> None:
    """The primary rule keeps precedence in the reported trigger."""
    scores = [_obs(0.2, below=True, z=-2.0, median=0.5)] * 8
    assert field_stress_score("f1", "T", "c", scores, []).trigger == "p10-run"


def test_a_sharp_short_breach_outscores_a_long_mild_one() -> None:
    """Persistence takes the longer of the two runs, so severity decides.

    Without this a field breaching p10 for two observations scored below a milder
    field purely because its run counter was smaller.
    """
    sharp = [_obs(0.10, below=False, z=-2.0, median=0.30)] * 6
    sharp = sharp[:-2] + [_obs(0.10, below=True, z=-2.0, median=0.30)] * 2
    mild = [_obs(0.48, below=False, z=-0.6, median=0.56)] * 11
    assert (
        field_stress_score("sharp", "T", "c", sharp, []).score
        > field_stress_score("mild", "T", "c", mild, []).score
    )


def test_shortfall_denominator_is_floored_for_indices_near_zero() -> None:
    """NDMI straddles zero, so a tiny median must not explode the ratio.

    Left unfloored this produced a 1557% shortfall and let NDMI dominate the
    composite score outright.
    """
    from dosojos_sat.baseline import SHORTFALL_FLOOR, relative_shortfall

    near_zero = [_obs(-0.055, median=0.001)] * 4
    shortfall = relative_shortfall(near_zero)
    assert shortfall == pytest.approx(0.056 / SHORTFALL_FLOOR, abs=1e-3)
    assert shortfall < 1.0


def test_shortfall_uses_the_median_when_it_is_comfortably_positive() -> None:
    """Above the floor, the gap is a plain fraction of the baseline median."""
    from dosojos_sat.baseline import relative_shortfall

    assert relative_shortfall([_obs(0.40, median=0.50)] * 3) == pytest.approx(0.2)

"""Multi-year day-of-year climatology: what normal looks like for one field.

Each field is judged against its own history rather than against its neighbours
or an absolute threshold, so a citrus block and a sugarcane field are never
compared to each other. Deviation scoring builds on this in step 6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

#: Day-of-year values run 1..366, so the cycle length is the leap-year maximum.
#: In a common year this makes the 31 December to 1 January step measure two days
#: instead of one, which is immaterial inside a window of a dozen days either side.
DAYS_IN_YEAR = 366

#: Column order of a climatology table.
BASELINE_COLUMNS = (
    "doy", "median", "p10", "p25", "p75", "p90",
    "n_obs", "n_years", "interpolated", "confidence",
)

_STATISTICS = ("median", "p10", "p25", "p75", "p90")


@dataclass(frozen=True)
class BaselineParams:
    """Settings that define one climatology."""

    season: int
    history_years: int = 4
    doy_window: int = 12
    smooth_window: int = 15

    @property
    def year_range(self) -> tuple[int, int]:
        """First and last full calendar year of history, inclusive.

        Season 2026 with four years of history reads 2022 to 2025; the season
        being judged is never part of the baseline it is judged against.
        """
        return self.season - self.history_years, self.season - 1


@dataclass(frozen=True)
class BaselineRun:
    """Provenance for one stored climatology."""

    index_name: str
    params: BaselineParams
    created_at: str

    @classmethod
    def now(cls, index_name: str, params: BaselineParams) -> "BaselineRun":
        """Stamp a run with the current UTC time."""
        return cls(
            index_name=index_name,
            params=params,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


# --------------------------------------------------------------------------- #
# Day-of-year arithmetic
# --------------------------------------------------------------------------- #


def doy_distance(a: np.ndarray | int, b: np.ndarray | int) -> np.ndarray:
    """Circular distance in days between day-of-year values.

    The wrap matters here: the Rio Grande Valley grows winter vegetables, so a
    baseline that treated 28 December and 3 January as 360 days apart would come
    apart exactly where the winter crop sits.
    """
    diff = np.abs(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))
    return np.minimum(diff, DAYS_IN_YEAR - diff)


def classify_confidence(n_obs: int, n_years: int) -> str:
    """Grade how well supported one day-of-year bin is.

    Both counts matter. Ten observations that all came from a single good year
    describe that year rather than a normal, so ``n_years`` gates the top grades.
    """
    if n_obs >= 6 and n_years >= 3:
        return "high"
    if n_obs >= 3 and n_years >= 2:
        return "medium"
    if n_obs >= 1:
        return "low"
    return "none"


# --------------------------------------------------------------------------- #
# Climatology
# --------------------------------------------------------------------------- #


def build_climatology(history: pd.DataFrame, params: BaselineParams) -> pd.DataFrame:
    """Pool history into one row per day of year, before smoothing.

    Every historical observation within ``doy_window`` days of a given day of
    year contributes to that bin. Empty bins are kept with NaN statistics and an
    ``n_obs`` of zero rather than dropped, so thin coverage stays visible instead
    of quietly disappearing from the output.

    Args:
        history: Observations carrying ``doy``, ``year`` and ``median`` columns.
        params: Window and history settings.
    """
    if history.empty:
        return _empty_climatology()

    values = history["median"].to_numpy(dtype=float)
    doys = history["doy"].to_numpy(dtype=int)
    years = history["year"].to_numpy(dtype=int)
    finite = np.isfinite(values)
    values, doys, years = values[finite], doys[finite], years[finite]

    rows = [
        _bin_for_doy(doy, values, doys, years, params.doy_window)
        for doy in range(1, DAYS_IN_YEAR + 1)
    ]
    return pd.DataFrame(rows)


def _bin_for_doy(
    doy: int,
    values: np.ndarray,
    doys: np.ndarray,
    years: np.ndarray,
    window: int,
) -> dict[str, object]:
    """Summarise every observation falling within ``window`` days of ``doy``."""
    selected = doy_distance(doys, doy) <= window
    sample = values[selected]
    if sample.size == 0:
        return {
            "doy": doy, "median": np.nan, "p10": np.nan, "p25": np.nan,
            "p75": np.nan, "p90": np.nan, "n_obs": 0, "n_years": 0,
        }

    p10, p25, median, p75, p90 = np.percentile(sample, [10, 25, 50, 75, 90])
    return {
        "doy": doy,
        "median": float(median),
        "p10": float(p10),
        "p25": float(p25),
        "p75": float(p75),
        "p90": float(p90),
        "n_obs": int(sample.size),
        "n_years": int(np.unique(years[selected]).size),
    }


def _empty_climatology() -> pd.DataFrame:
    """A full-year table with nothing behind any bin."""
    return pd.DataFrame(
        {
            "doy": list(range(1, DAYS_IN_YEAR + 1)),
            **{statistic: np.nan for statistic in _STATISTICS},
            "n_obs": 0,
            "n_years": 0,
        }
    )


# --------------------------------------------------------------------------- #
# Smoothing
# --------------------------------------------------------------------------- #


def smooth_circular(series: pd.Series, window: int) -> pd.Series:
    """Rolling mean across day of year that wraps at the year boundary.

    The series is padded with its own tail and head before rolling, so 1 January
    is smoothed against late December rather than against nothing. NaN bins are
    skipped by the mean, which is what fills short gaps from their neighbours.
    """
    if window <= 1 or series.empty:
        return series.reset_index(drop=True)

    pad = min(window, len(series))
    extended = pd.concat(
        [series.iloc[-pad:], series, series.iloc[:pad]], ignore_index=True
    )
    smoothed = extended.rolling(window=window, center=True, min_periods=1).mean()
    return smoothed.iloc[pad:pad + len(series)].reset_index(drop=True)


def smooth_baseline(raw: pd.DataFrame, params: BaselineParams) -> pd.DataFrame:
    """Smooth every statistic across day of year and grade each bin.

    ``interpolated`` marks bins that had no observations of their own and took a
    value from neighbouring days, so a chart can draw them differently and a
    score can discount them. Counts are never smoothed: they report what was
    actually measured.
    """
    table = raw.copy().reset_index(drop=True)
    was_empty = table["median"].isna()

    for statistic in _STATISTICS:
        table[statistic] = smooth_circular(table[statistic], params.smooth_window)

    # A bin is interpolated only if it was empty and the smoother reached it;
    # one the smoother could not reach is still empty and stays honest.
    table["interpolated"] = (was_empty & table["median"].notna()).astype(int)
    table["confidence"] = [
        classify_confidence(int(n_obs), int(n_years))
        for n_obs, n_years in zip(table["n_obs"], table["n_years"])
    ]
    return table[list(BASELINE_COLUMNS)]


def build_baseline(history: pd.DataFrame, params: BaselineParams) -> pd.DataFrame:
    """Build the smoothed day-of-year climatology from one field's history."""
    return smooth_baseline(build_climatology(history, params), params)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def confidence_summary(baseline: pd.DataFrame) -> dict[str, int]:
    """Count day-of-year bins at each confidence grade."""
    counts = baseline["confidence"].value_counts().to_dict()
    return {
        grade: int(counts.get(grade, 0))
        for grade in ("high", "medium", "low", "none")
    }


def thin_stretches(
    baseline: pd.DataFrame, min_length: int = 5
) -> list[tuple[int, int, str]]:
    """Runs of day-of-year bins graded below medium, as ``(start, end, grade)``.

    Reports where a baseline is too thin to lean on, which is the context a
    deviation score falling inside one of these stretches has to be read against.
    """
    weak = baseline["confidence"].isin(("low", "none")).to_numpy()
    stretches: list[tuple[int, int, str]] = []
    start: int | None = None

    for position, is_weak in enumerate(weak):
        if is_weak and start is None:
            start = position
        elif not is_weak and start is not None:
            _append_stretch(stretches, baseline, start, position - 1, min_length)
            start = None
    if start is not None:
        _append_stretch(stretches, baseline, start, len(weak) - 1, min_length)
    return stretches


def _append_stretch(
    stretches: list[tuple[int, int, str]],
    baseline: pd.DataFrame,
    start: int,
    end: int,
    min_length: int,
) -> None:
    """Record one weak stretch if it is long enough to matter."""
    if end - start + 1 < min_length:
        return
    grades = baseline["confidence"].iloc[start:end + 1]
    worst = "none" if (grades == "none").any() else "low"
    stretches.append(
        (int(baseline["doy"].iloc[start]), int(baseline["doy"].iloc[end]), worst)
    )


# --------------------------------------------------------------------------- #
# Deviation scoring
# --------------------------------------------------------------------------- #

#: How much each index contributes to the composite stress score.
NDVI_WEIGHT = 0.75
NDMI_WEIGHT = 0.25

#: Consecutive sub-p10 observations needed before a field is flagged.
FLAG_RUN_LENGTH = 2

#: A shortfall this large counts as maximum severity when scoring.
SEVERITY_CLIP = 0.40
PERSISTENCE_CLIP = 4

#: Secondary trigger. A field can sit far below normal for weeks without ever
#: breaching p10, because p10 is widened by whatever bad years the baseline
#: itself contains. Relative shortfall against the median is immune to that.
SUSTAINED_RUN_LENGTH = 6
SUSTAINED_SHORTFALL = 0.10

#: Smallest denominator used when expressing a gap as a fraction of the baseline.
#: NDVI sits comfortably above zero, but NDMI and NDWI oscillate around it, and
#: dividing by a near-zero median turns an ordinary gap into a 1500% shortfall.
SHORTFALL_FLOOR = 0.20

#: A thin baseline should not produce a confident accusation.
CONFIDENCE_MULTIPLIER = {"high": 1.0, "medium": 0.85, "low": 0.6, "none": 0.0}

#: Converts an interquartile range into a standard-deviation equivalent.
IQR_TO_SIGMA = 1.349


@dataclass(frozen=True)
class ObservationScore:
    """Where one current-season observation falls in its day-of-year baseline."""

    obs_date: object
    doy: int
    value: float
    baseline_median: float
    baseline_p10: float
    percentile: float
    robust_z: float
    below_p10: bool
    confidence: str


@dataclass(frozen=True)
class FieldScore:
    """One field's season summarised into a ranked triage row."""

    field_id: str
    name: str
    crop: str
    score: float
    flagged: bool
    water_stress: bool
    n_consecutive_low: int
    latest_ndvi: float | None
    baseline_median: float | None
    percentile: float | None
    robust_z: float | None
    last_observation_date: str | None
    baseline_confidence: str
    n_observations: int
    trigger: str
    shortfall: float
    note: str

    def as_dict(self) -> dict[str, object]:
        """Render as the JSON row the triage output expects."""
        return {
            "field_id": self.field_id,
            "name": self.name,
            "crop": self.crop,
            "score": self.score,
            "flagged": self.flagged,
            "water_stress": self.water_stress,
            "n_consecutive_low": self.n_consecutive_low,
            "latest_ndvi": _round(self.latest_ndvi, 4),
            "baseline_median": _round(self.baseline_median, 4),
            "percentile": _round(self.percentile, 1),
            "robust_z": _round(self.robust_z, 2),
            "last_observation_date": self.last_observation_date,
            "baseline_confidence": self.baseline_confidence,
            "n_observations": self.n_observations,
            "trigger": self.trigger,
            "shortfall_pct": _round(100.0 * self.shortfall, 1),
            "note": self.note,
        }


def _round(value: float | None, digits: int) -> float | None:
    """Round for output, leaving a missing value as null."""
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def percentile_of(value: float, sample: np.ndarray) -> float:
    """Percentile of ``value`` within ``sample``, by direct comparison.

    Measured against the pooled historical observations themselves rather than
    interpolated between five stored summary statistics, which is both more
    faithful and free, since the raw history is already in the cache.
    """
    sample = np.asarray(sample, dtype=float)
    sample = sample[np.isfinite(sample)]
    if sample.size == 0 or not np.isfinite(value):
        return float("nan")
    below = np.count_nonzero(sample < value)
    ties = np.count_nonzero(sample == value)
    return 100.0 * (below + 0.5 * ties) / sample.size


def robust_z(value: float, median: float, p25: float, p75: float) -> float:
    """Deviation from the baseline median in interquartile units.

    Uses the IQR rather than a standard deviation because a handful of cloudy or
    freshly harvested observations would drag an ordinary sigma around, and this
    has to stay stable on a few dozen samples per bin.
    """
    if not all(np.isfinite([value, median, p25, p75])):
        return float("nan")
    spread = (p75 - p25) / IQR_TO_SIGMA
    if spread <= 0:
        return 0.0
    return float((value - median) / spread)


def trailing_low_run(below_p10: Sequence[bool]) -> int:
    """Count consecutive sub-p10 observations ending at the most recent one.

    Triage asks whether a field is in trouble now, so a run that ended in June
    and recovered by September should not keep the field flagged.
    """
    run = 0
    for is_low in reversed(list(below_p10)):
        if not is_low:
            break
        run += 1
    return run


def score_observations(
    season: pd.DataFrame, baseline: pd.DataFrame, history: pd.DataFrame
) -> list[ObservationScore]:
    """Place every current-season observation inside its day-of-year baseline."""
    if season.empty or baseline.empty:
        return []

    bins = baseline.set_index("doy")
    scores: list[ObservationScore] = []
    for row in season.itertuples():
        doy = int(row.doy)
        if doy not in bins.index:
            continue
        b = bins.loc[doy]
        value = float(row.median)
        sample = _window_sample(history, doy)
        scores.append(
            ObservationScore(
                obs_date=row.date,
                doy=doy,
                value=value,
                baseline_median=float(b["median"]) if pd.notna(b["median"]) else float("nan"),
                baseline_p10=float(b["p10"]) if pd.notna(b["p10"]) else float("nan"),
                percentile=percentile_of(value, sample),
                robust_z=robust_z(value, b["median"], b["p25"], b["p75"]),
                below_p10=bool(pd.notna(b["p10"]) and value < float(b["p10"])),
                confidence=str(b["confidence"]),
            )
        )
    return scores


def _window_sample(history: pd.DataFrame, doy: int, window: int = 12) -> np.ndarray:
    """Historical values pooled around one day of year, for the percentile."""
    if history.empty:
        return np.empty(0)
    near = doy_distance(history["doy"].to_numpy(dtype=int), doy) <= window
    return history["median"].to_numpy(dtype=float)[near]


def trailing_depressed_run(scores: Sequence[ObservationScore]) -> int:
    """Count consecutive observations below the baseline median, most recent first.

    Wider than the p10 flag rule on purpose. A field can sit a full standard
    deviation under its normal for a month without ever breaching p10, because
    p10 is dragged down by whatever bad years are inside the baseline itself.
    """
    run = 0
    for score in reversed(list(scores)):
        if not (np.isfinite(score.robust_z) and score.robust_z < 0):
            break
        run += 1
    return run


def relative_shortfall(scores: Sequence[ObservationScore]) -> float:
    """Median gap below the baseline median across a run, as a fraction of it.

    Preferred over the robust z for scoring because the interquartile spread is
    inflated by any bad year inside the baseline, which shrinks z exactly when a
    field is having its second bad year running.

    The denominator is floored at :data:`SHORTFALL_FLOOR`, since NDMI and NDWI
    straddle zero and a near-zero median would otherwise turn a routine gap into
    an enormous ratio.
    """
    gaps = [
        (s.baseline_median - s.value) / max(abs(s.baseline_median), SHORTFALL_FLOOR)
        for s in scores
        if np.isfinite(s.baseline_median) and np.isfinite(s.value)
    ]
    return float(np.median(gaps)) if gaps else 0.0


def _index_term(scores: Sequence[ObservationScore]) -> tuple[float, int, bool]:
    """Score contribution for one index, plus its p10 run and sustained trigger.

    Severity comes from how far below normal the current stretch sits and is not
    gated on p10, so a field depressed for weeks still outranks a healthy one.
    Persistence counts the p10 run, or the below-median run when the sustained
    trigger fires, so both routes to a flag carry weight.
    """
    low_run = trailing_low_run([s.below_p10 for s in scores])
    depressed = trailing_depressed_run(scores)
    if depressed == 0:
        return 0.0, low_run, False

    recent = scores[-depressed:]
    shortfall = relative_shortfall(recent)
    sustained = depressed >= SUSTAINED_RUN_LENGTH and shortfall >= SUSTAINED_SHORTFALL

    severity = min(max(shortfall, 0.0), SEVERITY_CLIP) / SEVERITY_CLIP
    # Take whichever route gives the longer run: a short sharp p10 breach must
    # not score below a milder field simply because its run counter is smaller.
    effective = max(low_run, depressed if sustained else 0)
    persistence = min(effective, PERSISTENCE_CLIP) / PERSISTENCE_CLIP
    return 0.5 * severity + 0.5 * persistence, low_run, sustained


def field_stress_score(
    field_id: str,
    name: str,
    crop: str,
    ndvi: Sequence[ObservationScore],
    ndmi: Sequence[ObservationScore],
) -> FieldScore:
    """Combine one field's season into a single 0-100 triage score.

    NDVI carries the verdict and NDMI corroborates it. A field is flagged when
    NDVI has sat below its own baseline p10 for two or more consecutive valid
    observations; the score then grades how far below and for how long, and is
    scaled down where the baseline behind it is thin.

    ``water_stress`` is the useful distinction for a grower: NDVI down with NDMI
    also down reads as water, while NDVI down on its own points at disease,
    nutrient deficiency, or a harvest.
    """
    if not ndvi:
        return _empty_score(field_id, name, crop, "no observations this season")

    ndvi_term, run, sustained = _index_term(ndvi)
    ndmi_term, ndmi_run, ndmi_sustained = _index_term(ndmi)
    latest = ndvi[-1]

    confidence = latest.confidence
    multiplier = CONFIDENCE_MULTIPLIER.get(confidence, 0.0)
    raw = NDVI_WEIGHT * ndvi_term + NDMI_WEIGHT * ndmi_term

    breached_p10 = run >= FLAG_RUN_LENGTH
    flagged = breached_p10 or sustained
    trigger = "p10-run" if breached_p10 else ("sustained-shortfall" if sustained else "none")
    water_stress = flagged and (ndmi_run > 0 or ndmi_sustained)

    return FieldScore(
        field_id=field_id,
        name=name,
        crop=crop,
        score=round(100.0 * raw * multiplier, 1),
        flagged=flagged,
        water_stress=water_stress,
        n_consecutive_low=run,
        latest_ndvi=latest.value,
        baseline_median=latest.baseline_median,
        percentile=latest.percentile,
        robust_z=latest.robust_z,
        last_observation_date=str(latest.obs_date),
        baseline_confidence=confidence,
        n_observations=len(ndvi),
        trigger=trigger,
        shortfall=relative_shortfall(ndvi[-trailing_depressed_run(ndvi):]) if ndvi else 0.0,
        note=_describe(flagged, water_stress, run, confidence, trigger,
                       trailing_depressed_run(ndvi), latest.percentile),
    )


def _empty_score(field_id: str, name: str, crop: str, note: str) -> FieldScore:
    """A placeholder row for a field with nothing to judge."""
    return FieldScore(
        field_id=field_id, name=name, crop=crop, score=0.0, flagged=False,
        water_stress=False, n_consecutive_low=0, latest_ndvi=None,
        baseline_median=None, percentile=None, robust_z=None,
        last_observation_date=None, baseline_confidence="none",
        n_observations=0, trigger="none", shortfall=0.0, note=note,
    )


def _describe(
    flagged: bool,
    water_stress: bool,
    run: int,
    confidence: str,
    trigger: str,
    depressed: int,
    percentile: float,
) -> str:
    """One plain sentence a grower can act on, naming which rule fired."""
    caveat = "" if confidence in ("high", "medium") else "; baseline here is thin"
    what = "water stress" if water_stress else "stress of unclear cause"
    if trigger == "p10-run":
        return f"{run} consecutive observations below the 10th percentile, consistent with {what}{caveat}"
    if trigger == "sustained-shortfall":
        return (
            f"below its own normal for {depressed} consecutive observations without "
            f"a sharp drop, consistent with {what}{caveat}"
        )
    if run == 1:
        return f"one observation below the 10th percentile, not yet a trend{caveat}"
    if depressed >= FLAG_RUN_LENGTH:
        place = "" if not np.isfinite(percentile) else f", latest at the {percentile:.0f}th percentile"
        return f"mildly below its median for {depressed} observations{place}, within tolerance{caveat}"
    return f"within its normal range{caveat}"


def rank_fields(scores: Sequence[FieldScore]) -> list[dict[str, object]]:
    """Order fields worst first, ready to serialise.

    Flagged fields always outrank unflagged ones so triage never buries a real
    problem beneath a higher-scoring field that is merely borderline.
    """
    ordered = sorted(
        scores,
        key=lambda s: (s.flagged, s.score, s.n_consecutive_low),
        reverse=True,
    )
    return [s.as_dict() for s in ordered]

"""Per-field baseline charts, sized to be read at a glance from a slide.

Large type, one idea per panel, no chartjunk. The shaded band is the field's own
history; the line over it is this season.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display on a build machine or in CI

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

#: Deliberately large. These are read from across a room, not zoomed into.
FONT_SIZES = {
    "title": 19,
    "subtitle": 13,
    "axis": 14,
    "tick": 12,
    "legend": 12,
    "annotation": 12,
}

BAND_COLOR = "#c3c2b7"
MEDIAN_COLOR = "#6f6e69"
SEASON_COLOR = "#2a78d6"
FLAG_COLOR = "#d03b3b"
THIN_COLOR = "#eda100"

FIG_SIZE = (11.0, 5.6)
DPI = 150


def chart_path(out_dir: Path, field_id: str, index_name: str) -> Path:
    """Where one field's chart for an index is written."""
    return Path(out_dir) / f"{field_id}_{index_name}.png"


def _doy_to_date(doy: int, year: int) -> date:
    """Map a day of year onto a calendar date in the season being plotted."""
    return date(year, 1, 1) + timedelta(days=int(doy) - 1)


def _baseline_series(
    baseline: pd.DataFrame, year: int
) -> tuple[list[date], np.ndarray, np.ndarray, np.ndarray]:
    """Baseline dates and p10/median/p90 arrays, trimmed to the plotted year."""
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    table = baseline[baseline["doy"] <= days_in_year]
    dates = [_doy_to_date(d, year) for d in table["doy"]]
    return (
        dates,
        table["p10"].to_numpy(dtype=float),
        table["median"].to_numpy(dtype=float),
        table["p90"].to_numpy(dtype=float),
    )


def plot_field(
    *,
    field_id: str,
    name: str,
    crop: str,
    index_name: str,
    baseline: pd.DataFrame,
    season: pd.DataFrame,
    year: int,
    out_dir: Path,
    history_years: tuple[int, int] | None = None,
    flagged_dates: set[date] | None = None,
    verdict: str | None = None,
    run_start: date | None = None,
) -> Path:
    """Draw one field's season against its own baseline and save a PNG.

    Args:
        baseline: Climatology with ``doy``, ``p10``, ``median``, ``p90`` and
            ``confidence`` columns.
        season: Current-season observations with ``date`` and ``median``.
        flagged_dates: Observations to mark in red as below the baseline p10.
        verdict: One-line judgement printed under the title.
        run_start: First date of the trailing below-normal run, shaded so a field
            flagged for a sustained shortfall shows why even with no red markers.

    Returns:
        Path to the written PNG.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    flagged_dates = flagged_dates or set()

    figure, axes = plt.subplots(figsize=FIG_SIZE, dpi=DPI)
    dates, p10, median, p90 = _baseline_series(baseline, year)

    axes.fill_between(
        dates, p10, p90, color=BAND_COLOR, alpha=0.55, linewidth=0,
        label="normal range (p10-p90)", zorder=1,
    )
    axes.plot(
        dates, median, color=MEDIAN_COLOR, linewidth=2.0, linestyle="--",
        label="normal (median)", zorder=2,
    )
    _shade_thin_baseline(axes, baseline, year)

    if not season.empty:
        season_dates = list(season["date"])
        values = season["median"].to_numpy(dtype=float)
        axes.plot(
            season_dates, values, color=SEASON_COLOR, linewidth=2.4,
            label=f"{year} season", zorder=3,
        )
        axes.scatter(
            season_dates, values, s=46, color=SEASON_COLOR, edgecolor="white",
            linewidth=1.2, zorder=4,
        )
        low = [(d, v) for d, v in zip(season_dates, values) if d in flagged_dates]
        if low:
            axes.scatter(
                [d for d, _ in low], [v for _, v in low], s=120, color=FLAG_COLOR,
                edgecolor="white", linewidth=1.4, zorder=5,
                label="below normal range",
            )

    _shade_run(axes, run_start, season, year)
    _style_axes(axes, index_name, year)
    _add_titles(figure, axes, name, crop, index_name, history_years, verdict)

    path = chart_path(out_dir, field_id, index_name)
    figure.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    log.info("wrote %s", path)
    return path


def _shade_thin_baseline(axes: plt.Axes, baseline: pd.DataFrame, year: int) -> None:
    """Mark stretches where the baseline rests on too little history.

    Drawn so nobody reads confidence into a part of the year the history barely
    covers; silence there would be the dishonest option.
    """
    weak = baseline["confidence"].isin(("low", "none")).to_numpy()
    if not weak.any():
        return
    doys = baseline["doy"].to_numpy(dtype=int)
    axes.fill_between(
        [_doy_to_date(d, year) for d in doys],
        0, 1, where=weak, transform=axes.get_xaxis_transform(),
        color=THIN_COLOR, alpha=0.13, linewidth=0, zorder=0,
        label="baseline thin here",
    )


def _shade_run(
    axes: plt.Axes, run_start: date | None, season: pd.DataFrame, year: int
) -> None:
    """Shade the trailing stretch that sits below normal.

    A field flagged for a sustained shortfall never breaches p10, so without this
    its chart would carry no visible reason for the flag.
    """
    if run_start is None or season.empty:
        return
    last = max(season["date"])
    axes.axvspan(
        run_start, last, color=FLAG_COLOR, alpha=0.07, linewidth=0, zorder=0,
        label="below normal run",
    )


def _style_axes(axes: plt.Axes, index_name: str, year: int) -> None:
    """Apply the shared axis styling: large type, no chartjunk."""
    axes.set_ylabel(index_name, fontsize=FONT_SIZES["axis"])
    axes.tick_params(axis="both", labelsize=FONT_SIZES["tick"])
    axes.xaxis.set_major_locator(mdates.MonthLocator())
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    axes.set_xlim(date(year, 1, 1), date(year, 12, 31))
    axes.grid(axis="y", color="#e1e0d9", linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color("#c3c2b7")
    axes.legend(
        loc="upper left", fontsize=FONT_SIZES["legend"], frameon=False, ncol=2,
    )


def _add_titles(
    figure: plt.Figure,
    axes: plt.Axes,
    name: str,
    crop: str,
    index_name: str,
    history_years: tuple[int, int] | None,
    verdict: str | None,
) -> None:
    """Title with the field and crop, then the baseline span and the verdict."""
    pad = 46 if verdict else 26
    axes.set_title(
        f"{name} - {crop}",
        fontsize=FONT_SIZES["title"], fontweight="medium", loc="left", pad=pad,
    )
    span = f"{history_years[0]}-{history_years[1]}" if history_years else "prior years"
    axes.annotate(
        f"{index_name} against this field's own {span} normal",
        xy=(0, 1), xycoords="axes fraction", xytext=(0, 26 if verdict else 8),
        textcoords="offset points",
        fontsize=FONT_SIZES["subtitle"], color="#6f6e69",
    )
    if verdict:
        flagged = verdict.lower().startswith("flagged")
        axes.annotate(
            verdict,
            xy=(0, 1), xycoords="axes fraction", xytext=(0, 7), textcoords="offset points",
            fontsize=FONT_SIZES["annotation"],
            color=FLAG_COLOR if flagged else "#6f6e69",
            fontweight="medium" if flagged else "normal",
        )

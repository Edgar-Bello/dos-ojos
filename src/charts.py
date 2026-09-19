"""Per-field baseline charts, sized to be read at a glance from a slide.

Large type, one idea per panel, no chartjunk. The shaded band is the field's own
history; the line over it is this season.
"""

from __future__ import annotations

import logging
import math
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
#: Header band for data that is not our own; the drone figures use the same one.
BANNER_COLOR = "#1f5fa6"

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
    cutoff: date | None = None,
    banner: str | None = None,
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
        cutoff: The date the verdict was judged on. Later observations are drawn
            faded, since the verdict could not have known them.
        banner: A band above the title, for data that is not our own.

    Returns:
        Path to the written PNG.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    flagged_dates = flagged_dates or set()
    later = season.iloc[0:0]
    if cutoff is not None and not season.empty:
        later = season[season["date"] > cutoff]
        season = season[season["date"] <= cutoff]

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
    if not later.empty:
        axes.plot(
            list(later["date"]), later["median"].to_numpy(dtype=float),
            color=SEASON_COLOR, linewidth=1.4, alpha=0.3, marker="o", markersize=4,
            label="after the judged date", zorder=2,
        )
    if cutoff is not None:
        axes.axvline(cutoff, color=MEDIAN_COLOR, linewidth=1.4, linestyle=":", zorder=2)
        axes.annotate(
            f"judged as of {cutoff.day} {cutoff:%b}", xy=(cutoff, 1), xycoords=("data", "axes fraction"),
            xytext=(4, -4), textcoords="offset points", fontsize=FONT_SIZES["legend"] - 1,
            color=MEDIAN_COLOR, va="top",
        )

    _shade_run(axes, run_start, season, year)
    _style_axes(axes, index_name, year)
    _add_titles(figure, axes, name, crop, index_name, history_years, verdict)
    if banner:
        # Above the title, which _add_titles pads 46 points over the axes.
        axes.annotate(
            banner, xy=(0, 1), xycoords="axes fraction", xytext=(0, 84),
            textcoords="offset points", fontsize=FONT_SIZES["legend"], color="white",
            fontweight="bold",
            bbox={"boxstyle": "square,pad=0.45", "facecolor": BANNER_COLOR, "edgecolor": "none"},
        )

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


# --------------------------------------------------------------------------- #
# Water checkbook
# --------------------------------------------------------------------------- #

WATER_COLOR = SEASON_COLOR
RAIN_COLOR = "#7fb8e6"
IRRIGATION_COLOR = "#0e7c86"
WEEK_COLOR = "#b07a00"
MM_PER_INCH = 25.4


def water_chart_path(out_dir: Path, field_id: str) -> Path:
    """Where one field's water checkbook chart is written."""
    return Path(out_dir) / f"{field_id}_water.png"


def water_verdict(status: dict) -> tuple[str, str]:
    """The line under the title, and its colour, from a checkbook status."""
    word, days = status["status"], status["days_left"]
    if word == "harvested":
        return "Harvested: no water needed until the next crop", MEDIAN_COLOR
    if word == "mature":
        return "Mature (black layer): the grain is made, no more water needed", MEDIAN_COLOR
    if days == 0:
        return "WATER NOW: the crop is already short of water", FLAG_COLOR
    if days is None:
        return "OK for now: more than six weeks of water at the current rate", MEDIAN_COLOR
    when = date.fromisoformat(status["water_by"])
    low, high = status["days_range"] or (days, days)
    text = f"Water in about {days} days ({low}-{high}), by {when.day} {when:%b}, if it doesn't rain"
    color = FLAG_COLOR if days <= 3 else (WEEK_COLOR if days <= 7 else MEDIAN_COLOR)
    return text, color


def plot_water(
    *,
    status: dict,
    daily: pd.DataFrame,
    projection: pd.DataFrame,
    out_dir: Path,
    banner: str | None = None,
) -> Path:
    """Draw one field's water checkbook: water left, the stress line, rain and irrigation.

    The top panel is the root zone as a tank, in inches: its size grows with the
    roots, the dashed red line is where the crop starts to suffer, and the dotted
    line runs on from the judged date as if no rain came. The strip below shows
    the deposits.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    figure, (axes, strip) = plt.subplots(
        2, 1, figsize=(FIG_SIZE[0], FIG_SIZE[1] + 1.4), dpi=DPI, sharex=True,
        gridspec_kw={"height_ratios": [4.2, 1.0], "hspace": 0.08},
    )
    dates = list(daily["date"])
    capacity = daily["taw_mm"].to_numpy(dtype=float) / MM_PER_INCH
    stress = (daily["taw_mm"] - daily["raw_mm"]).to_numpy(dtype=float) / MM_PER_INCH
    left = (daily["taw_mm"] - daily["dr_mm"]).to_numpy(dtype=float) / MM_PER_INCH

    axes.fill_between(dates, 0, stress, color=FLAG_COLOR, alpha=0.06, linewidth=0, zorder=0)
    axes.fill_between(dates, 0, left, color=WATER_COLOR, alpha=0.16, linewidth=0, zorder=1)
    axes.plot(dates, capacity, color=MEDIAN_COLOR, linewidth=1.6, linestyle="--",
              label="soil full", zorder=2)
    axes.plot(dates, stress, color=FLAG_COLOR, linewidth=1.8, linestyle="--",
              label="crop starts to suffer below this", zorder=2)
    axes.plot(dates, left, color=WATER_COLOR, linewidth=2.6, label="water in the root zone",
              zorder=3)

    as_of = date.fromisoformat(status["as_of"])
    ends = [dates[-1]]
    if not projection.empty:
        taw = float(daily["taw_mm"].iloc[-1])
        path_dates = list(projection["date"])
        path = (taw - projection["dr_mm"].to_numpy(dtype=float)) / MM_PER_INCH
        axes.plot(path_dates, path, color=WATER_COLOR, linewidth=2.2, linestyle=":",
                  label="if it doesn't rain", zorder=3)
        ends.append(path_dates[-1])
        if status["days_left"]:
            axes.scatter([path_dates[-1]], [path[-1]], s=110, color=FLAG_COLOR,
                         edgecolor="white", linewidth=1.4, zorder=5)
            when = path_dates[-1]
            axes.annotate(f"water by {when.day} {when:%b}", xy=(when, path[-1]),
                          xytext=(8, 10), textcoords="offset points",
                          fontsize=FONT_SIZES["annotation"], color=FLAG_COLOR,
                          fontweight="medium")
    # A harvested field's balance stops at the harvest, which is the date worth
    # marking; the judged date may lie weeks past the end of the plot.
    mark = dates[-1] if status["status"] == "harvested" else as_of
    label = "harvested" if status["status"] == "harvested" else "as of"
    axes.axvline(mark, color=MEDIAN_COLOR, linewidth=1.2, linestyle=":", zorder=2)
    axes.annotate(f"{label} {mark.day} {mark:%b}", xy=(mark, 1), xycoords=("data", "axes fraction"),
                  xytext=(-4, -4), textcoords="offset points", ha="right", va="top",
                  fontsize=FONT_SIZES["legend"] - 1, color=MEDIAN_COLOR)

    rain = daily["rain_effective_mm"].to_numpy(dtype=float) / MM_PER_INCH
    irrigation = daily["irrigation_mm"].to_numpy(dtype=float) / MM_PER_INCH
    strip.bar(dates, rain, width=1.0, color=RAIN_COLOR, label="rain")
    strip.bar(dates, irrigation, width=1.6, color=IRRIGATION_COLOR, label="irrigation (stored)")
    strip.set_ylabel("in", fontsize=FONT_SIZES["tick"])
    strip.legend(loc="upper left", fontsize=FONT_SIZES["legend"] - 1, frameon=False, ncol=2)

    for panel in (axes, strip):
        panel.tick_params(axis="both", labelsize=FONT_SIZES["tick"])
        panel.grid(axis="y", color="#e1e0d9", linewidth=0.8)
        panel.set_axisbelow(True)
        for side in ("top", "right"):
            panel.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            panel.spines[side].set_color("#c3c2b7")
    locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
    strip.xaxis.set_major_locator(locator)
    strip.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    strip.set_xlim(dates[0], max(ends) + timedelta(days=2))
    # Headroom above the full line, so the two-row legend never sits on the data.
    axes.set_ylim(0, max(float(np.nanmax(capacity)) * 1.38, 0.5))
    axes.set_ylabel("inches of water", fontsize=FONT_SIZES["axis"])
    axes.legend(loc="upper left", fontsize=FONT_SIZES["legend"], frameon=False, ncol=2)

    soil = status.get("soil") or {}
    axes.set_title(f"{status['name']} - {status['crop']}", fontsize=FONT_SIZES["title"],
                   fontweight="medium", loc="left", pad=46)
    axes.annotate(
        f"Water checkbook  -  soil {soil.get('name', 'unknown')} "
        f"({soil.get('awc_in_per_ft', 0):.1f} in/ft)  -  confidence {status['confidence']}",
        xy=(0, 1), xycoords="axes fraction", xytext=(0, 26), textcoords="offset points",
        fontsize=FONT_SIZES["subtitle"], color="#6f6e69",
    )
    verdict, color = water_verdict(status)
    axes.annotate(verdict, xy=(0, 1), xycoords="axes fraction", xytext=(0, 7),
                  textcoords="offset points", fontsize=FONT_SIZES["annotation"],
                  color=color, fontweight="bold")
    if banner:
        axes.annotate(
            banner, xy=(0, 1), xycoords="axes fraction", xytext=(0, 84),
            textcoords="offset points", fontsize=FONT_SIZES["legend"], color="white",
            fontweight="bold",
            bbox={"boxstyle": "square,pad=0.45", "facecolor": BANNER_COLOR, "edgecolor": "none"},
        )

    path = water_chart_path(out_dir, status["field_id"])
    figure.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    log.info("wrote %s", path)
    return path


# --------------------------------------------------------------------------- #
# Sorghum growth stages
# --------------------------------------------------------------------------- #

STAGE_LINE_COLOR = "#2e6b45"
CRITICAL_COLOR = "#b8472a"
#: The stages worth a line on the chart; the three leaf counts would crowd it.
CHART_STAGES = ("emergence", "five_leaf", "panicle_initiation", "boot", "flowering",
                "soft_dough", "hard_dough", "black_layer")


def plot_sorghum_stages(
    *,
    stage: dict,
    heat: pd.DataFrame,
    out_path: Path,
    names: dict[str, str],
    title: str,
    subtitle: str,
    today_label: str,
    critical_label: str,
    projected_label: str,
    axis_label: str,
    banner: str | None = None,
    month_names: list[str] | None = None,
) -> Path:
    """Heat units piling up since planting, with the stage each total reaches.

    ``stage`` is :meth:`dosojos_sat.stages.StageEstimate.to_dict`; ``heat`` the daily
    ``date`` and ``cumulative`` from :func:`dosojos_sat.stages.gdu_series`. Every
    word on the chart comes in through the arguments, so the farmer reads it in
    their own language.
    """
    milestones = {m["stage"]: m for m in stage["milestones"]}
    planted = date.fromisoformat(stage["planted"])
    as_of = date.fromisoformat(stage["as_of"])
    last = milestones.get("black_layer", {}).get("date")
    end = max(date.fromisoformat(last) if last else as_of + timedelta(days=30),
              as_of + timedelta(days=7))

    figure, axes = plt.subplots(figsize=FIG_SIZE, dpi=DPI)
    critical_low = milestones["panicle_initiation"]["gdu"]
    critical_high = milestones["flowering"]["gdu"]
    axes.axhspan(critical_low, critical_high, color=CRITICAL_COLOR, alpha=0.10, lw=0)
    axes.annotate(critical_label, xy=(planted, (critical_low + critical_high) / 2),
                  xytext=(6, 0), textcoords="offset points", va="center",
                  fontsize=FONT_SIZES["annotation"], color=CRITICAL_COLOR, fontweight="bold")

    for key in CHART_STAGES:
        if key not in milestones:
            continue
        level = milestones[key]["gdu"]
        axes.axhline(level, color="#d5dcd6", lw=1, zorder=1)
        axes.annotate(names.get(key, key), xy=(1, level), xycoords=("axes fraction", "data"),
                      xytext=(6, 0), textcoords="offset points", va="center",
                      fontsize=FONT_SIZES["tick"], color="#3a4540")

    if heat is not None and not heat.empty:
        axes.plot([planted] + list(heat["date"]), [0.0] + list(heat["cumulative"]),
                  color=STAGE_LINE_COLOR, lw=2.8, zorder=3)
    rate = stage.get("gdu_per_day")
    if rate:
        days = (end - as_of).days
        future = [as_of + timedelta(days=i) for i in range(days + 1)]
        axes.plot(future, [stage["gdu"] + rate * i for i in range(days + 1)],
                  color=STAGE_LINE_COLOR, lw=2, ls=(0, (4, 3)), zorder=3,
                  label=projected_label)
        axes.legend(loc="upper left", frameon=False, fontsize=FONT_SIZES["legend"])

    axes.axvline(as_of, color="#6f6e69", lw=1.2, ls=":", zorder=2)
    axes.plot([as_of], [stage["gdu"]], "o", color=STAGE_LINE_COLOR, ms=9, zorder=4)
    axes.annotate(today_label, xy=(as_of, stage["gdu"]), xytext=(-10, 12),
                  textcoords="offset points", ha="right", fontsize=FONT_SIZES["annotation"],
                  color="#1e2b24", fontweight="bold")

    top = max(milestones.get("black_layer", {}).get("gdu", 0), stage["gdu"]) * 1.06
    axes.set_ylim(0, top)
    axes.set_xlim(planted, end)
    axes.set_ylabel(axis_label, fontsize=FONT_SIZES["axis"])
    axes.tick_params(labelsize=FONT_SIZES["tick"])
    if month_names:
        # The farmer's own month names, not the machine's locale.
        axes.xaxis.set_major_formatter(plt.FuncFormatter(
            lambda value, _: (lambda d: f"{d.day} {month_names[d.month - 1]}")(
                mdates.num2date(value))))
    else:
        axes.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)

    axes.annotate(title, xy=(0, 1), xycoords="axes fraction", xytext=(0, 48),
                  textcoords="offset points", fontsize=FONT_SIZES["title"], color="#1e2b24")
    axes.annotate(subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 26),
                  textcoords="offset points", fontsize=FONT_SIZES["subtitle"], color="#6f6e69")
    if banner:
        axes.annotate(
            banner, xy=(0, 1), xycoords="axes fraction", xytext=(0, 84),
            textcoords="offset points", fontsize=FONT_SIZES["legend"], color="white",
            fontweight="bold",
            bbox={"boxstyle": "square,pad=0.45", "facecolor": BANNER_COLOR, "edgecolor": "none"},
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path


# --------------------------------------------------------------------------- #
# The crop map
# --------------------------------------------------------------------------- #

#: Crop map classes that are not crops (towns, water, wild land, pasture), left
#: pale so the crops stand out.
_NOT_FARMLAND = set(range(81, 200))


def plot_cropmap(map_path: Path, classes: dict[int, dict], fields: dict, out_path: Path, *,
                 year: int, banner: str | None = None, top: int = 8) -> Path:
    """USDA's crop map in its own colours, with the chosen fields marked on it."""
    import rasterio
    from pyproj import Transformer

    with rasterio.open(map_path) as dataset:
        values, bounds, crs = dataset.read(1), dataset.bounds, dataset.crs
    palette = np.ones((256, 3))
    for value, row in classes.items():
        colour = np.array([row["Red"], row["Green"], row["Blue"]]) / 255.0
        # Towns, water and wild land fade to grey; the farmland keeps USDA's colours.
        palette[value] = 0.35 * colour + 0.65 if value in _NOT_FARMLAND else colour
    palette[0] = 1.0

    figure, axes = plt.subplots(figsize=(12.5, 7.6))
    axes.imshow(palette[values], extent=(bounds.left, bounds.right, bounds.bottom, bounds.top),
                interpolation="nearest")
    to_map = Transformer.from_crs(4326, crs, always_xy=True)
    labelled: list[tuple[float, float]] = []
    for feature in fields["features"]:
        ring = feature["geometry"]["coordinates"][0]
        xs, ys = zip(*[to_map.transform(lon, lat) for lon, lat in ring])
        cx, cy = float(np.mean(xs)), float(np.mean(ys))
        axes.plot(cx, cy, "o", markersize=15, markerfacecolor="none", markeredgecolor="black",
                  markeredgewidth=2.2)
        axes.plot(xs, ys, color="black", linewidth=1.2)
        # Neighbouring fields (the map is in metres) stack their labels instead of overlapping.
        below = sum(1 for x, y in labelled if math.hypot(cx - x, cy - y) < 4_000)
        labelled.append((cx, cy))
        axes.annotate(feature["properties"]["name"], (cx, cy), xytext=(12, 10 - 24 * below),
                      textcoords="offset points", fontsize=FONT_SIZES["legend"], fontweight="bold",
                      bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.85,
                            "edgecolor": "none"})

    present, counts = np.unique(values, return_counts=True)
    farm = [(n, v) for v, n in zip(present, counts) if v and v not in _NOT_FARMLAND]
    handles = [plt.Rectangle((0, 0), 1, 1, color=palette[v]) for _, v in sorted(farm, reverse=True)[:top]]
    labels = [classes[v]["Class_Names"] for _, v in sorted(farm, reverse=True)[:top]]
    axes.legend(handles, labels, loc="lower left", fontsize=FONT_SIZES["legend"] - 1,
                framealpha=0.9, title="Largest crops", title_fontsize=FONT_SIZES["legend"])
    axes.set_axis_off()
    axes.set_title(f"What grew where in {year}, and the demo's fields", loc="left",
                   fontsize=FONT_SIZES["title"], pad=34)
    axes.annotate(f"USDA NASS Cropland Data Layer {year} (public domain), 30 m. Each circle is "
                  "a real field of that crop, not ours.", xy=(0, 1), xycoords="axes fraction",
                  xytext=(0, 8), textcoords="offset points", fontsize=FONT_SIZES["subtitle"],
                  color="#6f6e69")
    if banner:
        axes.annotate(banner, xy=(0, 1), xycoords="axes fraction", xytext=(0, 62),
                      textcoords="offset points", fontsize=FONT_SIZES["legend"], color="white",
                      fontweight="bold",
                      bbox={"boxstyle": "square,pad=0.45", "facecolor": BANNER_COLOR,
                            "edgecolor": "none"})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, bbox_inches="tight", facecolor="white", dpi=DPI)
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path

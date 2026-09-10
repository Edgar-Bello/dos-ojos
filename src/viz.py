"""Rendered output: the pre-flight quicklook now, overlays and histograms later.

The quicklook exists to be looked at before ODM runs. Coverage gaps, a dropped
flight line, or a field the survey does not actually cover are obvious in a
picture and invisible in a table of numbers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # never needs a display

import matplotlib.pyplot as plt
from shapely.geometry.base import BaseGeometry

from .ingest import FlightSurvey

log = logging.getLogger(__name__)

PATH_COLOR = "#2a78d6"
FIELD_COLOR = "#199e70"
NOGPS_COLOR = "#d03b3b"
FIG_SIZE = (9.0, 8.0)
DPI = 140


def save_quicklook(
    survey: FlightSurvey,
    out_path: Path,
    *,
    field: BaseGeometry | None = None,
    field_label: str | None = None,
) -> Path:
    """Plot the flight path over the field outline and save a PNG.

    Frames without GPS are drawn on the path's midpoint in red, so a partial GPS
    dropout shows up as a cluster rather than silently vanishing from the plot.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    located = [s for s in survey.shots if s.has_gps]
    figure, axes = plt.subplots(figsize=FIG_SIZE, dpi=DPI)

    if field is not None:
        _draw_field(axes, field, field_label)

    if located:
        lons = [s.lon for s in located]
        lats = [s.lat for s in located]
        axes.plot(lons, lats, color=PATH_COLOR, linewidth=1.0, alpha=0.65, zorder=2)
        axes.scatter(
            lons, lats, s=14, color=PATH_COLOR, zorder=3,
            label=f"{len(located)} geotagged frames",
        )
        axes.scatter(
            [lons[0]], [lats[0]], s=110, marker="^", color="#0f6e56",
            edgecolor="white", linewidth=1.2, zorder=4, label="start",
        )

    missing = survey.n_images - survey.n_with_gps
    if missing and located:
        axes.scatter(
            [], [], s=40, color=NOGPS_COLOR,
            label=f"{missing} frame(s) without GPS",
        )

    _style(axes, survey)
    figure.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path


def _draw_field(axes: plt.Axes, field: BaseGeometry, label: str | None) -> None:
    """Outline the target field so coverage gaps against it are visible."""
    polygons = field.geoms if field.geom_type == "MultiPolygon" else [field]
    for index, polygon in enumerate(polygons):
        x, y = polygon.exterior.xy
        axes.fill(
            x, y, facecolor=FIELD_COLOR, alpha=0.10, edgecolor=FIELD_COLOR,
            linewidth=2.0, zorder=1,
            label=(label or "field") if index == 0 else None,
        )


def _style(axes: plt.Axes, survey: FlightSurvey) -> None:
    """Titles, aspect and the caption carrying the survey's numbers."""
    axes.set_title(
        f"{survey.flight_id} - flight path",
        fontsize=15, loc="left", pad=24,
    )
    axes.annotate(
        _caption(survey),
        xy=(0, 1), xycoords="axes fraction", xytext=(0, 8),
        textcoords="offset points", fontsize=10, color="#6f6e69",
    )
    axes.set_xlabel("longitude", fontsize=11)
    axes.set_ylabel("latitude", fontsize=11)
    axes.tick_params(labelsize=9)
    axes.ticklabel_format(useOffset=False, style="plain")
    axes.set_aspect("equal", adjustable="datalim")
    axes.grid(color="#e1e0d9", linewidth=0.7)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    if axes.get_legend_handles_labels()[0]:
        axes.legend(loc="upper right", fontsize=9, frameon=False)


def _caption(survey: FlightSurvey) -> str:
    """One line of the numbers that decide whether ODM is worth running."""
    parts = [f"{survey.n_images} images"]
    if survey.gsd_cm:
        parts.append(f"{survey.gsd_cm:.1f} cm/px")
    if survey.forward_overlap is not None:
        parts.append(f"{survey.forward_overlap:.0%} forward overlap")
    if survey.mean_spacing_m:
        parts.append(f"{survey.mean_spacing_m:.1f} m spacing")
    if survey.alt_mean_m:
        parts.append(f"{survey.alt_mean_m:.0f} m altitude")
    return "  -  ".join(parts)

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


CHM_CMAP = "YlGn"


def save_chm_png(
    chm, out_path: Path, *, resolution_m: float, title: str, subtitle: str = ""
) -> Path:
    """Render a canopy height model as a colourised PNG with a scale bar.

    Nodata is drawn in a flat grey rather than the colormap's low end, so a hole
    cannot be mistaken for bare ground.
    """
    import numpy as np

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    finite = chm[np.isfinite(chm)]
    vmax = float(np.percentile(finite, 99)) if finite.size else 1.0

    figure, axes = plt.subplots(figsize=(9.5, 8.0), dpi=DPI)
    colormap = plt.get_cmap(CHM_CMAP).copy()
    colormap.set_bad("#d9d8d2")

    extent_m = (0, chm.shape[1] * resolution_m, 0, chm.shape[0] * resolution_m)
    image = axes.imshow(
        np.ma.masked_invalid(chm), cmap=colormap, vmin=0, vmax=max(vmax, 0.1),
        extent=extent_m, origin="upper", interpolation="nearest",
    )
    bar = figure.colorbar(image, ax=axes, shrink=0.82, pad=0.02)
    bar.set_label("canopy height (m)", fontsize=11)

    axes.set_title(title, fontsize=15, loc="left", pad=24)
    if subtitle:
        axes.annotate(
            subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 8),
            textcoords="offset points", fontsize=10, color="#6f6e69",
        )
    axes.set_xlabel("metres east", fontsize=11)
    axes.set_ylabel("metres north", fontsize=11)
    axes.tick_params(labelsize=9)

    figure.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path


def save_units_overlay(
    chm,
    units,
    out_path: Path,
    *,
    transform,
    resolution_m: float,
    title: str,
    subtitle: str = "",
    column: str | None = None,
    colors: dict[str, str] | None = None,
) -> Path:
    """Draw detected units over the canopy model.

    ``column`` colours the units by one of their attributes, which is how step 8
    shows flags; without it every unit is drawn in a single accent colour.
    """
    import numpy as np
    from matplotlib.patches import Patch

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    figure, axes = plt.subplots(figsize=(10.0, 8.6), dpi=DPI)
    finite = chm[np.isfinite(chm)]
    vmax = float(np.percentile(finite, 99)) if finite.size else 1.0

    colormap = plt.get_cmap("Greys").copy()
    colormap.set_bad("#ffffff")
    height, width = chm.shape
    left, top = transform * (0, 0)
    right, bottom = transform * (width, height)
    axes.imshow(
        np.ma.masked_invalid(chm), cmap=colormap, vmin=0, vmax=max(vmax, 0.1),
        extent=(left, right, bottom, top), origin="upper", interpolation="nearest",
        alpha=0.85,
    )

    if column and column in units:
        palette = colors or {}
        for value, group in units.groupby(column):
            group.boundary.plot(
                ax=axes, linewidth=0.7,
                color=palette.get(str(value), PATH_COLOR),
            )
        axes.legend(
            handles=[
                Patch(edgecolor=palette.get(str(v), PATH_COLOR), facecolor="none",
                      label=f"{v} ({(units[column] == v).sum()})")
                for v in sorted(units[column].unique().tolist())
            ],
            loc="upper right", fontsize=9, frameon=False,
        )
    else:
        units.boundary.plot(ax=axes, linewidth=0.5, color=PATH_COLOR, alpha=0.8)

    axes.set_title(title, fontsize=15, loc="left", pad=24)
    if subtitle:
        axes.annotate(
            subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 8),
            textcoords="offset points", fontsize=10, color="#6f6e69",
        )
    axes.set_xlabel("easting (m)", fontsize=11)
    axes.set_ylabel("northing (m)", fontsize=11)
    axes.tick_params(labelsize=8)
    axes.ticklabel_format(useOffset=False, style="plain")
    axes.set_aspect("equal")

    figure.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path

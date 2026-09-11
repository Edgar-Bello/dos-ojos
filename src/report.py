"""Demo outputs: the flag overlay, the size histogram, and the join to satellite.

This is where the two halves of Dos Ojos meet. The satellite sees every field
every few days at 10 m and can say a field is drifting below its own normal; the
drone sees one field at a few centimetres and can say which plants. Joined on
``field_id``, the satellite's alarm is either confirmed plant by plant or shown
to be something else, such as a harvest.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import rasterio
from affine import Affine
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.windows import Window, from_bounds

from .flags import structure_column

log = logging.getLogger(__name__)

#: The reserved status palette. Validated as a set, amber and orange sit 13.6
#: apart for full colour vision, under the 15 floor, so hue alone cannot tell
#: STRESSED from MISSING. Every flag therefore also differs in how it is drawn.
FLAG_STYLE: dict[str, dict] = {
    "HEALTHY": {"color": "#0ca30c", "fill": False, "hatch": None, "label": "healthy"},
    "STRESSED": {"color": "#fab219", "fill": True, "hatch": None, "label": "stressed"},
    "MISSING": {"color": "#ec835a", "fill": False, "hatch": "////", "label": "missing"},
    "DEAD": {"color": "#d03b3b", "fill": True, "hatch": "xxxx", "label": "dead"},
    "NO_DATA": {"color": "#888780", "fill": False, "hatch": "....", "label": "no data"},
    # Thin and faint: on a plot trial a third of all pieces are plot ends, and at
    # full weight their outlines buried the flags that matter.
    "EDGE": {"color": "#888780", "fill": False, "hatch": None, "label": "edge, not assessed",
             "linewidth": 0.5, "alpha": 0.55},
}

#: Order problems are drawn and listed in: worst last, so it lands on top.
DRAW_ORDER = ("HEALTHY", "EDGE", "NO_DATA", "STRESSED", "MISSING", "DEAD")

#: Longest side of the orthophoto preview. A 2 cm mosaic of a 40 acre field is
#: some 400 million pixels; reading it whole to draw a slide would take gigabytes.
PREVIEW_PX = 2200

#: Share of units flagged before the drone is said to confirm a problem.
DRONE_CONCERN_SHARE = 0.10

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
#: Banner for data that is not ours. Blue, so it reads as information and can
#: never be confused with any of the four flag colours.
BANNER = "#1f5fa6"


class ReportError(RuntimeError):
    """Raised when a demo output cannot be produced."""


# --------------------------------------------------------------------------- #
# Orthophoto preview
# --------------------------------------------------------------------------- #


def read_ortho_preview(
    path: Path, bounds: tuple[float, float, float, float], *, max_px: int = PREVIEW_PX
) -> tuple[np.ndarray, Affine]:
    """Read only the part of the orthophoto under ``bounds``, decimated to fit.

    A real mosaic is far too large to read whole for a picture, so the window is
    cropped to the units and averaged down on read.

    Returns:
        ``(rgb, transform)`` with ``rgb`` shaped ``(height, width, 3)`` in 0..1.
    """
    with rasterio.open(path) as dataset:
        if dataset.count < 3:
            raise ReportError(f"{Path(path).name} is not an RGB orthophoto")
        window = from_bounds(*bounds, transform=dataset.transform)
        try:
            window = window.intersection(Window(0, 0, dataset.width, dataset.height))
        except WindowError:
            # rasterio raises rather than returning an empty window when two do
            # not meet, which would surface as a bare traceback.
            window = None
        if window is not None:
            window = window.round_offsets().round_lengths()
        if window is None or window.width < 1 or window.height < 1:
            raise ReportError(
                "the units do not overlap the orthophoto; check both come from "
                "the same flight"
            )

        scale = max(window.width, window.height) / float(max_px)
        scale = max(scale, 1.0)
        out_w = max(1, int(round(window.width / scale)))
        out_h = max(1, int(round(window.height / scale)))
        data = dataset.read(
            [1, 2, 3], window=window, out_shape=(3, out_h, out_w),
            resampling=Resampling.average,
        ).astype(np.float64)
        transform = dataset.window_transform(window) * Affine.scale(
            window.width / out_w, window.height / out_h
        )

    peak = 255.0 if data.max() > 1.0 else 1.0
    rgb = np.clip(np.moveaxis(data, 0, -1) / peak, 0, 1)
    empty = rgb.sum(axis=-1) == 0
    rgb[empty] = 1.0          # outside the mosaic draws as page, not as black
    return rgb, transform


def _extent(transform: Affine, shape: tuple[int, int]) -> tuple[float, float, float, float]:
    """Matplotlib extent (left, right, bottom, top) for a raster."""
    height, width = shape
    left, top = transform.c, transform.f
    return left, left + width * transform.a, top + height * transform.e, top


# --------------------------------------------------------------------------- #
# Flag overlay
# --------------------------------------------------------------------------- #


def save_flag_overlay(
    flagged: gpd.GeoDataFrame,
    ortho_path: Path | None,
    out_path: Path,
    *,
    title: str,
    subtitle: str = "",
    missing_points: gpd.GeoDataFrame | None = None,
    banner: str | None = None,
) -> Path:
    """Draw every unit's flag over the orthomosaic.

    Healthy units are left undrawn so the photo shows through and the eye goes
    straight to the problems. ``banner`` marks borrowed data above the title.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if flagged.empty:
        raise ReportError("no flagged units to draw")

    minx, miny, maxx, maxy = flagged.total_bounds
    margin = 0.03 * max(maxx - minx, maxy - miny)
    bounds = (minx - margin, miny - margin, maxx + margin, maxy + margin)

    figure, axes = plt.subplots(figsize=(11, 10), dpi=150)
    figure.patch.set_facecolor(SURFACE)

    if ortho_path is not None and Path(ortho_path).exists():
        rgb, transform = read_ortho_preview(ortho_path, bounds)
        axes.imshow(rgb, extent=_extent(transform, rgb.shape[:2]), interpolation="bilinear")
    else:
        axes.set_facecolor("#f1efe8")
        log.warning("no orthophoto; drawing flags on a plain background")

    handles = []
    counts = flagged["flag"].value_counts().to_dict()
    for flag in DRAW_ORDER:
        group = flagged[flagged["flag"] == flag]
        if group.empty or flag == "HEALTHY":
            continue
        style = FLAG_STYLE[flag]
        group.plot(
            ax=axes,
            facecolor=style["color"] if style["fill"] else "none",
            edgecolor=style["color"],
            hatch=style["hatch"],
            linewidth=style.get("linewidth", 1.1),
            alpha=style.get("alpha", 0.78 if style["fill"] else 1.0),
        )
        handles.append(_legend_patch(flag, counts.get(flag, 0)))

    if missing_points is not None and not missing_points.empty:
        style = FLAG_STYLE["MISSING"]
        axes.scatter(
            missing_points.geometry.x, missing_points.geometry.y,
            s=260, facecolors="none", edgecolors=style["color"], linewidths=2.6,
            marker="o", zorder=5,
        )
        axes.scatter(
            missing_points.geometry.x, missing_points.geometry.y,
            s=90, color=style["color"], marker="x", linewidths=2.2, zorder=6,
        )
        handles.append(Line2D(
            [], [], marker="o", color="none", markeredgecolor=style["color"],
            markeredgewidth=2.4, markersize=13,
            label=f"missing ({len(missing_points)})",
        ))

    healthy = counts.get("HEALTHY", 0)
    handles.insert(0, Patch(facecolor="none", edgecolor="none",
                            label=f"healthy ({healthy}), not drawn"))

    axes.set_xlim(bounds[0], bounds[2])
    axes.set_ylim(bounds[1], bounds[3])
    axes.set_aspect("equal")
    axes.set_xticks([])
    axes.set_yticks([])
    for side in axes.spines.values():
        side.set_visible(False)

    _scale_bar(axes, bounds)
    axes.legend(
        handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1.0),
        frameon=False, fontsize=12, labelcolor=INK_SECONDARY,
        handlelength=2.4, handleheight=1.6,
    )
    axes.set_title(title, fontsize=19, color=INK, loc="left", pad=30)
    if subtitle:
        axes.annotate(
            subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 10),
            textcoords="offset points", fontsize=12, color=INK_SECONDARY,
        )
    if banner:
        _banner(axes, banner, offset=62)

    figure.savefig(out_path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path


def _banner(axes: plt.Axes, text: str, *, offset: float) -> None:
    """A solid band above the title, for data that did not come from our flights."""
    axes.annotate(
        text, xy=(0, 1), xycoords="axes fraction", xytext=(0, offset),
        textcoords="offset points", fontsize=12, color="white", fontweight="bold",
        bbox={"boxstyle": "square,pad=0.45", "facecolor": BANNER, "edgecolor": "none"},
    )


def _legend_patch(flag: str, count: int) -> Patch:
    """A legend entry drawn the same way as the flag on the map."""
    style = FLAG_STYLE[flag]
    return Patch(
        facecolor=style["color"] if style["fill"] else "none",
        edgecolor=style["color"], hatch=style["hatch"], linewidth=1.4,
        alpha=0.78 if style["fill"] else 1.0,
        label=f"{style['label']} ({count})",
    )


def _scale_bar(axes: plt.Axes, bounds: tuple[float, float, float, float]) -> None:
    """A round-numbered scale bar in the lower left, in metres."""
    width = bounds[2] - bounds[0]
    target = width / 5
    magnitude = 10 ** math.floor(math.log10(target))
    length = min((m * magnitude for m in (1, 2, 5, 10)), key=lambda v: abs(v - target))
    x0 = bounds[0] + 0.04 * width
    y0 = bounds[1] + 0.04 * (bounds[3] - bounds[1])
    axes.plot([x0, x0 + length], [y0, y0], color="white", linewidth=6, solid_capstyle="butt")
    axes.plot([x0, x0 + length], [y0, y0], color=INK, linewidth=3, solid_capstyle="butt")
    axes.annotate(
        f"{length:g} m", xy=(x0 + length / 2, y0), xytext=(0, 7),
        textcoords="offset points", ha="center", fontsize=11, color=INK,
        bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "edgecolor": "none", "alpha": 0.8},
    )


# --------------------------------------------------------------------------- #
# Histogram
# --------------------------------------------------------------------------- #


def save_flag_histogram(
    flagged: gpd.GeoDataFrame, method: str, out_path: Path, *, title: str,
    banner: str | None = None, within: str | None = None,
) -> Path:
    """Histogram of unit size, stacked by flag, so the flagged tail stands out.

    Plots the measure the flags were decided on: canopy volume for crowns, mean
    canopy height for row segments. Row volume would put an artifact tail on the
    left, made of segments the field boundary clipped short. When units were
    judged ``within`` blocks, each is plotted as a share of its own block's
    median, since that is the comparison that flagged it.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    column = structure_column(method)
    values = flagged[column]
    if within:
        values = 100.0 * values / flagged.groupby(within)[column].transform("median")
        flagged = flagged.assign(**{column: values})
    finite = values[np.isfinite(values)]
    if finite.empty:
        raise ReportError(f"no finite {column} values to plot")

    # A handful of outliers would otherwise stretch the axis and squeeze the rest;
    # they are drawn in the last bin instead.
    top = float(np.percentile(finite, 99.5))
    values = values.clip(upper=top)
    flagged = flagged.assign(**{column: values})
    finite = values[np.isfinite(values)]
    bins = np.histogram_bin_edges(finite, bins=min(40, max(10, int(math.sqrt(len(finite))))))
    figure, axes = plt.subplots(figsize=(11, 5.6), dpi=150)
    figure.patch.set_facecolor(SURFACE)
    axes.set_facecolor(SURFACE)

    bottom = np.zeros(len(bins) - 1)
    order = ("HEALTHY", "STRESSED", "MISSING", "DEAD")
    for flag in order:
        subset = flagged.loc[(flagged["flag"] == flag) & np.isfinite(values), column]
        if subset.empty:
            continue
        counts, _ = np.histogram(subset, bins=bins)
        style = FLAG_STYLE[flag]
        axes.bar(
            bins[:-1], counts, width=np.diff(bins), bottom=bottom, align="edge",
            color=style["color"] if flag != "HEALTHY" else "#c3c2b7",
            edgecolor=SURFACE, linewidth=1.2, hatch=style["hatch"],
            label=f"{style['label']} ({len(subset)})",
        )
        bottom += counts

    median = float(np.median(finite))
    axes.axvline(median, color=INK_SECONDARY, linewidth=1.6, linestyle="--")
    axes.annotate(
        f"block median {median:.0f}%" if within else f"field median {median:.2f}",
        xy=(median, bottom.max()), xytext=(6, -4),
        textcoords="offset points", fontsize=11, color=INK_SECONDARY, va="top",
    )

    unit = "canopy height (m)" if column == "height_mean_m" else "canopy volume (m3)"
    if within:
        unit = unit.split(" (")[0] + ", % of its own block's median"
    noun = "row segments" if method == "rows" else "crowns"
    axes.set_xlabel(unit, fontsize=14, color=INK)
    axes.set_ylabel(noun, fontsize=14, color=INK)
    axes.tick_params(labelsize=12, colors=INK_MUTED)
    axes.grid(axis="y", color=GRID, linewidth=1)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color("#c3c2b7")
    axes.legend(frameon=False, fontsize=12, labelcolor=INK_SECONDARY, loc="upper left")
    axes.set_title(title, fontsize=19, color=INK, loc="left", pad=14)
    if banner:
        _banner(axes, banner, offset=44)

    figure.savefig(out_path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(figure)
    log.info("wrote %s", out_path)
    return out_path


# --------------------------------------------------------------------------- #
# Block summary and the satellite join
# --------------------------------------------------------------------------- #


def block_summary(
    flagged: gpd.GeoDataFrame,
    *,
    flight_id: str,
    field_id: str | None,
    method: str,
    flown_on: str | None = None,
    n_missing_positions: int = 0,
    source: str | None = None,
) -> dict:
    """One field-level record, shaped to join the satellite's flags.json.

    The requested keys are kept as named, ``n_trees`` included, since other code
    may already read them. For a row crop ``n_trees`` counts row segments, and
    ``unit_type`` says so. ``source`` is set only for data that is not ours.
    """
    counts = flagged["flag"].value_counts().to_dict()
    n_missing = int(counts.get("MISSING", 0)) + int(n_missing_positions)
    n_units = int(len(flagged))
    n_not_assessed = int(counts.get("EDGE", 0)) + int(counts.get("NO_DATA", 0))
    # Shares are over units actually judged. Counting edge slivers in the
    # denominator would dilute every rate by however much boundary the flight had.
    denominator = n_units - n_not_assessed + int(n_missing_positions)

    def share(n: int) -> float:
        return round(n / denominator, 4) if denominator else 0.0

    n_stressed = int(counts.get("STRESSED", 0))
    n_dead = int(counts.get("DEAD", 0))
    volume = flagged["volume_m3"].dropna()
    exg = flagged["exg_mean"].dropna() if "exg_mean" in flagged else volume.iloc[0:0]

    return {
        "field_id": field_id,
        "flight_id": flight_id,
        "flown_on": flown_on,
        "method": method,
        "unit_type": "row_segment" if method == "rows" else "crown",
        "n_trees": n_units,
        "n_healthy": int(counts.get("HEALTHY", 0)),
        "n_stressed": n_stressed,
        "n_dead": n_dead,
        "n_missing": n_missing,
        "n_not_assessed": n_not_assessed,
        "n_judged": int(denominator),
        "share_stressed": share(n_stressed),
        "share_dead": share(n_dead),
        "share_missing": share(n_missing),
        "share_problem": share(n_stressed + n_dead + n_missing),
        "median_canopy_volume": round(float(volume.median()), 4) if len(volume) else None,
        "mean_ExG": round(float(exg.mean()), 4) if len(exg) else None,
        "n_blocks": int(flagged["block"].nunique()) if "block" in flagged else None,
        "source": source,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def load_satellite_flags(path: Path) -> dict:
    """Read the satellite pipeline's ranked flags.json.

    Raises:
        ReportError: naming the command that produces it, if it is absent.
    """
    path = Path(path)
    if not path.exists():
        raise ReportError(
            f"satellite flags not found at {path}. Produce them in the satellite "
            "project with: dosojos-sat score --season <year> --out out/flags.json"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "fields" not in payload:
        raise ReportError(f"{path} has no 'fields' list; is it a satellite flags.json?")
    return payload


def agreement(satellite: dict | None, drone: dict | None, *, concern: float) -> str:
    """How the two eyes compare for one field.

    ``confirmed``      both see a problem
    ``not_confirmed``  satellite alarm, drone finds the plants mostly fine:
                       often a harvest, which looks identical from orbit
    ``drone_only``     drone finds problems the satellite's 10 m pixels missed
    ``both_clear``     neither sees a problem
    ``satellite_only`` / ``drone_only_no_satellite``  only one eye has looked
    """
    if drone is None:
        return "satellite_only"
    if satellite is None:
        return "drone_only_no_satellite"
    satellite_alarm = bool(satellite.get("flagged"))
    drone_alarm = (drone.get("share_problem") or 0.0) >= concern
    if satellite_alarm and drone_alarm:
        return "confirmed"
    if satellite_alarm:
        return "not_confirmed"
    if drone_alarm:
        return "drone_only"
    return "both_clear"


def join_with_satellite(
    satellite: dict, drone_summaries: list[dict], *, concern: float = DRONE_CONCERN_SHARE
) -> dict:
    """Merge drone block summaries into the satellite's ranked list on field_id.

    The satellite's ranking and fields are kept exactly; each field gains a
    ``drone`` object and an ``agreement`` verdict. Where one field has several
    flights, the most recent is used and the rest are counted.
    """
    latest: dict[str, dict] = {}
    extra: dict[str, int] = {}
    for summary in drone_summaries:
        field_id = summary.get("field_id")
        if not field_id:
            continue
        current = latest.get(field_id)
        if current is None or (summary.get("flown_on") or "") > (current.get("flown_on") or ""):
            if current is not None:
                extra[field_id] = extra.get(field_id, 0) + 1
            latest[field_id] = summary
        else:
            extra[field_id] = extra.get(field_id, 0) + 1

    satellite_ids = {entry["field_id"] for entry in satellite["fields"]}
    fields = []
    for entry in satellite["fields"]:
        drone = latest.get(entry["field_id"])
        merged = dict(entry)
        merged["drone"] = _drone_view(drone, extra.get(entry["field_id"], 0))
        merged["agreement"] = agreement(entry, drone, concern=concern)
        fields.append(merged)

    for field_id, drone in sorted(latest.items()):
        if field_id not in satellite_ids:
            fields.append({
                "field_id": field_id, "drone": _drone_view(drone, extra.get(field_id, 0)),
                "agreement": agreement(None, drone, concern=concern),
            })

    return {
        "season": satellite.get("season"),
        "satellite_generated": satellite.get("generated"),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "drone_concern_share": concern,
        "fields": fields,
    }


def _drone_view(summary: dict | None, n_other_flights: int) -> dict | None:
    """The drone fields worth carrying into the joined record."""
    if summary is None:
        return None
    keep = (
        "flight_id", "flown_on", "unit_type", "n_trees", "n_judged", "n_healthy",
        "n_stressed", "n_dead", "n_missing", "share_problem", "median_canopy_volume",
        "mean_ExG",
    )
    view = {key: summary.get(key) for key in keep}
    if summary.get("source"):
        view["source"] = summary["source"]
    if n_other_flights:
        view["n_other_flights"] = n_other_flights
    return view

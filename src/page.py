"""One page holding everything a flight found, satellite or no satellite.

The commands each leave their own picture in the out folder, which is right for
the team and wrong for everyone else: nobody wants to be handed six PNGs and a
JSON. This builds a single file that can be opened, saved, forwarded, or read on
a phone with no signal, because every picture is carried inside it.

It is built to be **worth opening with the drone half alone**. A field can have no
satellite record for honest reasons - too small for a Sentinel-2 pixel, outside
what we have fetched, or simply new - and the page then says so in as many words
instead of quietly leaving a gap. What the drone saw stands on its own.
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from pathlib import Path

#: Pictures the page shows, in the order it shows them, with what each is for.
#: Every one is optional: a flight that skipped a step simply has no such section.
FIGURES: tuple[tuple[str, str, str], ...] = (
    ("quicklook.png", "The flight",
     "Where the drone went and what it saw. Read this first: if the path has holes, "
     "everything below has holes too."),
    ("chm.png", "How tall the crop is",
     "Height above the ground under it, measured from the photos. Bare ground is "
     "zero."),
    ("flag_overlay.png", "Plant by plant",
     "Every plant the flight could judge, and the ones worth walking out to."),
    ("flag_histogram.png", "How the field is spread",
     "Most of a field sits in the middle. The tail on the left is what to look at."),
    ("units_watershed.png", "The crowns it found",
     "One outline per tree. Where trees have grown into each other it draws one "
     "crown for two, so treat the count as a floor."),
    ("terrain.png", "The lie of the land",
     "Where water runs and where it sits, from the ground under the crop."),
    ("thermal.png", "Warm patches",
     "Leaves running hotter than the rest of the field. A scorecard, not a "
     "diagnosis."),
    ("answer_key.png", "Scored against the ground truth",
     "This field was measured by hand, so the flight can be checked rather than "
     "admired."),
)

#: Satellite pictures, looked for beside the satellite's flags file.
SATELLITE_FIGURES: tuple[tuple[str, str, str], ...] = (
    ("{field}_NDVI.png", "Green, against its own normal",
     "This season against what this same field usually does on this date. The "
     "yardstick is its own history, never another farm."),
    ("{field}_water.png", "The water checkbook",
     "The root zone as a tank: what the soil holds, what went in, what the sun "
     "took out."),
)

#: Said plainly wherever the satellite half is missing, so a gap is never silent.
NO_SATELLITE = (
    "There is no satellite record for this field, so this page is the drone's word "
    "alone. That is not a failure: a field can be too small for a Sentinel-2 pixel "
    "(they are 10 m across, so anything under about an acre is a couple of pixels), "
    "it can sit outside what we have fetched, or it can simply be new. What it means "
    "for you is that there is no <em>how does this compare with its own normal</em> "
    "here, and no water advice. Everything below was measured from the photos."
)

STYLE = """
:root { --ink:#1b1b1b; --quiet:#5c5c5c; --line:#dcdcdc; --paper:#fff;
        --warn:#b3261e; --good:#2b7a4b; }
* { box-sizing:border-box; }
body { margin:0; background:#f4f3f0; color:var(--ink);
       font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.sheet { max-width:820px; margin:0 auto; background:var(--paper); padding:24px 20px 48px; }
h1 { font-size:1.5rem; margin:0 0 2px; line-height:1.25; }
h2 { font-size:1.15rem; margin:36px 0 6px; padding-top:14px; border-top:1px solid var(--line); }
h1 + p.sub { margin:0 0 18px; color:var(--quiet); }
p { margin:0 0 12px; }
.banner { background:var(--warn); color:#fff; font-weight:700; padding:9px 12px;
          border-radius:3px; margin:0 0 16px; font-size:.86rem; }
.note { background:#f3f0e6; border-left:4px solid #c8a93a; padding:12px 14px;
        border-radius:3px; margin:0 0 16px; }
figure { margin:12px 0 0; }
figure img { width:100%; height:auto; display:block; border:1px solid var(--line);
             border-radius:3px; }
figcaption { color:var(--quiet); font-size:.9rem; margin-top:6px; }
.table-wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; margin:8px 0 0; font-size:.94rem; }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
.headline { font-size:1.06rem; }
.sources { color:var(--quiet); font-size:.88rem; }
.sources li { margin-bottom:5px; }
@media (max-width:420px) { .sheet { padding:16px 14px 36px; } h1 { font-size:1.3rem; } }
"""


class PageError(RuntimeError):
    """The page cannot be built from what is on disk."""


@dataclass
class Figure:
    """One picture on the page, already read off disk."""

    title: str
    caption: str
    data_uri: str


@dataclass
class PageData:
    """Everything the page draws, gathered before any HTML is written."""

    flight_id: str
    field_id: str
    field_name: str
    crop: str
    flown_on: str
    source: str
    n_images: int | None = None
    summary: dict = dataclass_field(default_factory=dict)
    terrain: dict = dataclass_field(default_factory=dict)
    thermal: dict = dataclass_field(default_factory=dict)
    satellite: dict | None = None
    figures: list[Figure] = dataclass_field(default_factory=list)
    satellite_figures: list[Figure] = dataclass_field(default_factory=list)
    generated: str = ""


def data_uri(path: Path) -> str:
    """A picture as a string the page can carry, so the file works offline."""
    kind = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{kind};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _read_json(path: Path) -> dict:
    """The file's contents, or an empty dict where a step was never run."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gather(
    out_dir: Path,
    flight_id: str,
    *,
    flight: dict,
    field_name: str = "",
    satellite_dir: Path | None = None,
    triage_path: Path | None = None,
) -> PageData:
    """Read everything the flight left on disk. Missing steps are simply absent."""
    flight_dir = out_dir / flight_id
    if not flight_dir.is_dir():
        raise PageError(
            f"nothing to show for {flight_id}: {flight_dir} does not exist. Run "
            f"'dosojos-drone report {flight_id}' first."
        )

    summary = _read_json(flight_dir / "block_summary.json")
    survey = _read_json(flight_dir / "survey.json")
    field_id = summary.get("field_id") or flight.get("field_id") or ""

    data = PageData(
        flight_id=flight_id,
        field_id=field_id,
        field_name=field_name or field_id,
        crop=flight.get("crop") or summary.get("crop") or "",
        flown_on=flight.get("flown_on") or summary.get("flown_on") or "",
        source=flight.get("source") or summary.get("source") or "",
        n_images=survey.get("n_images"),
        summary=summary,
        terrain=_read_json(flight_dir / "terrain.json"),
        thermal=_read_json(flight_dir / "thermal.json"),
        generated=date.today().isoformat(),
    )

    for name, title, caption in FIGURES:
        path = flight_dir / name
        if path.exists():
            data.figures.append(Figure(title, caption, data_uri(path)))

    if satellite_dir is not None and field_id:
        for pattern, title, caption in SATELLITE_FIGURES:
            path = satellite_dir / pattern.format(field=field_id)
            if path.exists():
                data.satellite_figures.append(Figure(title, caption, data_uri(path)))

    if triage_path is not None:
        triage = _read_json(triage_path)
        for entry in triage.get("fields", []):
            if entry.get("field_id") == field_id and entry.get("latest_ndvi") is not None:
                data.satellite = entry
                break

    return data


def headline(data: PageData) -> str:
    """The one sentence to read if you read nothing else."""
    summary = data.summary
    judged = summary.get("n_judged") or 0
    share = summary.get("share_problem")
    unit = "trees" if summary.get("unit_type") == "crown" else "stretches of row"
    if not judged:
        return ("The flight was processed, but nothing was judged plant by plant yet. "
                "The pictures below are what it saw.")
    if share is None:
        return f"{judged:,} {unit} were measured."
    problem = round(share * judged)
    if share < 0.05:
        return (f"{judged:,} {unit} were measured and {problem:,} of them "
                f"({share:.0%}) are worth a look. That is a field in good order.")
    return (f"{judged:,} {unit} were measured and {problem:,} of them "
            f"({share:.0%}) are worth a look.")


def _figure_html(figure: Figure) -> str:
    return (
        f"<figure><img alt=\"{html.escape(figure.title)}\" src=\"{figure.data_uri}\">"
        f"<figcaption>{html.escape(figure.caption)}</figcaption></figure>"
    )


def _counts_table(summary: dict) -> str:
    """What the flight judged, as counts and shares."""
    rows = [
        ("Healthy", summary.get("n_healthy"), None),
        ("Worth a look", summary.get("n_stressed"), summary.get("share_stressed")),
        ("Dead", summary.get("n_dead"), summary.get("share_dead")),
        ("Missing", summary.get("n_missing"), summary.get("share_missing")),
        ("Not judged", summary.get("n_not_assessed"), None),
    ]
    body = []
    for name, count, share in rows:
        if count is None:
            continue
        percent = f"{share:.1%}" if share is not None else ""
        body.append(
            f"<tr><td>{name}</td><td class=\"num\">{count:,}</td>"
            f"<td class=\"num\">{percent}</td></tr>"
        )
    if not body:
        return ""
    return (
        "<div class=\"table-wrap\"><table><thead><tr><th>Verdict</th>"
        "<th class=\"num\">Count</th><th class=\"num\">Share</th></tr></thead>"
        "<tbody>" + "".join(body) + "</tbody></table></div>"
    )


def _ground_html(terrain: dict) -> str:
    """What the ground model found, worst first.

    ``terrain.json`` keeps each finding as its own record - what was measured, and
    what to do about it - so the page lists them rather than running them together.
    """
    findings = terrain.get("advice") or []
    if isinstance(findings, str):                 # an older terrain.json
        findings = [{"finding": findings, "priority": 1}]
    if not findings:
        return ""

    ordered = sorted(findings, key=lambda item: item.get("priority", 9))
    items = []
    for item in ordered:
        said = html.escape(str(item.get("finding") or ""))
        todo = html.escape(str(item.get("advice") or ""))
        items.append(f"<li>{said}" + (f" <em>{todo}</em>" if todo else "") + "</li>")

    source = terrain.get("ground_source")
    tail = (f"<p class=\"sources\">Ground measured from {html.escape(str(source))}.</p>"
            if source else "")
    return "<h2>The ground</h2><ul>" + "".join(items) + "</ul>" + tail


def _satellite_html(data: PageData) -> str:
    """The satellite's own words, or a plain statement that there are none."""
    if not data.satellite and not data.satellite_figures:
        return f"<h2>No satellite for this field</h2><div class=\"note\">{NO_SATELLITE}</div>"

    parts = ["<h2>What the satellite says</h2>"]
    entry = data.satellite or {}
    if entry:
        note = entry.get("note") or ""
        seen = entry.get("last_observation_date") or ""
        ndvi = entry.get("latest_ndvi")
        percentile = entry.get("percentile")
        line = f"Last clear look {html.escape(str(seen))}: green reads {ndvi:.2f}" \
            if ndvi is not None else "Last clear look " + html.escape(str(seen))
        if percentile is not None:
            line += (f", which is higher than {percentile:.0f}% of what this field has "
                     f"done on this date in past years")
        if note:
            line += f" - {html.escape(str(note))}"
        parts.append(f"<p class=\"headline\">{line}.</p>")
        water = entry.get("water") or {}
        if water.get("status"):
            days = water.get("days_left")
            when = ("water now" if days == 0 else
                    "no watering needed for over six weeks" if days is None else
                    f"about {days} days of water left")
            parts.append(f"<p>Water: {html.escape(str(when))}"
                         + (f", by {html.escape(str(water['water_by']))}"
                            if water.get("water_by") and days else "") + ".</p>")
    for figure in data.satellite_figures:
        parts.append(f"<h2>{html.escape(figure.title)}</h2>{_figure_html(figure)}")
    return "".join(parts)


def render(data: PageData) -> str:
    """The whole page as one string, pictures and all."""
    public = "public" in data.source.lower() or "cc0" in data.source.lower() \
        or "cc-by" in data.source.lower() or "public domain" in data.source.lower()
    title = f"{data.field_name} - drone flight {data.flown_on}".strip(" -")

    parts = [
        "<title>", html.escape(title), "</title>",
        "<style>", STYLE, "</style>",
        "<div class=\"sheet\">",
    ]
    if public and data.source:
        parts.append(
            f"<p class=\"banner\">FREE PUBLIC DATA, NOT OUR FLIGHT &nbsp;-&nbsp; "
            f"{html.escape(data.source)}</p>"
        )
    parts.append(f"<h1>{html.escape(data.field_name)}</h1>")
    subtitle = " &middot; ".join(
        html.escape(str(bit)) for bit in
        (data.crop, f"flown {data.flown_on}" if data.flown_on else "",
         f"{data.n_images} photos" if data.n_images else "") if bit
    )
    parts.append(f"<p class=\"sub\">{subtitle}</p>")
    parts.append(f"<p class=\"headline\">{html.escape(headline(data))}</p>")

    parts.append(_satellite_html(data))

    parts.append("<h2>What the flight measured</h2>")
    table = _counts_table(data.summary)
    if table:
        parts.append(table)
    else:
        parts.append("<p>Nothing was judged plant by plant on this flight.</p>")

    ground = _ground_html(data.terrain)
    if ground:
        parts.append(ground)

    for figure in data.figures:
        parts.append(f"<h2>{html.escape(figure.title)}</h2>{_figure_html(figure)}")

    parts.append("<h2>Where this came from, and what it is not</h2><ul class=\"sources\">")
    if data.source:
        parts.append(f"<li>Pictures: {html.escape(data.source)}.</li>")
    parts.append(
        "<li>Heights are measured from the photographs, not with a tape. On tree "
        "crops they run short; trust the order more than the number.</li>"
    )
    if not data.satellite and not data.satellite_figures:
        parts.append("<li>No satellite: see the note near the top.</li>")
    parts.append(
        "<li>This is what a camera saw on one day. It cannot tell thirst from "
        "disease from a fertiliser miss. Go and look.</li>"
    )
    parts.append(f"<li>Page made {html.escape(data.generated)} "
                 f"from flight {html.escape(data.flight_id)}.</li>")
    parts.append("</ul></div>")
    return "".join(parts)

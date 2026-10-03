"""Renders the standalone static HTML map report for a trip."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from route_recap.core.models import TripSummary

_TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Distinct, basemap-friendly colors for per-day route rendering. Trips with
#: more days than colors cycle through the palette.
DAY_PALETTE = [
    "#d62728",  # red
    "#2ca02c",  # green
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#9467bd",  # purple
    "#17becf",  # cyan
    "#e377c2",  # pink
    "#8c564b",  # brown
    "#bcbd22",  # olive
    "#393b79",  # indigo
    "#843c39",  # maroon
    "#637939",  # dark green
]


def _day_count(summary: TripSummary) -> int:
    """Number of distinct trip days covered by the route segments."""
    return summary.day_count or 1


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _fmt_date_range(summary: TripSummary) -> str:
    start, end = summary.started_at, summary.ended_at

    def fmt(dt: datetime) -> str:
        return dt.strftime("%d %b %Y")

    if start and end and start.date() != end.date():
        return f"{fmt(start)} – {fmt(end)}"
    if start:
        return fmt(start)
    return "—"


def _json_for_script(data: object) -> str:
    """JSON safe to inline inside a <script> element.

    HTML parsers do not decode entities inside script blocks, so the
    characters that could terminate the element or be misparsed are
    escaped as JSON unicode sequences.
    """
    return (
        json.dumps(data, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _provider_note(summary: TripSummary) -> str:
    if summary.routing_provider == "google":
        return "routed with Google Maps"
    if summary.routing_provider == "google+osrm":
        return "routed with Google Maps + OSRM"
    if summary.routing_provider == "osrm":
        return "routed with OSRM"
    return f"routing: {summary.routing_provider or 'none'}"


def _render_html(
    summary: TripSummary,
    *,
    carto_key: str | None = None,
    assets: dict[str, dict] | None = None,
    inline: dict[str, str] | None = None,
) -> str:
    """Render the report template to a string.

    ``inline`` carries pre-fetched library sources (``vendor_css`` /
    ``vendor_js``); when omitted the template links Leaflet from the CDN.
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("map.html.j2")

    trip_data = {**summary.model_dump(mode="json"), "assets": assets or {}}
    context = {
        "trip_name": summary.trip_name,
        "trip_json": _json_for_script(trip_data),
        "carto_key": carto_key,
        "date_range": _fmt_date_range(summary),
        "generated_at": datetime.now().strftime("%d %b %Y, %H:%M"),
        "distance_display": summary.display_distance(),
        "driving_display": _fmt_duration(summary.driving_duration_s),
        "stop_duration_display": _fmt_duration(summary.stop_duration_s),
        "stop_count": summary.stop_count,
        "off_road_count": summary.off_road_count,
        "media_count": f"{summary.media_processed:,}",
        "provider_note": _provider_note(summary),
        "day_count": _day_count(summary),
        "day_palette": DAY_PALETTE,
    }
    if inline:
        context.update(inline)
    return template.render(**context)


def build_html(
    summary: TripSummary,
    output_dir: str | Path,
    *,
    carto_key: str | None = None,
    assets: dict[str, dict] | None = None,
) -> Path:
    """Render ``index.html`` + ``summary.json`` for a trip and return the path.

    ``carto_key`` enables CARTO Voyager basemap tiles in the report (the key
    is embedded in the HTML — it is a browser-side token by design). When
    omitted, the report falls back to keyless OpenStreetMap tiles.

    ``assets`` maps waypoint source-file paths (as stored in
    ``Waypoint.source_files``) to ``{"url", "thumb", "kind"}`` entries created
    by :func:`route_recap.generator.stage_media`; the map is embedded in the
    report so popups can render photo strips. Media is viewable through
    ``route-recap serve`` (browsers block file:// images).

    This produces the *folder* report: media stays as separate files, so the
    photos need an HTTP server. For a single shareable file see
    :func:`route_recap.generator.single_file.build_single_file`.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    html = _render_html(summary, carto_key=carto_key, assets=assets)
    trip_data = {**summary.model_dump(mode="json"), "assets": assets or {}}

    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    (output_dir / "summary.json").write_text(
        json.dumps(trip_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return index_path

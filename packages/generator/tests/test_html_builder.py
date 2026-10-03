"""Unit tests for route_recap.generator.html_builder day-color rendering."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import polyline

from route_recap.core.models import (
    RouteSegment,
    TripSummary,
    Waypoint,
)
from route_recap.generator.html_builder import DAY_PALETTE, build_html

T0 = datetime(2026, 8, 14, 9, 0, 0)


def _summary(*, day_indexes: list[int | None]) -> TripSummary:
    """A small multi-segment trip with one waypoint per segment boundary."""
    coords = [(63.4 + 0.01 * i, -19.0 - 0.01 * i) for i in range(len(day_indexes) + 1)]
    waypoints = [
        Waypoint(
            latitude=lat,
            longitude=lon,
            timestamp=T0 + timedelta(days=i),
        )
        for i, (lat, lon) in enumerate(coords)
    ]
    segments = [
        RouteSegment(
            start_index=i,
            end_index=i + 1,
            encoded_polyline=polyline.encode(coords[i : i + 2], precision=5),
            distance_m=1000.0,
            duration_s=600.0,
            day_index=day,
        )
        for i, day in enumerate(day_indexes)
    ]
    return TripSummary(
        trip_name="Test Trip",
        started_at=T0,
        ended_at=T0 + timedelta(days=max(len(day_indexes) - 1, 0)),
        distance_km=float(len(segments)),
        waypoints=waypoints,
        segments=segments,
    )


def test_build_html_writes_index_and_summary(tmp_path):
    summary = _summary(day_indexes=[0, 1, 2])
    index = build_html(summary, tmp_path / "trip")

    assert index == tmp_path / "trip" / "index.html"
    html = index.read_text(encoding="utf-8")
    summary_json = json.loads((tmp_path / "trip" / "summary.json").read_text("utf-8"))

    # Day count and palette are embedded for the frontend.
    assert '"#d62728"' in html  # first palette color
    assert "day-toggles" in html
    assert "Days · 3" in html
    # Each segment keeps its assigned day in the embedded data.
    assert [s["day_index"] for s in summary_json["segments"]] == [0, 1, 2]


def test_build_html_single_day_still_renders(tmp_path):
    summary = _summary(day_indexes=[None, None])
    index = build_html(summary, tmp_path / "trip")
    assert "Days · 1" in index.read_text(encoding="utf-8")


def test_day_palette_is_non_empty_and_distinct():
    assert len(DAY_PALETTE) >= 8
    assert len(set(DAY_PALETTE)) == len(DAY_PALETTE)
    assert all(c.startswith("#") and len(c) == 7 for c in DAY_PALETTE)


def test_build_html_includes_theme_icon_and_reveal_hooks(tmp_path):
    summary = _summary(day_indexes=[0, 1])
    html = build_html(summary, tmp_path / "trip").read_text(encoding="utf-8")

    # Dark-mode toggle: button, theme variables, persistence key, tile fallback.
    assert 'id="btn-theme"' in html
    assert "html.dark" in html
    assert "route-recap-theme" in html
    assert "tiles-fallback" in html
    assert "dark_all" in html  # CARTO dark basemap style

    # Icons instead of plain circles for waypoints / off-road POIs.
    assert "poi-icon" in html
    assert "📷" in html
    assert "🏔️" in html

    # Arrows take their day's route color, and the route reveal is progressive.
    assert "arrowSvg" in html
    assert "renderTrail" in html
    assert "setStaticRouteVisible" in html
    # The car must not be rotated to the travel heading any more.
    assert "headingAt" not in html


def test_build_html_has_reset_arrow_spacing_and_legend_clarity(tmp_path):
    summary = _summary(day_indexes=[0, 1])
    html = build_html(summary, tmp_path / "trip").read_text(encoding="utf-8")

    # Restart control for the journey.
    assert 'id="anim-reset"' in html
    assert "Back to the start of the journey" in html

    # Arrows are placed at an even distance interval, rotated inside the SVG.
    assert "ARROW_TARGET" in html
    assert "ARROW_MIN_SPACING_KM" in html
    assert "rot.toFixed(1)" in html  # rotation baked into the SVG transform
    # anim must carry routeDays, else every arrow silently falls back to day 0.
    assert "routeDays: info.routeDays" in html

    # Constant nominal pace with capped stop pauses (no timestamp-driven speedups).
    assert "NOMINAL_KMH" in html
    assert "MAX_PAUSE_S" in html

    # Legend explains what the numbered pin and the clusters mean.
    assert "numbered pin" in html
    assert "Cluster" in html


def test_build_html_photos_open_in_a_lightbox(tmp_path):
    """Photos must open in-page, not in a new tab, and the lightbox must be
    able to page through every photo at a location."""
    summary = _summary(day_indexes=[0])
    html = build_html(summary, tmp_path / "trip").read_text(encoding="utf-8")

    # The lightbox lives in the page.
    assert "lightbox" in html
    assert "openLightbox" in html
    assert "closeLightbox" in html
    assert "stepPhoto" in html

    # Photos open in-page. Videos deliberately keep an external link, since a
    # video file can't be embedded in the report.
    strip_src = html.split("function assetStrip")[1].split("var lbEl")[0]
    assert 'class="photo-open"' in strip_src
    assert '<a class="vid"' in strip_src
    assert '<a href="' not in strip_src  # images are buttons, never navigation

    # A location with more photos than the strip limit gets a "+N" opener.
    assert "photo-more" in html
    # The lightbox browses ONE trip-ordered gallery, so swiping continues into
    # neighbouring points instead of stopping at the end of a location.
    assert "allPhotos" in html
    assert "photoIndexOf" in html
    assert "buildGallery" in html
    # Keyboard, swipe, and wrap-around navigation.
    assert "ArrowLeft" in html and "ArrowRight" in html
    assert "% allPhotos.length" in html


def test_build_html_supports_waypoint_names(tmp_path):
    """Waypoints show the road/label instead of a bare "Waypoint"."""
    summary = _summary(day_indexes=[0])
    summary.waypoints[0].name = "Austurvegur"
    summary.waypoints[1].road = "Route 1"
    html = build_html(summary, tmp_path / "trip").read_text(encoding="utf-8")

    data = json.loads(
        (tmp_path / "trip" / "summary.json").read_text(encoding="utf-8")
    )
    assert data["waypoints"][0]["name"] == "Austurvegur"
    assert data["waypoints"][1]["road"] == "Route 1"
    # Mid-drive the road wins; for an off-road spot the landmark does.
    assert "function wpName" in html
    assert "wp.road || wp.name" in html
    assert "wp.name || wp.road" in html


def test_build_html_supports_ios_safari(tmp_path):
    summary = _summary(day_indexes=[0])
    html = build_html(summary, tmp_path / "trip").read_text(encoding="utf-8")

    # viewport-fit + env() keep content clear of the notch / home indicator.
    assert "viewport-fit=cover" in html
    assert "env(safe-area-inset-top)" in html
    assert "env(safe-area-inset-bottom)" in html
    # dvh tracks Safari's collapsing toolbar.
    assert "100dvh" in html
    assert "-webkit-text-size-adjust" in html
    # Native share sheet beats clipboard on iOS (no secure context on http://).
    assert "navigator.share" in html
    assert "AbortError" in html

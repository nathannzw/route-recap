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

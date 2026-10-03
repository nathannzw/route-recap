"""Unit tests for route_recap.core.clustering."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from route_recap.core.clustering import (
    assign_days,
    deduplicate,
    detect_stops,
    filter_outliers,
    haversine_m,
)
from route_recap.core.models import RouteSegment, Waypoint

T0 = datetime(2026, 8, 14, 9, 0, 0)


def wp(lat, lon, t, media_count=1, files=None):
    return Waypoint(
        latitude=lat,
        longitude=lon,
        timestamp=t,
        media_count=media_count,
        source_files=files or [],
    )


def test_haversine_known_distances():
    # 1 degree of latitude ≈ 111.19 km at the equator
    assert haversine_m(0.0, 0.0, 1.0, 0.0) == pytest.approx(111_194.9, rel=1e-3)
    assert haversine_m(63.4, -19.0, 63.4, -19.0) == 0.0


def test_dedup_burst_collapses():
    pts = [
        wp(63.4190, -19.0060, T0, files=[Path("a.jpg")]),
        wp(63.4191, -19.0061, T0 + timedelta(minutes=1), files=[Path("b.jpg")]),
        wp(63.4192, -19.0062, T0 + timedelta(minutes=4), files=[Path("c.jpg")]),
    ]
    out = deduplicate(pts)
    assert len(out) == 1
    assert out[0].media_count == 3
    assert len(out[0].source_files) == 3
    assert out[0].timestamp == T0  # anchor is the first photo


def test_dedup_far_away_separates():
    pts = [
        wp(63.0, -19.0, T0),
        wp(64.0, -19.0, T0 + timedelta(minutes=2)),  # ~111 km away
    ]
    assert len(deduplicate(pts)) == 2


def test_dedup_window_exceeded_separates():
    pts = [
        wp(63.4190, -19.0060, T0),
        wp(63.4191, -19.0061, T0 + timedelta(minutes=6)),  # same spot, > 5 min
    ]
    assert len(deduplicate(pts)) == 2


def test_dedup_untimed_kept_separately():
    pts = [
        wp(63.4190, -19.0060, T0),
        wp(63.4191, -19.0061, None),
    ]
    out = deduplicate(pts)
    assert len(out) == 2


def test_detect_stops_basic():
    pts = [
        wp(63.4190, -19.0060, T0, files=[Path("a.jpg"), Path("b.jpg")]),
        wp(63.4192, -19.0065, T0 + timedelta(minutes=45)),  # ~35 m away
        wp(63.5000, -19.5000, T0 + timedelta(hours=1, minutes=30)),
    ]
    out, stops = detect_stops(pts)
    assert len(stops) == 1
    assert stops[0].duration_s == 45 * 60
    assert stops[0].source_files == [Path("a.jpg"), Path("b.jpg")]
    assert out[0].is_stop
    assert out[0].stop_index == 0


def test_detect_stops_short_gap_is_not_a_stop():
    pts = [
        wp(63.4190, -19.0060, T0),
        wp(63.4192, -19.0065, T0 + timedelta(minutes=10)),
    ]
    _, stops = detect_stops(pts)
    assert stops == []


def test_detect_stops_moved_far_within_threshold_is_not_a_stop():
    pts = [
        wp(63.4190, -19.0060, T0),
        wp(64.4190, -19.0060, T0 + timedelta(hours=1)),  # 111 km in 1 h
    ]
    _, stops = detect_stops(pts)
    assert stops == []


def test_detect_stops_overnight_bypasses_distance():
    pts = [
        wp(63.4190, -19.0060, T0),
        wp(64.5000, -18.0000, T0 + timedelta(hours=7)),  # ~130 km away
    ]
    out, stops = detect_stops(pts)
    assert len(stops) == 1
    assert stops[0].duration_s == 7 * 3600
    assert out[0].is_stop


# ------------------------------------------------------------- outliers


def test_filter_outliers_removes_foreign_photos():
    # Two photos in London, a flight, then the actual Iceland trip.
    pts = [
        wp(51.5074, -0.1278, T0),
        wp(51.5075, -0.1277, T0 + timedelta(minutes=10)),
        wp(63.6158, -19.9888, T0 + timedelta(hours=4)),          # ~1900 km
        wp(63.5600, -19.6000, T0 + timedelta(hours=4, minutes=15)),
        wp(63.5100, -19.4500, T0 + timedelta(hours=4, minutes=30)),
    ]
    kept, excluded = filter_outliers(pts)
    assert len(kept) == 3
    assert len(excluded) == 2
    assert excluded[0].latitude == pytest.approx(51.5074)


def test_filter_outliers_keeps_largest_segment():
    # Pre-trip home photo followed by a flight and a longer trip.
    pts = [
        wp(51.5074, -0.1278, T0),
        wp(63.6158, -19.9888, T0 + timedelta(hours=4)),
        wp(63.5600, -19.6000, T0 + timedelta(hours=4, minutes=15)),
        wp(63.5100, -19.4500, T0 + timedelta(hours=4, minutes=30)),
        wp(63.4800, -19.3300, T0 + timedelta(hours=4, minutes=45)),
    ]
    kept, excluded = filter_outliers(pts)
    assert len(kept) == 4  # the Iceland segment
    assert len(excluded) == 1  # the London photo


def test_filter_outliers_overnight_stop_is_not_a_jump():
    # 8 h gap but essentially no movement — a stop, not a flight.
    pts = [
        wp(63.4186, -19.0060, T0),
        wp(63.4190, -19.0070, T0 + timedelta(hours=8)),
    ]
    kept, excluded = filter_outliers(pts)
    assert len(kept) == 2
    assert excluded == []


def test_filter_outliers_gap_below_threshold_kept():
    # Impossible jump but shorter than the minimum gap (e.g. clock noise).
    pts = [
        wp(51.5074, -0.1278, T0),
        wp(63.6158, -19.9888, T0 + timedelta(minutes=20)),
    ]
    kept, excluded = filter_outliers(pts)
    assert len(kept) == 2
    assert excluded == []


def test_filter_outliers_untimed_waypoints_kept():
    pts = [
        wp(51.5074, -0.1278, T0),
        wp(63.6158, -19.9888, T0 + timedelta(hours=4)),
        wp(63.5600, -19.6000, T0 + timedelta(hours=4, minutes=15)),
        wp(63.0000, -18.0000, None),  # untimed — cannot classify
    ]
    kept, excluded = filter_outliers(pts)
    assert len(kept) == 3  # Iceland segment + untimed point
    assert len(excluded) == 1


def test_filter_outliers_no_jumps_keeps_everything():
    pts = [
        wp(63.6158, -19.9888, T0),
        wp(63.5600, -19.6000, T0 + timedelta(hours=1)),
        wp(63.5100, -19.4500, T0 + timedelta(hours=2)),
    ]
    kept, excluded = filter_outliers(pts)
    assert len(kept) == 3
    assert excluded == []


# ------------------------------------------------------------- day assignment


def seg(start, end):
    return RouteSegment(start_index=start, end_index=end)


def test_assign_days_groups_by_calendar_day():
    # Each segment starts on a distinct calendar day.
    wps = [
        wp(63.4, -19.0, T0),                              # day 0
        wp(63.5, -19.1, T0 + timedelta(days=1, hours=1)), # day 1
        wp(63.6, -19.2, T0 + timedelta(days=2, hours=1)), # day 2
        wp(63.7, -19.3, T0 + timedelta(days=3, hours=1)), # day 3
    ]
    out = assign_days(wps, [seg(0, 1), seg(1, 2), seg(2, 3)])
    assert [s.day_index for s in out] == [0, 1, 2]


def test_assign_days_segment_belongs_to_start_day():
    # A segment crossing midnight keeps the day it starts on.
    wps = [
        wp(63.4, -19.0, T0),
        wp(63.5, -19.1, T0 + timedelta(hours=2)),
        wp(63.6, -19.2, T0 + timedelta(days=1, hours=2)),
    ]
    out = assign_days(wps, [seg(0, 1), seg(1, 2)])
    assert [s.day_index for s in out] == [0, 0]


def test_assign_days_uses_end_waypoint_when_start_untimed():
    wps = [
        wp(63.4, -19.0, T0),
        wp(63.5, -19.1, None),  # untimed start — falls back to end waypoint
        wp(63.6, -19.2, T0 + timedelta(days=1)),
    ]
    out = assign_days(wps, [seg(0, 1), seg(1, 2)])
    assert [s.day_index for s in out] == [0, 1]


def test_assign_days_untimed_segment_inherits_previous_day():
    wps = [
        wp(63.4, -19.0, T0),
        wp(63.5, -19.1, None),
        wp(63.6, -19.2, None),
        wp(63.7, -19.3, T0 + timedelta(days=1)),
    ]
    out = assign_days(wps, [seg(0, 1), seg(1, 2), seg(2, 3)])
    assert [s.day_index for s in out] == [0, 0, 1]


def test_assign_days_no_timestamps_defaults_to_day_zero():
    wps = [wp(63.4, -19.0, None), wp(63.5, -19.1, None)]
    out = assign_days(wps, [seg(0, 1)])
    assert [s.day_index for s in out] == [0]


def test_assign_days_does_not_mutate_input_segments():
    wps = [
        wp(63.4, -19.0, T0),
        wp(63.5, -19.1, T0 + timedelta(days=1)),
    ]
    original = [seg(0, 1)]
    out = assign_days(wps, original)
    assert out[0].day_index == 0  # start waypoint is day 0
    assert original[0].day_index is None  # input untouched


def test_assign_days_empty_segments():
    assert assign_days([], []) == []

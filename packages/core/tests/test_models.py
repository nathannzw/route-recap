"""Unit tests for route_recap.core.models."""

from pathlib import Path

import pytest

from route_recap.core.models import (
    KM_PER_MILE,
    DistanceUnit,
    MediaMetadata,
    MediaType,
    TripConfig,
    TripSummary,
    Waypoint,
)


def test_waypoint_coordinate_ranges():
    with pytest.raises(ValueError):
        Waypoint(latitude=91.0, longitude=0.0)
    with pytest.raises(ValueError):
        Waypoint(latitude=0.0, longitude=-181.0)
    Waypoint(latitude=63.4, longitude=-19.0)  # valid


def test_media_metadata_has_gps():
    tagged = MediaMetadata(
        path=Path("a.jpg"), media_type=MediaType.IMAGE, latitude=1.0, longitude=2.0
    )
    assert tagged.has_gps
    untagged = MediaMetadata(path=Path("b.jpg"), media_type=MediaType.IMAGE)
    assert not untagged.has_gps


def test_trip_summary_distance_conversion():
    summary = TripSummary(trip_name="t", distance_km=KM_PER_MILE * 10)
    assert summary.distance_mi == pytest.approx(10.0, abs=1e-6)
    assert summary.display_distance() == "16.1 km"  # default unit is km
    summary.unit = DistanceUnit.MI
    assert summary.display_distance() == "10.0 mi"


def test_trip_config_defaults():
    cfg = TripConfig(trip_name="Trip")
    assert cfg.min_stop_minutes == 30
    assert cfg.dedup_radius_m == 50.0
    assert cfg.dedup_window_minutes == 5
    assert cfg.distance_unit is DistanceUnit.KM
    with pytest.raises(ValueError):
        TripConfig(trip_name="x", min_stop_minutes=1)

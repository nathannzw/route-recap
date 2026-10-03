"""Unit tests for the route-recap CLI (LAN serving, waypoint naming)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import route_recap.cli.main as main_module
from route_recap.cli.main import (
    _export_single_file,
    _lan_ipv4_addresses,
    _plan_waypoint_queries,
    app,
)
from route_recap.core.models import TripConfig, TripSummary, Waypoint


def test_lan_addresses_exclude_loopback():
    """A phone cannot reach 127.0.0.1, so loopback must never be advertised."""
    addrs = _lan_ipv4_addresses()
    assert isinstance(addrs, list)
    assert all(not a.startswith("127.") for a in addrs)
    assert addrs == sorted(addrs)


def test_serve_exposes_host_option():
    """`--host 0.0.0.0` is what makes the report reachable from an iPhone."""
    result = CliRunner().invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.stdout
    assert "0.0.0.0" in result.stdout


def test_export_single_file_forwards_options(tmp_path):
    """Guards the CLI→builder option mapping.

    The builder's parameter is ``embed`` while the CLI flag is
    ``--embed-photos``; when those names were wired straight through, the flag
    raised ``TypeError`` on first use. Going via this helper keeps the mapping
    in one place and this test fails if it drifts again.
    """
    path, warnings = _export_single_file(
        TripSummary(trip_name="Export Test"),
        tmp_path / "out",
        carto_key=None,
        assets={},
        embed_photos=False,
        max_embed_mb=1.0,
        inline_libraries=False,  # avoids any network access in tests
        photo_size=120,
        photo_quality=60,
    )

    assert path == tmp_path / "out" / "journey.html"
    assert path.is_file()
    assert warnings == []


def test_export_single_file_top_level_flag_parses():
    """`--single-file` must be a real option on the default command."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--single-file" in result.stdout
    assert "--max-embed-mb" in result.stdout


# ------------------------------------------------------- waypoint naming


def _wp(lat, lon, name=None, road=None):
    return Waypoint(latitude=lat, longitude=lon, name=name, road=road)


def test_plan_waypoint_queries_reuses_one_request_per_road():
    """Waypoints are dense, so nearby ones must share a single geocode call."""
    waypoints = [
        _wp(63.4000, -19.0000),  # new group
        _wp(63.4005, -19.0005),  # ~70 m away -> same group
        _wp(63.4010, -19.0010),  # ~70 m from the previous -> same group
        _wp(63.5000, -19.5000),  # ~13 km away -> new group
    ]
    groups = _plan_waypoint_queries(waypoints)
    assert [probe for probe, _ in groups] == [0, 3]
    assert [members for _, members in groups] == [[0, 1, 2], [3]]


def test_plan_waypoint_queries_skips_already_labelled():
    waypoints = [
        _wp(63.4000, -19.0000, name="Vík"),
        _wp(63.5000, -19.5000, road="Route 1"),
        _wp(63.9000, -19.9000),
    ]
    groups = _plan_waypoint_queries(waypoints)
    assert [probe for probe, _ in groups] == [2]


def test_geocode_waypoints_labels_the_whole_group(monkeypatch):
    calls = []

    def fake_detail(lat, lon, **kwargs):
        calls.append((lat, lon))
        return {"name": "Vík", "address": "Vík, Iceland", "road": "Austurvegur"}

    monkeypatch.setattr(main_module, "reverse_geocode_detail", fake_detail)
    waypoints = [
        _wp(63.4000, -19.0000),
        _wp(63.4005, -19.0005),  # shares the group -> no extra request
    ]

    named = main_module._geocode_waypoints(waypoints, use_nominatim=False)

    assert named == 2
    assert len(calls) == 1  # one request covered both waypoints
    for wp in waypoints:
        assert wp.name == "Vík"
        assert wp.road == "Austurvegur"
    assert main_module._label_for(waypoints[0]) == "Austurvegur"


def test_geocode_waypoints_caps_nominatim(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "reverse_geocode_detail",
        lambda lat, lon, **kw: {"name": "X", "address": "X", "road": "R"},
    )
    monkeypatch.setattr(main_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main_module, "_NOMINATIM_WAYPOINT_CAP", 2)
    # Waypoints far apart so each one is its own group.
    waypoints = [_wp(63.0 + i, -19.0) for i in range(5)]

    named = main_module._geocode_waypoints(waypoints, use_nominatim=True)

    assert named == 2  # capped, not all five


def test_config_disables_waypoint_naming():
    cfg = TripConfig(trip_name="t", geocode_waypoints=False)
    assert cfg.geocode_waypoints is False
    assert TripConfig(trip_name="t").geocode_waypoints is True

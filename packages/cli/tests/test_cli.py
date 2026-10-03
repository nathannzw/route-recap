"""Unit tests for the route-recap CLI (LAN serving helpers)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from route_recap.cli.main import _export_single_file, _lan_ipv4_addresses, app
from route_recap.core.models import TripSummary


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

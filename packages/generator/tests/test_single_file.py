"""Unit tests for the single-file (shareable) report builder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import polyline
import pytest

from route_recap.core.models import RouteSegment, Stop, TripSummary, Waypoint
from route_recap.generator import single_file
from route_recap.generator.html_builder import _render_html
from route_recap.generator.single_file import (
    _inline_safe,
    _ordered_sources,
    build_single_file,
    embed_photos,
    load_vendor_assets,
)

T0 = datetime(2026, 8, 14, 9, 0, 0)


def _jpeg(path: Path, color=(120, 140, 160), size=(400, 300)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "jpeg")


def _summary(tmp_path: Path) -> tuple[TripSummary, dict[str, dict]]:
    """A trip with one stop photo, one off-road photo and one plain photo."""
    stop_photo = tmp_path / "stop.jpg"
    offroad_photo = tmp_path / "offroad.jpg"
    plain_photo = tmp_path / "plain.jpg"
    video = tmp_path / "clip.mov"
    for p in (stop_photo, offroad_photo, plain_photo):
        _jpeg(p)
    video.write_bytes(b"not a real movie")

    waypoints = [
        Waypoint(latitude=63.4, longitude=-19.0, timestamp=T0, source_files=[stop_photo]),
        Waypoint(
            latitude=63.5,
            longitude=-19.1,
            timestamp=T0 + timedelta(hours=1),
            source_files=[offroad_photo],
            off_road=True,
        ),
        Waypoint(
            latitude=63.6,
            longitude=-19.2,
            timestamp=T0 + timedelta(hours=2),
            source_files=[plain_photo],
        ),
    ]
    coords = [(63.4, -19.0), (63.5, -19.1), (63.6, -19.2)]
    summary = TripSummary(
        trip_name="Share Test",
        started_at=T0,
        ended_at=T0 + timedelta(hours=2),
        waypoints=waypoints,
        stops=[
            Stop(
                latitude=63.4,
                longitude=-19.0,
                arrived_at=T0,
                media_count=1,
                source_files=[stop_photo],
            )
        ],
        segments=[
            RouteSegment(
                start_index=0,
                end_index=1,
                encoded_polyline=polyline.encode(coords[:2], precision=5),
                day_index=0,
            ),
            RouteSegment(
                start_index=1,
                end_index=2,
                encoded_polyline=polyline.encode(coords[1:], precision=5),
                day_index=1,
            ),
        ],
    )
    assets = {
        str(stop_photo): {"url": "media/0001.jpg", "thumb": "thumbs/0001.jpg", "kind": "image"},
        str(offroad_photo): {"url": "media/0002.jpg", "thumb": "thumbs/0002.jpg", "kind": "image"},
        str(plain_photo): {"url": "media/0003.jpg", "thumb": "thumbs/0003.jpg", "kind": "image"},
        str(video): {"url": "media/0004.mov", "thumb": None, "kind": "video"},
    }
    return summary, assets


# ------------------------------------------------------------- source ordering


def test_ordered_sources_prioritises_stops_then_offroad(tmp_path):
    summary, _ = _summary(tmp_path)
    ordered = _ordered_sources(summary)
    assert ordered[0] == str(tmp_path / "stop.jpg")       # stop photo first
    assert ordered[1] == str(tmp_path / "offroad.jpg")    # then off-road
    assert ordered[2] == str(tmp_path / "plain.jpg")      # then the rest
    assert len(ordered) == 3                               # de-duplicated


# ------------------------------------------------------------------- embedding


def test_embed_photos_uses_data_uris_without_duplicating_payload(tmp_path):
    summary, assets = _summary(tmp_path)
    embedded, count, truncated = embed_photos(
        summary, tmp_path, assets, max_bytes=5 * 1024 * 1024, size=120, quality=60
    )

    assert count == 3
    assert not truncated
    for entry in embedded.values():
        assert entry["thumb"].startswith("data:image/jpeg;base64,")
        # Regression: pointing url and thumb at the same data URI stored the
        # payload twice and doubled the file size.
        assert entry["url"] == ""


def test_embed_photos_skips_videos(tmp_path):
    summary, assets = _summary(tmp_path)
    embedded, _, _ = embed_photos(
        summary, tmp_path, assets, max_bytes=5 * 1024 * 1024, size=120, quality=60
    )
    assert str(tmp_path / "clip.mov") not in embedded


def test_embed_photos_respects_budget(tmp_path):
    summary, assets = _summary(tmp_path)
    # A budget smaller than a single image embeds nothing but flags truncation.
    embedded, count, truncated = embed_photos(
        summary, tmp_path, assets, max_bytes=100, size=120, quality=60
    )
    assert count == 0
    assert truncated
    assert embedded == {}


# -------------------------------------------------------------------- inlining


def test_inline_safe_rejects_tag_terminating_payload():
    assert _inline_safe("var x = 1;")
    assert not _inline_safe("document.write('</script>')")
    assert not _inline_safe("</style>")


def test_load_vendor_assets_warns_and_degrades_when_offline(tmp_path, monkeypatch):
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("no network")

    monkeypatch.setattr(single_file.httpx, "get", boom)
    inline, warnings = load_vendor_assets(tmp_path / "cache")

    assert inline == {}          # template keeps its CDN links
    assert warnings               # and the user is told why


def test_load_vendor_assets_inlines_when_available(tmp_path, monkeypatch):
    class _Resp:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        single_file.httpx, "get", lambda url, **kw: _Resp(f"/* {url} */ body {{}}")
    )
    inline, warnings = load_vendor_assets(tmp_path / "cache")

    assert warnings == []
    assert "vendor_css" in inline and "vendor_js" in inline
    assert "leaflet.css" in inline["vendor_css"]
    # Second call is served from the cache written by the first.
    again, _ = load_vendor_assets(tmp_path / "cache")
    assert again == inline


# --------------------------------------------------------------- the template


def test_render_html_inlines_libraries_when_given(tmp_path):
    summary, _ = _summary(tmp_path)
    html = _render_html(summary, inline={"vendor_css": "/*css*/", "vendor_js": "/*js*/"})

    assert "unpkg.com" not in html
    assert "<style>/*css*/</style>" in html
    assert "<script>/*js*/</script>" in html


def test_render_html_falls_back_to_cdn(tmp_path):
    summary, _ = _summary(tmp_path)
    html = _render_html(summary)
    assert "unpkg.com/leaflet@1.9.4" in html


# ------------------------------------------------------------- build_single_file


def test_build_single_file_is_self_contained(tmp_path, monkeypatch):
    summary, assets = _summary(tmp_path)

    class _Resp:
        text = "window.L = {};"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(single_file.httpx, "get", lambda url, **kw: _Resp())
    path, warnings = build_single_file(
        summary,
        tmp_path / "out",
        assets=assets,
        max_embed_mb=5.0,
        thumb_size=120,
        thumb_quality=60,
        cache_dir=tmp_path / "cache",
    )

    assert path.name == "journey.html"
    assert path.is_file()
    html = path.read_text(encoding="utf-8")
    assert warnings == []

    # No external library dependency, photos inlined, videos dropped.
    assert "unpkg.com" not in html
    assert html.count("data:image/jpeg;base64,") == 3
    assert "media/0004.mov" not in html
    # Single-file mode writes only the HTML — no summary.json sidecar.
    assert not (path.parent / "summary.json").exists()


def test_build_single_file_can_skip_photos(tmp_path):
    summary, assets = _summary(tmp_path)
    path, _ = build_single_file(
        summary,
        tmp_path / "out",
        assets=assets,
        embed=False,
        inline_libraries=False,
    )
    html = path.read_text(encoding="utf-8")
    assert "data:image/jpeg;base64," not in html
    assert "unpkg.com" in html  # library inlining was skipped too

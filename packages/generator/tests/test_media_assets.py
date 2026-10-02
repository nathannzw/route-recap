"""Unit tests for route_recap.generator.media_assets and asset embedding."""

from pathlib import Path

import pytest
from PIL import Image


def _make_jpeg(path: Path, color: tuple[int, int, int] = (120, 140, 160)) -> None:
    Image.new("RGB", (64, 48), color).save(path, "jpeg")


def test_stage_media_images(tmp_path):
    from route_recap.generator import stage_media

    src_dir = tmp_path / "photos"
    src_dir.mkdir()
    a = src_dir / "a.jpg"
    b = src_dir / "b.jpg"
    _make_jpeg(a)
    _make_jpeg(b, color=(200, 60, 60))
    out = tmp_path / "report"

    assets = stage_media([a, b], out)

    assert set(assets) == {str(a), str(b)}
    asset_a = assets[str(a)]
    assert asset_a["kind"] == "image"
    assert asset_a["url"] == "media/0001.jpg"
    assert asset_a["thumb"] == "thumbs/0001.jpg"
    assert (out / "media" / "0001.jpg").exists()
    assert (out / "thumbs" / "0001.jpg").exists()
    # hardlink or copy — staged bytes must match the source
    assert (out / "media" / "0001.jpg").read_bytes() == a.read_bytes()
    # thumbnail must be a valid image
    with Image.open(out / "thumbs" / "0001.jpg") as thumb:
        assert thumb.size[0] <= 320


def test_stage_media_deduplicates_sources(tmp_path):
    from route_recap.generator import stage_media

    a = tmp_path / "a.jpg"
    _make_jpeg(a)
    out = tmp_path / "report"
    assets = stage_media([a, a], out)
    assert len(assets) == 1
    assert not (out / "media" / "0002.jpg").exists()


def test_stage_media_video_without_ffmpeg(tmp_path, monkeypatch):
    import route_recap.generator.media_assets as ma
    from route_recap.generator import stage_media

    monkeypatch.setattr(ma, "_FFMPEG", None)
    v = tmp_path / "clip.mov"
    v.write_bytes(b"fake video bytes")
    out = tmp_path / "report"

    assets = stage_media([v], out)

    asset = assets[str(v)]
    assert asset["kind"] == "video"
    assert asset["url"] == "media/0001.mov"
    assert asset["thumb"] is None
    assert (out / "media" / "0001.mov").exists()


def test_stage_media_missing_source_is_skipped(tmp_path):
    from route_recap.generator import stage_media

    missing = tmp_path / "gone.jpg"
    out = tmp_path / "report"
    assets = stage_media([missing], out)
    assert assets == {}


def test_html_embeds_assets(tmp_path):
    from route_recap.core.models import TripSummary
    from route_recap.generator import build_html

    summary = TripSummary(trip_name="t")
    out = tmp_path / "report"
    assets = {
        "/abs/a.jpg": {
            "url": "media/0001.jpg",
            "thumb": "thumbs/0001.jpg",
            "kind": "image",
        }
    }
    path = build_html(summary, out, assets=assets)
    html = path.read_text(encoding="utf-8")
    assert "media/0001.jpg" in html
    assert "thumbs/0001.jpg" in html


def test_html_without_assets_still_renders(tmp_path):
    from route_recap.core.models import TripSummary
    from route_recap.generator import build_html

    path = build_html(TripSummary(trip_name="t"), tmp_path / "report")
    assert '"assets": {}' in path.read_text(encoding="utf-8")

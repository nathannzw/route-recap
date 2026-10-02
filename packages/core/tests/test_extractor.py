"""Unit tests for route_recap.core.extractor (Pillow path + parsers)."""

from pathlib import Path

import pytest

from route_recap.core.extractor import MediaExtractor, parse_gps, parse_timestamp


def _make_jpeg(path: Path, *, with_gps: bool = True) -> None:
    """Write a small JPEG with EXIF GPS + DateTimeOriginal using Pillow only."""
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif[0x010F] = "Apple"          # Make
    exif[0x0110] = "iPhone 16 Pro"  # Model
    exif[0x9003] = "2026:08:14 08:00:00"  # DateTimeOriginal
    if with_gps:
        exif[0x8825] = {
            1: "N",
            2: (IFDRational(63, 1), IFDRational(36, 1), IFDRational(5688, 100)),
            3: "W",
            4: (IFDRational(19, 1), IFDRational(59, 1), IFDRational(1968, 100)),
            5: b"\x00",
            6: IFDRational(4000, 100),  # 40.0 m
        }
    Image.new("RGB", (32, 32), (120, 140, 160)).save(path, "jpeg", exif=exif)


def test_parse_timestamp_formats():
    assert parse_timestamp("2026:08:14 08:00:00").strftime("%H:%M") == "08:00"
    assert parse_timestamp("2026-08-14T08:00:00Z").hour == 8
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


def test_parse_gps_with_refs():
    lat, lon = parse_gps("63.6158", "N", "19.9888", "W")
    assert lat == pytest.approx(63.6158)
    assert lon == pytest.approx(-19.9888)
    # DMS string form
    lat, lon = parse_gps("63 deg 36' 56.88\"", "S", "19 deg 59' 19.68\"", "E")
    assert lat == pytest.approx(-63.6158)
    assert lon == pytest.approx(19.9888)


def test_extract_pillow_path(tmp_path):
    tagged = tmp_path / "tagged.jpg"
    untagged = tmp_path / "untagged.jpg"
    _make_jpeg(tagged, with_gps=True)
    _make_jpeg(untagged, with_gps=False)

    extractor = MediaExtractor()
    result = extractor.extract_files([tagged, untagged])

    assert result.total_files == 2
    assert result.with_gps == 1
    assert result.skipped == 0

    meta = next(m for m in result.items if m.path == tagged)
    assert meta.has_gps
    assert meta.latitude == pytest.approx(63.6158, abs=1e-3)
    assert meta.longitude == pytest.approx(-19.9888, abs=1e-3)
    assert meta.datetime_original is not None
    assert meta.datetime_original.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-14 08:00:00"
    assert meta.model == "iPhone 16 Pro"

    plain = next(m for m in result.items if m.path == untagged)
    assert not plain.has_gps


def test_extract_unsupported_extension(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("not media")
    extractor = MediaExtractor()
    assert extractor.extract_files([bogus]).total_files == 0


def test_list_media_filters_extensions(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.HEIC").write_bytes(b"x")
    (tmp_path / "c.mov").write_bytes(b"x")
    (tmp_path / "d.txt").write_bytes(b"x")
    extractor = MediaExtractor()
    found = {p.name for p in extractor.list_media(tmp_path, recursive=False)}
    assert found == {"a.jpg", "b.HEIC", "c.mov"}

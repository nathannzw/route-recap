"""Unit tests for route_recap.core.extractor (Pillow path + parsers)."""

from pathlib import Path

import pytest

from route_recap.core.extractor import MediaExtractor, parse_gps, parse_timestamp
from route_recap.core.models import MediaType


class _FakeExifTool:
    """Stand-in for pyexiftool's helper, recording how it is called.

    ``get_tags`` mirrors the real signature ``(files, tags, params)``.
    """

    def __init__(self, payload: dict):
        self.payload = payload
        self.kwargs: dict | None = None

    def get_tags(self, files=None, tags=None, params=None):  # noqa: ANN001
        self.kwargs = {"files": files, "tags": tags}
        return [self.payload]

    def terminate(self) -> None:
        pass


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


# --------------------------------------------------------------- exiftool path

#: Realistic pyexiftool output: keys are group-prefixed, GPS comes back as a
#: Composite value (no N/S/E/W ref tags) and QuickTime holds the timestamp.
_MOV_TAGS = {
    "SourceFile": "clip.mov",
    "File:FileType": "MOV",
    "Composite:GPSLatitude": 63.9954,
    "Composite:GPSLongitude": -22.6187,
    "Composite:GPSAltitude": 45.607,
    "QuickTime:CreateDate": "2026:09:12 07:57:46",
    "QuickTime:Make": "Apple",
    "QuickTime:Model": "iPhone 15",
}


def test_read_exiftool_passes_file_as_first_arg(tmp_path):
    """Regression: pyexiftool's signature is get_tags(files, tags).

    The call was previously reversed (tags first), which made pyexiftool treat
    the file path as an invalid tag name and raise — so ExifTool never worked.
    Images survived via the Pillow fallback; videos were dropped entirely.
    """
    fake = _FakeExifTool(_MOV_TAGS)
    tags = MediaExtractor._read_exiftool(fake, Path("clip.mov"))

    assert fake.kwargs is not None
    assert "GPSLatitude" in fake.kwargs["tags"]
    assert "clip.mov" in str(fake.kwargs["files"])
    # Group prefixes are stripped so lookups by bare name work.
    assert tags["GPSLatitude"] == 63.9954
    assert tags["CreateDate"] == "2026:09:12 07:57:46"


def test_extract_video_gets_gps_from_exiftool(tmp_path, monkeypatch):
    """Videos have no Pillow fallback, so ExifTool is their only GPS source."""
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"not a real movie")
    fake = _FakeExifTool(_MOV_TAGS)
    monkeypatch.setattr(MediaExtractor, "_open_exiftool", lambda self: fake)

    result = MediaExtractor().extract_files([clip])

    assert result.total_files == 1
    assert result.skipped == 0
    meta = result.items[0]
    assert meta.media_type is MediaType.VIDEO
    assert meta.has_gps
    assert meta.latitude == pytest.approx(63.9954)
    assert meta.longitude == pytest.approx(-22.6187)
    assert meta.datetime_original is not None
    assert meta.datetime_original.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-12 07:57:46"
    assert meta.model == "iPhone 15"

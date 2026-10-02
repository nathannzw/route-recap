"""EXIF/GPS extraction for iPhone travel media (HEIC/JPEG/MOV/MP4).

Primary path: ExifTool (via pyexiftool) — uniform support for images and
videos, including GPS and capture timestamps.
Fallback path: Pillow + pillow-heif for images only.

Files with no GPS data are returned as records with ``None`` coordinates;
the pipeline skips them gracefully instead of crashing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from .models import MediaMetadata, MediaType

if TYPE_CHECKING:
    from exiftool import ExifToolHelper

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mov", ".mp4"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

_EXIFTOOL_TAGS = [
    "GPSLatitude",
    "GPSLatitudeRef",
    "GPSLongitude",
    "GPSLongitudeRef",
    "GPSAltitude",
    "DateTimeOriginal",
    "CreateDate",
    "FileType",
    "Make",
    "Model",
    "FileSize",
]

# Pillow EXIF tag ids (fallback path).
_PIL_GPS_IFD = 0x8825
_PIL_EXIF_IFD = 0x8769  # ExifOffset — sub-IFD holding DateTimeOriginal
_PIL_MAKE = 0x010F
_PIL_MODEL = 0x0110
_PIL_DATETIME_ORIGINAL = 0x9003
_PIL_GPS_LAT_REF = 1
_PIL_GPS_LAT = 2
_PIL_GPS_LON_REF = 3
_PIL_GPS_LON = 4
_PIL_GPS_ALT = 6

_TIMESTAMP_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S%z",
    "%Y:%m:%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)

_DMS_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*deg\s+(\d+(?:\.\d+)?)\s*'?\s*"
    r"(\d+(?:\.\d+)?)\s*\"?\s*([NSEWnsew])?"
)
_DECIMAL_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_timestamp(value: object) -> datetime | None:
    """Parse common EXIF / QuickTime timestamp formats into a naive UTC datetime."""
    if value is None:
        return None
    s = str(value).strip().strip("\x00").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in _TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


def _dms_to_decimal(value: str) -> float | None:
    """Convert '63 deg 25' 10.20" N' style strings to decimal degrees."""
    m = _DMS_RE.fullmatch(value.strip())
    if not m:
        return None
    deg, minutes, seconds = float(m.group(1)), float(m.group(2)), float(m.group(3))
    val = abs(deg) + minutes / 60.0 + seconds / 3600.0
    if deg < 0:
        val = -val
    if m.group(4) and m.group(4).upper() in ("S", "W"):
        val = -abs(val)
    return val


def _coord(value: object, ref: object | None) -> float | None:
    """Combine an exiftool coordinate value and its N/S/E/W reference."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if _DECIMAL_RE.fullmatch(v):
        val = float(v)
    else:
        val = _dms_to_decimal(v)
        if val is None:
            return None
    r = str(ref).strip().upper() if ref else ""
    if v.upper().endswith(("S", "W")) or r in ("S", "W"):
        val = -abs(val)
    return val


def parse_gps(
    lat_raw: object,
    lat_ref: object | None,
    lon_raw: object,
    lon_ref: object | None,
) -> tuple[float | None, float | None]:
    return _coord(lat_raw, lat_ref), _coord(lon_raw, lon_ref)


@dataclass
class ExtractionResult:
    """Outcome of scanning a media directory."""

    items: list[MediaMetadata] = field(default_factory=list)
    total_files: int = 0
    with_gps: int = 0
    skipped: int = 0


class MediaExtractor:
    """Extracts GPS and timestamps from iPhone media files in a directory."""

    def __init__(self, exiftool_path: str | None = None) -> None:
        self._exiftool_path = exiftool_path or os.getenv("EXIFTOOL_PATH") or None
        self._exiftool_unavailable = False

    # ------------------------------------------------------------------ scan

    @staticmethod
    def list_media(
        directory: str | Path, *, recursive: bool = True
    ) -> list[Path]:
        """Return supported media files in a directory, sorted by path."""
        directory = Path(directory)
        pattern = "**/*" if recursive else "*"
        return sorted(
            p
            for p in directory.glob(pattern)
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    def extract_directory(
        self,
        directory: str | Path,
        *,
        recursive: bool = True,
        on_file: Callable[[Path], None] | None = None,
    ) -> ExtractionResult:
        return self.extract_files(
            self.list_media(directory, recursive=recursive), on_file=on_file
        )

    def extract_files(
        self,
        files: Sequence[Path],
        *,
        on_file: Callable[[Path], None] | None = None,
    ) -> ExtractionResult:
        files = [
            p for p in files if p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        result = ExtractionResult(total_files=len(files))
        et = self._open_exiftool()
        try:
            for path in files:
                try:
                    meta = self.extract_file(path, et)
                except Exception:
                    logger.exception("unexpected error reading metadata from %s", path)
                    result.skipped += 1
                else:
                    if meta is None:
                        result.skipped += 1
                    else:
                        result.items.append(meta)
                        if meta.has_gps:
                            result.with_gps += 1
                finally:
                    if on_file is not None:
                        on_file(path)
        finally:
            if et is not None:
                try:
                    et.terminate()
                except Exception:
                    logger.debug("error terminating exiftool process", exc_info=True)
        return result

    def extract_file(
        self, path: str | Path, et: "ExifToolHelper | None" = None
    ) -> MediaMetadata | None:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return None
        media_type = MediaType.VIDEO if suffix in VIDEO_EXTENSIONS else MediaType.IMAGE

        tags: dict[str, object] = {}
        if et is not None:
            try:
                tags = self._read_exiftool(et, path)
            except Exception:
                logger.debug("exiftool read failed for %s; trying Pillow", path)
        if not tags and media_type is MediaType.IMAGE:
            tags = self._read_pillow(path)
        if not tags:
            logger.debug("no metadata for %s", path)
            return None

        latitude, longitude = parse_gps(
            tags.get("GPSLatitude"),
            tags.get("GPSLatitudeRef"),
            tags.get("GPSLongitude"),
            tags.get("GPSLongitudeRef"),
        )
        altitude = _to_float(tags.get("GPSAltitude"))
        timestamp = parse_timestamp(
            tags.get("DateTimeOriginal") or tags.get("CreateDate")
        )
        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = None

        return MediaMetadata(
            path=path,
            media_type=media_type,
            latitude=latitude,
            longitude=longitude,
            altitude_m=altitude,
            datetime_original=timestamp,
            make=_to_str(tags.get("Make")),
            model=_to_str(tags.get("Model")),
            file_size_bytes=file_size,
        )

    # -------------------------------------------------------------- exiftool

    def _open_exiftool(self) -> "ExifToolHelper | None":
        if self._exiftool_unavailable:
            return None
        try:
            from exiftool import ExifToolHelper

            kwargs = {"executable": self._exiftool_path} if self._exiftool_path else {}
            et = ExifToolHelper(**kwargs)
            et.run()  # surface startup errors immediately
            return et
        except Exception as exc:
            logger.warning(
                "ExifTool unavailable (%s); using Pillow for images only "
                "— videos will be skipped.",
                exc,
            )
            self._exiftool_unavailable = True
            return None

    @staticmethod
    def _read_exiftool(et: "ExifToolHelper", path: Path) -> dict[str, object]:
        out = et.get_tags(_EXIFTOOL_TAGS, str(path))
        if not out or not isinstance(out[0], dict):
            return {}
        # -G prefixes keys with group names (e.g. "EXIF:GPSLatitude").
        return {str(k).split(":", 1)[-1]: v for k, v in out[0].items()}

    # ---------------------------------------------------------------- pillow

    @staticmethod
    def _read_pillow(path: Path) -> dict[str, object]:
        try:
            from PIL import Image
            from pillow_heif import register_heif_opener
        except ImportError:
            return {}
        try:
            register_heif_opener()
            with Image.open(path) as img:
                exif = img.getexif()
        except Exception:
            logger.debug("pillow failed to open %s", path)
            return {}
        if not exif:
            return {}

        gps = exif.get_ifd(_PIL_GPS_IFD) or {}
        exif_sub = exif.get_ifd(_PIL_EXIF_IFD) or {}
        latitude = _pillow_coord(gps.get(_PIL_GPS_LAT), gps.get(_PIL_GPS_LAT_REF))
        longitude = _pillow_coord(gps.get(_PIL_GPS_LON), gps.get(_PIL_GPS_LON_REF))
        tags: dict[str, object] = {
            "GPSLatitude": latitude,
            "GPSLongitude": longitude,
            "GPSAltitude": _pillow_altitude(gps.get(_PIL_GPS_ALT)),
            # iPhone/piexif files keep DateTimeOriginal in the Exif sub-IFD;
            # Pillow-written files place it at the top level. Check both.
            "DateTimeOriginal": _to_str(
                exif_sub.get(_PIL_DATETIME_ORIGINAL)
                or exif.get(_PIL_DATETIME_ORIGINAL)
            ),
            "Make": _to_str(exif.get(_PIL_MAKE)),
            "Model": _to_str(exif.get(_PIL_MODEL)),
        }
        return {k: v for k, v in tags.items() if v is not None}


# ------------------------------------------------------------------- helpers


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip().strip("\x00").strip()
    return s or None


def _pillow_coord(value: object, ref: object | None) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, (tuple, list)) and len(value) >= 3:
            d, m, s = (float(x) for x in value[:3])
            val = d + m / 60.0 + s / 3600.0
        else:
            val = float(value)
    except (TypeError, ValueError):
        return None
    r = str(ref).strip().upper()[:1] if ref else ""
    if r in ("S", "W"):
        val = -abs(val)
    return val


def _pillow_altitude(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

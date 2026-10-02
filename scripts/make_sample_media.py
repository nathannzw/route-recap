"""Generate synthetic geotagged travel media for pipeline testing.

Creates ~17 JPEGs in ``data/sample/`` simulating an Iceland south-coast
road trip: a photo burst at a viewpoint, a 45-minute stop at Vík, and one
file with no GPS data (to exercise the graceful-skip path).

Run from the repo root:  uv run --with piexif python scripts/make_sample_media.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

import piexif

T0 = datetime(2026, 8, 14, 8, 0, 0)

# (offset_minutes, latitude, longitude, label)
PLAN = [
    # burst at Seljalandsfoss viewpoint (4 rapid shots)
    (0, 63.6158, -19.9888, "seljalandsfoss-1"),
    (1, 63.6159, -19.9887, "seljalandsfoss-2"),
    (2, 63.6160, -19.9886, "seljalandsfoss-3"),
    (3, 63.6161, -19.9885, "seljalandsfoss-4"),
    # driving east along the ring road
    (15, 63.5600, -19.6000, "drive-1"),
    (25, 63.5100, -19.4500, "drive-2"),
    (35, 63.4800, -19.3300, "drive-3"),
    (45, 63.4500, -19.1800, "drive-4"),
    (55, 63.4300, -19.1000, "drive-5"),
    # stop at Vík (3 shots, then a 45 min pause)
    (70, 63.4186, -19.0058, "vik-1"),
    (72, 63.4187, -19.0059, "vik-2"),
    (75, 63.4185, -19.0061, "vik-3"),
    (120, 63.4190, -19.0070, "vik-depart"),
    # onward east
    (140, 63.3900, -18.6000, "east-1"),
    (165, 63.3500, -18.3000, "east-2"),
    (190, 63.3400, -18.0500, "east-3"),
]

COLORS = [
    (90, 140, 190), (120, 160, 200), (60, 110, 160), (150, 180, 210),
    (40, 90, 140), (70, 130, 180), (100, 150, 195), (130, 175, 215),
    (55, 100, 150), (85, 135, 185), (110, 155, 200), (45, 95, 145),
    (75, 125, 175), (105, 150, 195), (135, 170, 205), (95, 140, 190),
]


def dms(value: float):
    """Split a float degree value into a piexif DMS rational tuple."""
    sign = 1 if value >= 0 else -1
    value = abs(value)
    d = int(value)
    minutes_full = (value - d) * 60
    m = int(minutes_full)
    s = (minutes_full - m) * 60
    return ((d, 1), (m, 1), (round(s * 100), 100)), sign


def gps_dict(lat: float, lon: float, alt_m: float = 40.0) -> dict:
    (lat_dms, lat_sign), (lon_dms, lon_sign) = dms(lat), dms(lon)
    return {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat_sign > 0 else b"S",
        piexif.GPSIFD.GPSLatitude: lat_dms,
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon_sign > 0 else b"W",
        piexif.GPSIFD.GPSLongitude: lon_dms,
        piexif.GPSIFD.GPSAltitudeRef: 0,  # above sea level
        piexif.GPSIFD.GPSAltitude: (int(alt_m * 100), 100),
    }


def make_photo(
    path: Path,
    taken: datetime,
    lat: float | None,
    lon: float | None,
    color: tuple[int, int, int],
) -> None:
    img = Image.new("RGB", (640, 480), color)
    if lat is not None and lon is not None:
        exif_dict = {
            "0th": {
                piexif.ImageIFD.Make: "Apple",
                piexif.ImageIFD.Model: "iPhone 16 Pro",
            },
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: taken.strftime("%Y:%m:%d %H:%M:%S"),
            },
            "GPS": gps_dict(lat, lon),
        }
        img.save(path, "jpeg", exif=piexif.dump(exif_dict))
    else:
        img.save(path, "jpeg")


def main() -> int:
    out_dir = Path(__file__).resolve().parent.parent / "data" / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (offset, lat, lon, label) in enumerate(PLAN):
        taken = T0 + timedelta(minutes=offset)
        color = COLORS[i % len(COLORS)]
        make_photo(out_dir / f"{label}.jpg", taken, lat, lon, color)
    # one file without GPS — the pipeline must skip it gracefully
    make_photo(out_dir / "no-gps.jpg", T0 + timedelta(minutes=200), None, None, (200, 200, 200))
    print(f"Wrote {len(PLAN) + 1} sample JPEGs to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

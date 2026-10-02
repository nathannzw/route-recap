# Sample Media for Testing

`scripts/make_sample_media.py` generates a synthetic Iceland south-coast
"trip" of geotagged JPEGs in `data/sample/`:

- **4-shot burst** at a viewpoint (~same spot, seconds apart) → exercises
  deduplication into a single waypoint.
- **1 photo from London taken an hour earlier** → exercises the outlier
  filter (flight-speed timeline jump).
- **10–20 minute spaced photos** while "driving" → transient waypoints.
- **3 photos + 45-minute pause** at Vík → exercises stop detection.
- **1 file without GPS** → exercises the graceful-skip path.

```powershell
uv run --with piexif python scripts/make_sample_media.py
uv run route-recap -n "Iceland South Coast Test" -i data/sample --no-open
```

The output report lands in `output/iceland-south-coast-test/index.html`.

> Note: the fixtures are JPEGs only (HEIC/MOV synthesis isn't practical
> without device media). Drop real iPhone files into `data/` to exercise the
> ExifTool HEIC/MOV paths — see `docs/setup_exiftool.md`.

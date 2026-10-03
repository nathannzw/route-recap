# route-recap 🗺️

Inspired by a recent road trip: after coming back with heaps of geotagged photos
and videos, existing tools didn't offer a clean, fully controlled, native way to
view the whole journey. `route-recap` is a custom solution built to turn raw
travel media into an automated, continuous route map summary.

## 🎯 What it does

Feed it a folder of iPhone travel media (HEIC / JPEG / MOV / MP4) and it:

1. **Extracts** GPS coordinates and capture timestamps from every file
   (ExifTool first, Pillow + `pillow-heif` as an image fallback). Both photos
   **and videos** carry GPS on iPhone — in a sample 4,040-file trip, 1,889 of
   1,895 videos had location data.
2. **Filters outliers** — photo dumps usually contain noise: home, airport,
   or even foreign-country photos. Waypoints that imply flight-speed jumps
   in the timeline are split off and the largest continuous segment is kept
   as the trip (opt out with `--no-filter-outliers`).
3. **Deduplicates** photo bursts (same viewpoint within ~50 m / 5 min) into
   single waypoints.
4. **Detects stops** — places where you paused long enough to matter
   (default 30 min, or 6 h for overnight stays) — and reverse-geocodes them
   into named landmarks.
5. **Reconstructs the driven route** snapped to real roads using Google
   (Snap to Roads + Routes API) with automatic OSRM fallback when no API key
   is configured. Where Google's road network is incomplete (gravel side
   roads, fjord viewpoints), the unsnapped runs are **bridged via OSRM**
   (OpenStreetMap data) so the route stays continuous; only truly isolated
   points become **off-road POIs** on the map.
6. **Builds a static report** — one `index.html` with the trip data embedded,
   a Leaflet map with the route, numbered stop pins and off-road POIs, plus a
   `summary.json` sidecar. Each trip **day gets its own route color**, the
   legend has day filter buttons, and a **drive animation** plays the whole
   journey with a car whose traveled trail turns gray while the road ahead
   keeps its day color. Trip media is hardlinked into the report folder with
   thumbnails, and stop popups show photo strips linking back to the
   originals. The single HTML file is **shareable and works on mobile** — a
   Share button copies the link and a Download button saves the file. Leaflet
   and the basemap tiles load from the network when you open the report.
7. **Packs it into one file you can send** (`--single-file`) — `journey.html`
   embeds small copies of every photo plus Leaflet itself, so it survives being
   AirDropped, emailed or messaged. No server, no setup for the person opening
   it. See [Sharing a trip](#-sharing-a-trip).

## 🔄 How a trip becomes a map

Run the CLI once; it coordinates local media processing, optional map APIs, and
report generation — extract → filter/dedupe/detect stops → reconstruct the road
route → name stops → render. Every external service has a fallback, so a run
degrades rather than fails.

**[→ Full pipeline diagram and technical guide](docs/technical-guide.md)** —
the flowchart, plus per-stage algorithms, provider fallback rules, the data
model, frontend internals, the single-file export, and known limitations.

### What each package does

| Package | Responsibility |
|---|---|
| `packages/cli` | Collects trip settings, runs the pipeline, shows progress, serves reports. |
| `packages/core` | Defines the data models; extracts media metadata; filters out foreign/airport outliers; deduplicates waypoints; detects stops; routes, reverse-geocodes, and tags route segments with their trip day. |
| `packages/generator` | Turns the final trip summary into `index.html`, `summary.json` and the shareable `journey.html` — day-colored routes, day filter legend, drive animation, single-file export; stages media hardlinks and thumbnails. |

The short version of the routing rules, since they shape what you see on the
map: with a Google key, road-snapped waypoints form the route and consecutive
waypoints Google *can't* snap are bridged via OSRM so the route stays
continuous; only isolated trip-boundary points become off-road POIs. Without a
key, OSRM routes everything. If routing fails outright, you still get a report —
just without a route line.

Provider selection, the API-key rules and where the boundaries are between
local work and network calls are all spelled out in the
[technical guide](docs/technical-guide.md#7-configuration--providers).

## 🌈 Day colors & animation

**Color-coded routes by day.** After routing, `clustering.assign_days` tags
every `RouteSegment` with the 0-based day it belongs to (derived from its
start waypoint's timestamp, so an overnight leg stays on the day it set off).
The report renders each segment in that day's color from a curated 12-color
palette (trips longer than 12 days cycle), and the legend lists one toggle per
day so you can show or hide individual days.

**Day-aware animation.** The play/speed/slider controls drive a car along the
route. During playback the full route is hidden and the line is **revealed in
the car's wake**, segment by segment, in each day's color — so you only ever
see ground you've already covered. The label's `Day N` chip is tinted with the
current day's color. Jumping the slider ahead reveals up to that point
instantly, and the **⟲ reset button** (or dragging back to 0) restarts the
journey from the beginning and brings the whole overview route back. Toggling a
day off removes its segments from the animation timeline, and the car skips
across the hidden days instead of driving a straight line. The car itself stays
upright on screen — direction is conveyed by the route arrows.

**Steady pacing.** The drive is deliberately *not* driven by the raw photo
timestamps. Real elapsed time made the car crawl through long overnight gaps
and then rocket through legs whose two photos were seconds apart. Instead the
car moves at a constant nominal speed and each stop becomes a short, capped
pause, while the clock and `Day` label still read from the true trip timeline.

**Icons, not dots.** Waypoints are drawn as 📷 markers and off-road POIs as a
larger, glowing 🏔️, so the map reads at a glance; stops keep their numbered
pins and show a `Stop N` tooltip on hover. Direction arrows are spaced at an
even distance interval along the whole route (roughly one per 60 km on a
country-scale trip), each tinted with its day's color.

About **off-road spots**: they are genuinely rare by design. Only waypoints
that Snap-to-Roads cannot place *and* that sit at the trip's boundary stay
off-road — interior ones are bridged via OSRM so the route stays continuous. On
a real 748-waypoint trip that is typically a handful of points (e.g. arrival
photos at the airport), so they are easy to miss among thousands of 📷 pins and
often collapse into a single cluster bubble at low zoom.

**Light & dark.** A header toggle switches the report between light and dark
(themes respect `prefers-color-scheme` on first load and the choice is
remembered). Dark mode loads CARTO's `dark_all` basemap when a CARTO key is
configured, and inverts the OpenStreetMap tiles as a fallback when it isn't.

**Share anywhere.** The header has a **Share** button (native share sheet on
iOS, clipboard elsewhere) and a **Download** button. On touch devices, swipe
up/down on the map to change animation speed and tap the progress label to
play/pause. To send the trip to someone who isn't on your network, use
[`--single-file`](#-sharing-a-trip).

## 🚀 Setup

Requires **Python ≥ 3.12** and [**uv**](https://docs.astral.sh/uv/).

```powershell
# 1. Install everything (one shared .venv, all packages installed editable)
uv sync --all-packages

# 2. Install ExifTool (video metadata; Pillow covers images only)
winget install OliverBetz.ExifTool
#    see docs/setup_exiftool.md for choco / scoop / manual options
exiftool -ver

# 3. Optional: configure a Google Maps Platform API key
Copy-Item .env.example .env   # then edit .env and add GOOGLE_MAPS_API_KEY

# 4. Optional: ffmpeg for video preview frames in map popups
winget install Gyan.FFmpeg
```

## 🗺️ Usage

```powershell
# Interactive: prompts for trip name, folder, units, stop threshold
uv run route-recap

# Non-interactive: all defaults except the input folder
uv run route-recap --input-dir "C:\Photos\iceland-2026" --name "Iceland Ring Road 2026" --unit km

# Keep waypoints from a different trip (home/airport/foreign photos)
uv run route-recap --no-filter-outliers

# Skip media staging/thumbnails
uv run route-recap --no-media

# Also write journey.html — one self-contained file you can send to anyone
uv run route-recap --single-file

# See all options
uv run route-recap --help
```

Output lands in `output/<trip-name>/index.html` (plus a `summary.json`
sidecar). Trip media is hardlinked into `output/<trip-name>/media/` with
thumbnails in `thumbs/` (no extra disk space, no copies).

To view the photos in the map popups, serve the reports over HTTP —
browsers block `file://` pages from loading `file://` images:

```powershell
uv run route-recap serve            # serves output/ at http://127.0.0.1:8000
uv run route-recap serve --port 9000 --no-open
```

The trip data is embedded, while Leaflet, map tiles, and photo strips are
fetched through the local server.

## 📤 Sharing a trip

The simplest way to show someone else your trip needs **no server at all**:

```powershell
uv run route-recap --single-file
```

This writes `output/<trip>/journey.html` — one file, typically 10–20 MB for a
couple of thousand photos. Send it however you like (AirDrop, iMessage, email,
WhatsApp, a USB stick) and the recipient just opens it. What's inside:

| Embedded | Not embedded |
|---|---|
| All trip data (route, stops, days, animation) | Full-resolution originals (they'd be gigabytes) |
| Leaflet + markercluster, so no CDN is needed | Videos (too large) |
| A small copy of every photo, so the lightbox works offline | The basemap tiles (see below) |

Useful flags:

```powershell
uv run route-recap --single-file --max-embed-mb 8    # smaller file
uv run route-recap --single-file --no-embed-photos   # ~1 MB, map only
uv run route-recap --single-file --photo-size 400    # sharper in the lightbox, bigger file
uv run route-recap --single-file --no-inline-libraries   # keep the CDN links
```

`--photo-size` (default 200 px) and `--photo-quality` (default 70) trade file
size for sharpness, since 2,000+ photos are being embedded. On the reference
trip: **200 px → 16 MB**, roughly **400 px → 60 MB**.

**Photos:** click any thumbnail and the photo opens **in the page** — no new
tab. From there swipe (or use ← / →, or the arrows) to browse every photo at
that place, and press Esc or tap the backdrop to close. A popup only shows the
first handful of thumbnails, so when a place has more you'll get a **`+N`**
tile: click it to jump into the full set. Nothing is hidden.

In the **folder** report the lightbox shows the **full-resolution** original
(`media/`), so you can zoom into a photo properly. In a **single-file** report
there are no originals, so it shows the embedded preview instead.

**Basemap:** tiles are the one thing that can't be inlined cheaply, so the map
needs internet to show the actual terrain. Without it the report still fully
works — route, day colors, arrows, stops, photos, animation — just drawn on a
blank background. Everything except the tiles is offline.

### 📱 Opening it on an iPhone / iPad

Send the file to the phone, then **tap it** and choose **Safari** (in the iOS
Files app, tap the file, then the share icon → *Open in Safari*). Safari can
open a local `.html` file directly, so nothing needs to be hosted.

> A quick note: a `.html` attachment is slightly awkward on iOS compared to a
> link — some apps preview it instead of opening it. If that bothers you, see
> the alternatives below, which trade a little setup for a nicer experience.

### Alternatives

| Approach | Command | When to use it |
|---|---|---|
| **Single file** | `uv run route-recap --single-file` | Default. Works anywhere, no setup. |
| **LAN server** | `uv run route-recap serve --host 0.0.0.0` | Same Wi-Fi as your PC; photos at full size. |
| **HTTPS tunnel** | `route-recap serve --host 0.0.0.0` + `cloudflared tunnel --url http://localhost:8000` | A **secure context**, so the Share button uses the iOS share sheet. Works off-LAN. |
| **Static host** | Upload `output/<trip>/` to GitHub Pages / Netlify / Cloudflare Pages | Best for sending a *link* people just tap; include `media/` and `thumbs/`. |

```powershell
# LAN: serve on all interfaces (not just this PC's loopback)
uv run route-recap serve --host 0.0.0.0
# It prints something like:
#   On your phone (same Wi-Fi): http://192.168.1.107:8000/
```

If the phone times out on the LAN option, allow Python through the firewall on
**Private** networks — and note that Windows firewalls match on the *exact*
interpreter path, so a rule for another Python install won't cover the
project's `.venv\Scripts\python.exe`.

## ⚙️ Configuration (`.env`)

| Variable               | Purpose                                                            |
| ---------------------- | ------------------------------------------------------------------ |
| `GOOGLE_MAPS_API_KEY`  | Enables Google routing + geocoding (primary provider)              |
| `CARTO_BASEMAP_KEY`    | CARTO Voyager basemap tiles in the HTML report (falls back to OSM) |
| `NOMINATIM_USER_AGENT` | User-Agent for OSM Nominatim geocoding fallback                    |
| `EXIFTOOL_PATH`        | Explicit path to `exiftool.exe` when it isn't on `PATH`            |
| `OSRM_BASE_URL`        | OSRM server used as the routing fallback                           |

**Provider fallback:** with a Google key, on-road waypoints are snapped and
routed with Google Roads and Routes APIs; stop names use Google Geocoding.
Unsnapped waypoints become separate POIs. Without a key, routing uses the
public OSRM demo server and stop naming uses Nominatim. Both public fallbacks
are rate-limited; OSRM does not identify off-road POIs.

## 🧪 Development

```powershell
uv run pytest          # unit tests for models, clustering, router
uv run --with piexif python scripts/make_sample_media.py   # synthetic fixtures in data/sample/
```

## 🔮 Roadmap

- **Stay & Stop Detection:** identify lodging and rest stops (shipped in v0.1)
- **LLM Layer:** parse key highlight photos and generate trip journals / leg summaries
- **Native Interface:** media retrieval shipped in v0.1 — hardlinked originals,
  thumbnails, popup photo strips, and `route-recap serve`. A full-res media
  explorer UI remains on the roadmap.

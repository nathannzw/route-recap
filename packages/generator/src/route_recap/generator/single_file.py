"""Build a single self-contained HTML file that can be shared as-is.

The normal report (:func:`route_recap.generator.html_builder.build_html`) is a
*folder*: ``index.html`` plus ``media/`` and ``thumbs/`` next to it. That is
fine on your own machine, but browsers block a ``file://`` page from loading
``file://`` images, so the folder only works through ``route-recap serve``.

This module produces the opposite trade-off: one ``journey.html`` you can
AirDrop, email or message to someone. Concretely it

* inlines Leaflet + markercluster instead of linking them from a CDN,
* re-encodes a small copy of each photo and embeds it as a ``data:`` URI,
* drops videos and full-resolution originals (they are far too large), and
* leaves the basemap tiles online, which is the one thing that cannot be
  inlined cheaply — without a network the map still draws the route, just on a
  blank background.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path

import httpx

from route_recap.core.models import TripSummary

from .html_builder import _render_html

logger = logging.getLogger(__name__)

UNPKG = "https://unpkg.com"
_LEAFLET = f"{UNPKG}/leaflet@1.9.4/dist"
_CLUSTER = f"{UNPKG}/leaflet.markercluster@1.5.3/dist"

#: Order matters: MarkerCluster.Default.css must come after MarkerCluster.css.
VENDOR_CSS = (
    (_LEAFLET, "leaflet.css"),
    (_CLUSTER, "MarkerCluster.css"),
    (_CLUSTER, "MarkerCluster.Default.css"),
)
VENDOR_JS = (
    (_LEAFLET, "leaflet.js"),
    (_CLUSTER, "leaflet.markercluster.js"),
)

#: Where downloaded library files are cached between runs.
_CACHE_ENV = "ROUTE_RECAP_CACHE"

#: Priority order for photo embedding: the photos that matter most for a trip
#: story go in first, so a budget-limited file still shows the highlights.
STOP_FIRST = "stops-first"


def vendor_cache_dir() -> Path:
    """Directory holding downloaded Leaflet sources between runs."""
    override = os.getenv(_CACHE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".route-recap" / "vendor"


def _download(url: str, cache: Path) -> str:
    """Fetch ``url``, reusing a cached copy when present."""
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / url.rsplit("/", 1)[-1]
    if cached.is_file():
        return cached.read_text(encoding="utf-8")
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    text = resp.text
    cached.write_text(text, encoding="utf-8")
    return text


def _inline_safe(text: str) -> bool:
    """True when ``text`` can be inlined without breaking the HTML parser.

    A literal ``</script>`` (or ``</style>``) inside the payload would end the
    element early, so such a payload is refused rather than corrupting the page.
    """
    lowered = text.lower()
    return "</script" not in lowered and "</style" not in lowered


def load_vendor_assets(
    cache: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(inline_context, warnings)`` for inlining Leaflet.

    Any failure (offline, blocked CDN) is reported as a warning and simply
    leaves that file out, in which case the template keeps its CDN link.
    """
    cache = cache or vendor_cache_dir()
    warnings: list[str] = []
    css_parts: list[str] = []
    js_parts: list[str] = []

    sources = [(url, name, True) for url, name in VENDOR_CSS]
    sources += [(url, name, False) for url, name in VENDOR_JS]

    for base, name, is_css in sources:
        url = f"{base}/{name}"
        try:
            text = _download(url, cache)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the build
            warnings.append(f"could not inline {name} ({exc}); using the CDN link")
            continue
        if not _inline_safe(text):
            warnings.append(f"{name} contains a tag-terminating sequence; using the CDN link")
            continue
        (css_parts if is_css else js_parts).append(text)

    inline: dict[str, str] = {}
    # Only inline when *every* file arrived — a partial inline would leave the
    # page half-broken (e.g. Leaflet CSS but no Leaflet JS).
    if len(css_parts) == len(VENDOR_CSS):
        inline["vendor_css"] = "\n".join(css_parts)
    else:
        warnings.append("incomplete stylesheet set; linking the CDN instead")
    if len(js_parts) == len(VENDOR_JS):
        inline["vendor_js"] = "\n".join(js_parts)
    else:
        warnings.append("incomplete script set; linking the CDN instead")
    return inline, warnings


def _ordered_sources(summary: TripSummary) -> list[str]:
    """Source-file keys ordered by storytelling value.

    Stops first, then off-road POIs, then remaining waypoints — so an embedded
    budget is spent on the photos a viewer is most likely to open.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add(paths) -> None:  # noqa: ANN001 - list[Path]
        for p in paths:
            key = str(p)
            if key not in seen:
                seen.add(key)
                ordered.append(key)

    for stop in summary.stops:
        add(stop.source_files)
    for wp in summary.waypoints:
        if wp.off_road:
            add(wp.source_files)
    for wp in summary.waypoints:
        add(wp.source_files)
    return ordered


def _encode_photo(path: Path, size: int, quality: int) -> str | None:
    """Downscale ``path`` and return a ``data:`` URI, or ``None`` on failure."""
    try:
        from PIL import Image, ImageOps
        from pillow_heif import register_heif_opener

        register_heif_opener()
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
    except Exception:  # noqa: BLE001 - one bad photo must not fail the export
        logger.debug("could not embed %s", path, exc_info=True)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def embed_photos(
    summary: TripSummary,
    output_dir: Path,
    assets: dict[str, dict],
    *,
    max_bytes: int,
    size: int = 200,
    quality: int = 70,
) -> tuple[dict[str, dict], int, bool]:
    """Embed small copies of the trip photos into the asset map.

    Returns ``(embedded_assets, count, truncated)``. Assets that are not
    embedded (videos, unreadable files, or anything past the budget) are simply
    left out, which makes the report skip them instead of rendering a broken
    link. Existing ``thumbs/`` are preferred over originals so the source
    images are never re-read.
    """
    embedded: dict[str, dict] = {}
    used = 0
    truncated = False

    for key in _ordered_sources(summary):
        asset = assets.get(key)
        if not asset or asset.get("kind") != "image":
            continue  # videos and full-res originals are never embedded
        source: Path | None = None
        thumb = asset.get("thumb")
        if thumb:
            candidate = output_dir / thumb
            if candidate.is_file():
                source = candidate
        if source is None:
            candidate = Path(key)
            if candidate.is_file():
                source = candidate
        if source is None:
            continue

        data_uri = _encode_photo(source, size, quality)
        if data_uri is None:
            continue
        if used + len(data_uri) > max_bytes:
            truncated = True
            continue  # keep trying smaller remaining files

        used += len(data_uri)
        # ``url`` is deliberately left empty: pointing both ``url`` and
        # ``thumb`` at the data URI would store the same base64 payload twice.
        # The template falls back to ``thumb`` as the link target.
        embedded[key] = {"url": "", "thumb": data_uri, "kind": "image"}

    return embedded, len(embedded), truncated


def build_single_file(
    summary: TripSummary,
    output_dir: str | Path,
    *,
    carto_key: str | None = None,
    assets: dict[str, dict] | None = None,
    embed: bool = True,
    max_embed_mb: float = 20.0,
    thumb_size: int = 200,
    thumb_quality: int = 70,
    inline_libraries: bool = True,
    cache_dir: Path | None = None,
) -> tuple[Path, list[str]]:
    """Write one shareable ``journey.html`` and return ``(path, warnings)``."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = assets or {}
    warnings: list[str] = []

    embedded: dict[str, dict] = {}
    if embed:
        embedded, count, truncated = embed_photos(
            summary,
            output_dir,
            assets,
            max_bytes=int(max_embed_mb * 1024 * 1024),
            size=thumb_size,
            quality=thumb_quality,
        )
        if truncated:
            warnings.append(
                f"photo budget of {max_embed_mb:.0f} MB reached — "
                f"{count} photo(s) embedded, lower resolutions of the rest omitted"
            )

    inline: dict[str, str] = {}
    if inline_libraries:
        inline, lib_warnings = load_vendor_assets(cache_dir)
        warnings.extend(lib_warnings)

    html = _render_html(
        summary, carto_key=carto_key, assets=embedded, inline=inline
    )
    path = output_dir / "journey.html"
    path.write_text(html, encoding="utf-8")
    return path, warnings

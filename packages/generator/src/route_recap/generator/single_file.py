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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx

from route_recap.core.models import TripSummary

from .html_builder import _render_html
from .media_assets import THUMB_SIZE

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
            # Ask libjpeg to decode straight to a reduced size (DCT scaling)
            # instead of decoding a full 12 MP frame and then shrinking it.
            # Without this, embedding at a high --photo-size reads every
            # original at full resolution: fine for a few files, but minutes to
            # hours across thousands of photos. Files that don't support draft
            # simply ignore it.
            try:
                img.draft("RGB", (size, size))
            except Exception:  # noqa: BLE001 - draft is a pure optimisation
                pass
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            # No ``optimize=True``: it re-runs the Huffman pass several times for
            # a ~2% size saving, which is invisible next to the cost of doing it
            # across thousands of photos.
            img.save(buf, "JPEG", quality=quality)
    except Exception:  # noqa: BLE001 - one bad photo must not fail the export
        logger.debug("could not embed %s", path, exc_info=True)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _pick_source(
    asset: dict, key: str, output_dir: Path, size: int
) -> Path | None:
    """Choose the file to encode an embedded photo from.

    The staged ``thumbs/`` are capped at :data:`THUMB_SIZE` and Pillow's
    ``thumbnail()`` never upscales, so embedding *from* a thumbnail silently
    caps sharpness at that size no matter how large ``--photo-size`` is. For a
    small embed the thumbnail is a cheap shortcut; past it we must go back to
    the original.
    """
    original = Path(key)
    thumb_path = None
    thumb = asset.get("thumb")
    if thumb:
        candidate = output_dir / thumb
        if candidate.is_file():
            thumb_path = candidate
    if size <= THUMB_SIZE and thumb_path is not None:
        return thumb_path
    if original.is_file():
        return original
    return thumb_path


def embed_photos(
    summary: TripSummary,
    output_dir: Path,
    assets: dict[str, dict],
    *,
    max_bytes: int,
    size: int = 200,
    quality: int = 70,
    workers: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, dict], int, bool]:
    """Embed small copies of the trip photos into the asset map.

    Returns ``(embedded_assets, count, truncated)``. Assets that are not
    embedded (videos, unreadable files, or anything past the budget) are left
    out, which makes the report skip them instead of rendering a broken link.

    Scaling matters here because a trip can carry thousands of photos:

    * encoding runs in a **thread pool** — Pillow releases the GIL for decode
      and encode, so this is close to a linear speed-up on multiple cores;
    * once ``max_bytes`` is spent, encoding **stops** instead of grinding
      through every remaining photo only to discard it;
    * ``on_progress(done, total)`` lets the caller show progress, since a
      high-resolution export of a large trip legitimately takes a while.
    """
    candidates: list[tuple[str, Path]] = []
    for key in _ordered_sources(summary):
        asset = assets.get(key)
        if not asset or asset.get("kind") != "image":
            continue  # videos are never embedded
        source = _pick_source(asset, key, output_dir, size)
        if source is not None:
            candidates.append((key, source))

    embedded: dict[str, dict] = {}
    used = 0
    truncated = False
    total = len(candidates)
    if not total:
        return embedded, 0, False

    if workers is None:
        workers = min(8, (os.cpu_count() or 4))

    def work(item: tuple[str, Path]) -> tuple[str, str | None]:
        key, source = item
        return key, _encode_photo(source, size, quality)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, item) for item in candidates]
        for i, future in enumerate(futures):
            if used >= max_bytes:
                truncated = True
                for pending in futures[i:]:
                    pending.cancel()
                break
            key, data_uri = future.result()
            if data_uri is None:
                continue
            if used + len(data_uri) > max_bytes:
                # Skip this one but keep filling from smaller photos.
                truncated = True
                continue
            used += len(data_uri)
            # ``url`` is deliberately left empty: pointing both ``url`` and
            # ``thumb`` at the data URI would store the payload twice.
            embedded[key] = {"url": "", "thumb": data_uri, "kind": "image"}
            if on_progress is not None:
                on_progress(len(embedded), total)

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
    on_progress: Callable[[int, int], None] | None = None,
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
            on_progress=on_progress,
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

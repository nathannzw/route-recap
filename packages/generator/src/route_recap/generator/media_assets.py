"""Stage trip media into the report folder (hardlinks + thumbnails)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

THUMB_SIZE = 320
_FFMPEG = shutil.which("ffmpeg")

VIDEO_EXTENSIONS = {".mov", ".mp4"}
IMAGE_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg"}


def _kind_of(suffix: str) -> str:
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "image"


def _link_or_copy(src: Path, dst: Path) -> bool:
    """Hardlink ``src`` to ``dst``; fall back to a copy across devices."""
    try:
        dst.unlink(missing_ok=True)
        os.link(src, dst)
        return True
    except FileExistsError:
        return True  # already linked from a previous run
    except OSError:
        try:
            shutil.copy2(src, dst)
            return True
        except OSError:
            logger.warning("could not stage media file %s", src)
            return False


def _make_image_thumb(src: Path, dst: Path, size: int) -> bool:
    try:
        from PIL import Image, ImageOps
        from pillow_heif import register_heif_opener

        register_heif_opener()
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)  # respect phone orientation
            img.thumbnail((size, size))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(dst, "JPEG", quality=82)
        return True
    except Exception:
        logger.debug("thumbnail failed for %s", src, exc_info=True)
        return False


def _make_video_thumb(src: Path, dst: Path, size: int) -> bool:
    if not _FFMPEG:
        return False
    try:
        result = subprocess.run(
            [
                _FFMPEG,
                "-y",
                "-ss",
                "1",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({size},iw)':-2",
                str(dst),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0 and dst.exists()
    except Exception:
        logger.debug("video thumbnail failed for %s", src, exc_info=True)
        return False


def stage_media(
    sources: Sequence[Path],
    output_dir: Path,
    *,
    thumb_size: int = THUMB_SIZE,
) -> dict[str, dict]:
    """Hardlink trip media into ``output/<trip>/media/`` and build thumbnails.

    Returns a mapping ``{source_path_string: {"url", "thumb", "kind"}}`` where
    ``url``/``thumb`` are report-relative URLs and ``kind`` is ``"image"`` or
    ``"video"``. Hardlinks keep the originals on disk without copying;
    :func:`shutil.copy2` is used as a cross-device fallback. Video thumbnails
    require ``ffmpeg`` on PATH — without it videos are staged but have no
    preview image.
    """
    media_dir = output_dir / "media"
    thumbs_dir = output_dir / "thumbs"
    media_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    seen: list[Path] = []
    for src in sources:
        if src not in seen:
            seen.append(src)

    assets: dict[str, dict] = {}
    for i, src in enumerate(seen, 1):
        key = str(src)
        abs_src = src if src.is_absolute() else Path.cwd() / src
        ext = src.suffix.lower()
        kind = _kind_of(ext)
        asset_name = f"{i:04d}{ext if ext else ''}"
        dst = media_dir / asset_name
        if not _link_or_copy(abs_src, dst):
            continue
        thumb_url: str | None = None
        if kind == "image":
            if _make_image_thumb(abs_src, thumbs_dir / f"{i:04d}.jpg", thumb_size):
                thumb_url = f"thumbs/{i:04d}.jpg"
        elif _make_video_thumb(abs_src, thumbs_dir / f"{i:04d}.jpg", thumb_size):
            thumb_url = f"thumbs/{i:04d}.jpg"
        assets[key] = {"url": f"media/{asset_name}", "thumb": thumb_url, "kind": kind}
    return assets

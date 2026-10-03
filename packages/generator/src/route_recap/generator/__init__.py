"""Static HTML map report generation for route-recap."""

from .html_builder import build_html
from .media_assets import stage_media
from .single_file import build_single_file

__all__ = ["build_html", "build_single_file", "stage_media"]

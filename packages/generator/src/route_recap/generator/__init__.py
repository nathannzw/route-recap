"""Static HTML map report generation for route-recap."""

from .html_builder import build_html
from .media_assets import stage_media

__all__ = ["build_html", "stage_media"]

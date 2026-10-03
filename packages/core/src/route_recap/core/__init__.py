"""Core pipeline for route-recap: EXIF extraction, clustering, and routing."""

from .clustering import (
    assign_days,
    deduplicate,
    detect_stops,
    filter_outliers,
    haversine_m,
)
from .extractor import MediaExtractor
from .models import (
    DistanceUnit,
    MediaMetadata,
    MediaType,
    RouteSegment,
    Stop,
    TripConfig,
    TripSummary,
    Waypoint,
)
from .router import (
    RouteResult,
    RoutingError,
    RoutingProvider,
    compute_route,
    reverse_geocode,
    reverse_geocode_detail,
)

__all__ = [
    "DistanceUnit",
    "MediaExtractor",
    "MediaMetadata",
    "MediaType",
    "RouteResult",
    "RouteSegment",
    "RoutingError",
    "RoutingProvider",
    "Stop",
    "TripConfig",
    "TripSummary",
    "Waypoint",
    "assign_days",
    "compute_route",
    "deduplicate",
    "detect_stops",
    "filter_outliers",
    "haversine_m",
    "reverse_geocode",
    "reverse_geocode_detail",
]

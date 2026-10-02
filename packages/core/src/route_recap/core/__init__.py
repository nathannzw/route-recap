"""Core pipeline for route-recap: EXIF extraction, clustering, and routing."""

from .clustering import deduplicate, detect_stops, haversine_m
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
    "compute_route",
    "deduplicate",
    "detect_stops",
    "haversine_m",
    "reverse_geocode",
    "reverse_geocode_detail",
]

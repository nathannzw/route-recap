"""Spatial/temporal clustering for route-recap.

Two operations:
1. **Deduplication** — collapse photo bursts (same viewpoint within a small
   radius and time window) into single representative waypoints to avoid
   routing-API bloat.
2. **Stop detection** — identify significant pauses as "stops/landmarks"
   rather than transient road waypoints.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from .models import Stop, Waypoint

logger = logging.getLogger(__name__)

_EARTH_RADIUS_M = 6_371_008.8  # mean earth radius (meters)


def haversine_m(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two coordinates, in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _time_since(a: datetime | None, b: datetime | None) -> float | None:
    """Seconds between two datetimes (b - a); None if either is missing."""
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def deduplicate(
    waypoints: list[Waypoint],
    *,
    radius_m: float = 50.0,
    window_minutes: int = 5,
) -> list[Waypoint]:
    """Collapse waypoints taken at the same viewpoint in rapid succession.

    Waypoints are sorted chronologically (missing timestamps go last, each
    kept separate); consecutive points within ``radius_m`` of the current
    cluster anchor AND within ``window_minutes`` merge into that anchor.
    """
    if not waypoints:
        return []

    window_s = window_minutes * 60.0
    timed = sorted(
        (w for w in waypoints if w.timestamp is not None),
        key=lambda w: w.timestamp,
    )
    untimed = [w for w in waypoints if w.timestamp is None]

    result: list[Waypoint] = []
    for anchor in timed:
        if not result:
            result.append(_copy_anchor(anchor))
            continue
        prev = result[-1]
        gap = _time_since(prev.timestamp, anchor.timestamp)
        dist = haversine_m(
            prev.latitude, prev.longitude, anchor.latitude, anchor.longitude
        )
        if gap is not None and gap <= window_s and dist <= radius_m:
            _merge_into(prev, anchor)
        else:
            result.append(_copy_anchor(anchor))
    result.extend(w for w in untimed)  # keep individually, order preserved
    return result


def detect_stops(
    waypoints: list[Waypoint],
    *,
    min_stop_minutes: int = 30,
    stop_max_distance_m: float = 500.0,
    overnight_hours: int = 6,
) -> tuple[list[Waypoint], list[Stop]]:
    """Tag waypoints that begin a significant pause and build the stop list.

    A stop is declared between consecutive waypoints ``a`` and ``b`` when the
    time gap is at least ``min_stop_minutes`` AND either
      * the distance is within ``stop_max_distance_m`` (you didn't move), or
      * the gap is at least ``overnight_hours`` (overnight stay, regardless
        of where the next photo was taken).
    """
    if not waypoints:
        return [], []

    stops: list[Stop] = []
    for a, b in zip(waypoints, waypoints[1:]):
        gap_s = _time_since(a.timestamp, b.timestamp)
        if gap_s is None or gap_s < min_stop_minutes * 60:
            continue
        dist = haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
        is_overnight = gap_s >= overnight_hours * 3600
        if dist <= stop_max_distance_m or is_overnight:
            stop = Stop(
                latitude=a.latitude,
                longitude=a.longitude,
                arrived_at=a.timestamp,
                departed_at=b.timestamp,
                duration_s=int(gap_s),
                media_count=a.media_count,
            )
            a.is_stop = True
            a.stop_index = len(stops)
            stops.append(stop)
    return waypoints, stops


# ------------------------------------------------------------------- helpers


def _copy_anchor(w: Waypoint) -> Waypoint:
    return w.model_copy(
        update={
            "is_stop": False,
            "stop_index": None,
            "media_count": w.media_count,
            "source_files": list(w.source_files),
        }
    )


def _merge_into(target: Waypoint, incoming: Waypoint) -> None:
    target.media_count += incoming.media_count
    for src in incoming.source_files:
        if src not in target.source_files:
            target.source_files.append(src)
    # A stop waypoint absorbs its cluster members.
    if incoming.is_stop and not target.is_stop:
        target.is_stop = True
        target.stop_index = incoming.stop_index

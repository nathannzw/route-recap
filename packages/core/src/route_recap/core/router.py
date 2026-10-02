"""Road-route reconstruction for route-recap.

Primary provider: Google Maps Platform (Snap to Roads + Routes API).
Fallback provider: OSRM (public demo server by default).

``compute_route`` tries providers in order and returns the first successful
result; with no API key configured only OSRM is used. Reverse geocoding
similarly prefers Google Geocoding and falls back to OSM Nominatim.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import httpx
import polyline

from .models import RouteSegment, Waypoint

logger = logging.getLogger(__name__)

DEFAULT_OSRM_URL = "https://router.project-osrm.org"
DEFAULT_NOMINATIM_UA = "route-recap/0.1 (personal use)"

_GOOGLE_SNAP_URL = "https://roads.googleapis.com/v1/snapToRoads"
_GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

_GOOGLE_SNAP_BATCH = 100  # Snap to Roads hard limit
_GOOGLE_ROUTE_WAYPOINTS = 25  # origin + 23 intermediates + destination
_OSRM_WAYPOINTS = 100  # conservative chunk for the public demo server

_KEY_REDACT_RE = re.compile(r"key=[^&\s]+", re.IGNORECASE)


def _sanitize_message(message: str) -> str:
    """Redact API keys from error/log text (e.g. request URLs)."""
    return _KEY_REDACT_RE.sub("key=REDACTED", message)


class RoutingError(Exception):
    """Raised when no routing provider can produce a route."""


class RoutingProvider(Protocol):
    name: str

    def compute_route(self, waypoints: Sequence[Waypoint]) -> RouteResult: ...


@dataclass
class RouteResult:
    provider: str
    segments: list[RouteSegment] = field(default_factory=list)
    #: Indices (into the input waypoint list) that were too far from any
    #: road to route — they are kept as separate off-road POIs instead.
    off_road_indices: list[int] = field(default_factory=list)
    #: Snap-to-Roads result: {original_index: (lat, lon)} for snapped points.
    #: Used by the hybrid merge so OSRM bridges connect exactly where the
    #: Google segments end (no disjoints).
    snap_map: dict[int, tuple[float, float]] = field(default_factory=dict)


# ------------------------------------------------------------------ google


class GoogleRoutesProvider:
    """Google Maps routing: Snap to Roads, then Routes API computeRoutes."""

    name = "google"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def compute_route(self, waypoints: Sequence[Waypoint]) -> RouteResult:
        """Route each contiguous run of on-road waypoints separately.

        Waypoints that Snap to Roads can't place (viewpoints, trailheads,
        parking lots further than ~300 m from a road) are excluded from the
        route and returned via ``off_road_indices``. Each contiguous run of
        snapped waypoints is routed on its own — Google never routes *across*
        an unsnapped gap, so the caller can bridge those gaps with OSRM
        without double-counting distance.
        """
        if len(waypoints) < 2:
            return RouteResult(provider=self.name)

        snap_map = self.snap_to_roads(waypoints)
        if len(snap_map) < 2:
            raise RoutingError(
                "Google Snap to Roads placed fewer than 2 waypoints on roads"
            )

        # Split into maximal runs of consecutive snapped indices.
        runs: list[list[int]] = []
        current: list[int] = []
        for i in range(len(waypoints)):
            if i in snap_map:
                current.append(i)
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)

        segments: list[RouteSegment] = []
        for run in runs:
            if len(run) < 2:
                continue  # single snapped point — covered by a neighbor bridge
            # Several photos can snap to the same road point — drop exact
            # consecutive duplicates so computeRoutes isn't fed zero-length legs.
            index_map: list[int] = []
            routed: list[Waypoint] = []
            for i in run:
                lat, lon = snap_map[i]
                w = waypoints[i].model_copy(
                    update={"latitude": lat, "longitude": lon}
                )
                if (
                    routed
                    and routed[-1].latitude == w.latitude
                    and routed[-1].longitude == w.longitude
                ):
                    continue
                index_map.append(i)
                routed.append(w)
            if len(routed) < 2:
                continue
            segments.extend(self._compute_routes(routed, index_map))

        off_road = [i for i in range(len(waypoints)) if i not in snap_map]
        return RouteResult(
            provider=self.name,
            segments=segments,
            off_road_indices=off_road,
            snap_map=snap_map,
        )

    def snap_to_roads(
        self, waypoints: Sequence[Waypoint]
    ) -> dict[int, tuple[float, float]]:
        """Snap waypoints to the road network.

        Returns ``{original_index: (latitude, longitude)}`` for every point
        within snapping range. Points further than ~300 m from any road are
        silently dropped by the API and simply absent from the result.
        """
        snapped: dict[int, tuple[float, float]] = {}
        offset = 0
        for batch in _chunks(list(waypoints), _GOOGLE_SNAP_BATCH):
            path = "|".join(f"{w.latitude:.6f},{w.longitude:.6f}" for w in batch)
            try:
                resp = self._client.get(
                    _GOOGLE_SNAP_URL, params={"path": path, "key": self._api_key}
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise RoutingError(
                    f"Google Snap to Roads failed: {_sanitize_message(str(exc))}"
                ) from exc
            for sp in resp.json().get("snappedPoints", []):
                idx = sp.get("originalIndex")
                loc = sp.get("location") or {}
                if (
                    idx is None
                    or "latitude" not in loc
                    or "longitude" not in loc
                ):
                    continue
                snapped[offset + int(idx)] = (
                    float(loc["latitude"]),
                    float(loc["longitude"]),
                )
            offset += len(batch)
        return snapped

    def _compute_routes(
        self,
        waypoints: Sequence[Waypoint],
        index_map: Sequence[int],
    ) -> list[RouteSegment]:
        segments: list[RouteSegment] = []
        i = 0
        n = len(waypoints)
        while i < n - 1:
            j = min(i + _GOOGLE_ROUTE_WAYPOINTS, n)
            chunk = waypoints[i:j]
            segment = self._route_chunk(
                chunk,
                start_index=index_map[i],
                end_index=index_map[j - 1],
            )
            if segment:
                segments.append(segment)
            i = j - 1  # next chunk reuses the shared endpoint
        return segments

    def _route_chunk(
        self,
        chunk: Sequence[Waypoint],
        *,
        start_index: int,
        end_index: int,
    ) -> RouteSegment | None:
        def loc(w: Waypoint) -> dict:
            return {
                "location": {
                    "latLng": {"latitude": w.latitude, "longitude": w.longitude}
                }
            }

        body = {
            "origin": loc(chunk[0]),
            "destination": loc(chunk[-1]),
            "intermediates": [loc(w) for w in chunk[1:-1]],
            "travelMode": "DRIVE",
            "polylineEncoding": "ENCODED_POLYLINE",
            "polylineQuality": "HIGH_QUALITY",
        }
        resp = self._client.post(
            _GOOGLE_ROUTES_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": (
                    "routes.distanceMeters,routes.duration,"
                    "routes.polyline.encodedPolyline"
                ),
            },
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            detail = _sanitize_message(f"{exc}")
            if isinstance(exc, httpx.HTTPStatusError):
                detail = (
                    f"status {exc.response.status_code}: "
                    f"{_sanitize_message(exc.response.text[:300])}"
                )
            raise RoutingError(f"Google computeRoutes failed: {detail}") from exc
        route = (resp.json().get("routes") or [None])[0]
        if not route:
            raise RoutingError("Google computeRoutes returned no route")
        encoded = (route.get("polyline") or {}).get("encodedPolyline", "")
        if not encoded:
            raise RoutingError("Google computeRoutes returned an empty polyline")
        return RouteSegment(
            start_index=start_index,
            end_index=end_index,
            encoded_polyline=encoded,
            distance_m=float(route.get("distanceMeters", 0.0)),
            duration_s=_parse_google_duration(route.get("duration")),
        )


def _parse_google_duration(value: object) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if s.endswith("s"):
        try:
            return float(s[:-1])
        except ValueError:
            return 0.0
    return 0.0


# --------------------------------------------------------------------- osrm


class OSRMProvider:
    """OSRM routing provider — used as the no-API-key fallback."""

    name = "osrm"

    def __init__(
        self,
        base_url: str = DEFAULT_OSRM_URL,
        client: httpx.Client | None = None,
        retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None
        self._retries = retries

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, url: str, params: dict) -> httpx.Response:
        """GET with retry/backoff — the public demo server is rate-limited."""
        import time

        last_exc: Exception | None = None
        for attempt in range(self._retries):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = RoutingError(
                        f"OSRM request failed ({resp.status_code}): "
                        f"{_sanitize_message(resp.text[:300])}"
                    )
                else:
                    raise RoutingError(
                        f"OSRM request failed ({resp.status_code}): "
                        f"{_sanitize_message(resp.text[:300])}"
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
            if attempt < self._retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        raise last_exc if isinstance(last_exc, RoutingError) else RoutingError(
            f"OSRM request failed after {self._retries} attempts: {last_exc}"
        )

    def compute_route(self, waypoints: Sequence[Waypoint]) -> RouteResult:
        if len(waypoints) < 2:
            return RouteResult(provider=self.name)
        segments: list[RouteSegment] = []
        i, n = 0, len(waypoints)
        while i < n - 1:
            chunk = waypoints[i : i + _OSRM_WAYPOINTS]
            coord_str = ";".join(
                f"{w.longitude:.6f},{w.latitude:.6f}" for w in chunk
            )
            resp = self._request(
                f"{self._base_url}/route/v1/driving/{coord_str}",
                {"overview": "full", "geometries": "geojson"},
            )
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                raise RoutingError(
                    f"OSRM routing failed: "
                    f"{data.get('code')} {data.get('message', '')}".strip()
                )
            route = data["routes"][0]
            coords = route["geometry"]["coordinates"]  # [[lon, lat], ...]
            encoded = polyline.encode(
                [(lat, lon) for lon, lat in coords], precision=5
            )
            segments.append(
                RouteSegment(
                    start_index=i,
                    end_index=i + len(chunk) - 1,
                    encoded_polyline=encoded,
                    distance_m=float(route.get("distance", 0.0)),
                    duration_s=float(route.get("duration", 0.0)),
                )
            )
            i += len(chunk) - 1
        return RouteResult(provider=self.name, segments=segments)


# -------------------------------------------------------------- orchestration


def _hybrid_merge(
    waypoints: Sequence[Waypoint],
    google_result: RouteResult,
    osrm: OSRMProvider,
) -> tuple[list[RouteSegment], list[int]]:
    """Bridge Google's unsnapped runs with OSRM.

    Google's road network is incomplete in remote areas (gravel side roads,
    fjord viewpoints) — Snap to Roads drops those points even though roads
    exist. OSRM uses OpenStreetMap data, which covers them. Each consecutive
    run of unsnapped waypoints is routed via OSRM *including its boundary
    points* (the last snapped point before the run and the first after), so
    the merged route stays continuous and no distance is double-counted
    (Google never routes across the gap). Only isolated single points (true
    off-road POIs) are kept out of the route.

    Returns ``(merged_segments, isolated_indices)``.
    """
    unsnapped = sorted(google_result.off_road_indices)
    if not unsnapped:
        return list(google_result.segments), []

    # Split into maximal runs of consecutive indices.
    runs: list[list[int]] = []
    for idx in unsnapped:
        if runs and idx == runs[-1][-1] + 1:
            runs[-1].append(idx)
        else:
            runs.append([idx])

    merged = list(google_result.segments)
    isolated: list[int] = []
    for run in runs:
        if len(run) < 2:
            isolated.extend(run)
            continue
        # Bridge from the previous waypoint through the run to the next one,
        # so the route is continuous and the fjord road is fully covered.
        bridge_indices: list[int] = []
        if run[0] > 0:
            bridge_indices.append(run[0] - 1)
        bridge_indices.extend(run)
        if run[-1] < len(waypoints) - 1:
            bridge_indices.append(run[-1] + 1)
        # Drop consecutive duplicates (boundary may equal run start).
        dedup: list[int] = []
        for i in bridge_indices:
            if dedup and dedup[-1] == i:
                continue
            dedup.append(i)
        if len(dedup) < 2:
            isolated.extend(run)
            continue
        try:
            # Boundary points (run[0]-1 and run[-1]+1) are snapped by Google —
            # use their SNAPPED coordinates so the bridge connects exactly
            # where the Google segments end, keeping the route continuous.
            bridge_wps: list[Waypoint] = []
            for i in dedup:
                if i in google_result.snap_map:
                    lat, lon = google_result.snap_map[i]
                    bridge_wps.append(
                        waypoints[i].model_copy(
                            update={"latitude": lat, "longitude": lon}
                        )
                    )
                else:
                    bridge_wps.append(waypoints[i])
            result = osrm.compute_route(bridge_wps)
            offset = dedup[0]
            for seg in result.segments:
                seg.start_index += offset
                seg.end_index += offset
            merged.extend(result.segments)
        except (RoutingError, httpx.HTTPError) as exc:
            logger.warning(
                "OSRM could not bridge unsnapped run %s: %s", run, exc
            )
            # Fall back to a straight-line connector so the route stays
            # continuous (the car never teleports) even if OSRM is down.
            if len(bridge_wps) >= 2:
                coords = [
                    (w.latitude, w.longitude) for w in bridge_wps
                ]
                merged.append(
                    RouteSegment(
                        start_index=dedup[0],
                        end_index=dedup[-1],
                        encoded_polyline=polyline.encode(coords, precision=5),
                        distance_m=0.0,
                        duration_s=0.0,
                    )
                )
            else:
                isolated.extend(run)
    merged.sort(key=lambda s: s.start_index)
    return merged, isolated


def compute_route(
    waypoints: Sequence[Waypoint],
    *,
    api_key: str | None = None,
    osrm_base_url: str | None = None,
    client: httpx.Client | None = None,
) -> RouteResult:
    """Route a waypoint list through the best available provider.

    Google is attempted first when a key is available; any failure falls
    back to OSRM. When Google succeeds but cannot snap some waypoints
    (remote roads it lacks), the unsnapped runs are bridged via OSRM so the
    route stays continuous. Raises :class:`RoutingError` when everything
    fails.
    """
    if len(waypoints) < 2:
        return RouteResult(provider="none", segments=[])

    key = api_key if api_key is not None else os.getenv("GOOGLE_MAPS_API_KEY") or ""
    base = (
        osrm_base_url
        or os.getenv("OSRM_BASE_URL")
        or DEFAULT_OSRM_URL
    )
    errors: list[str] = []

    google: GoogleRoutesProvider | None = None
    if key:
        google = GoogleRoutesProvider(key, client=client)
        try:
            result = google.compute_route(waypoints)
            if result.segments:
                if result.off_road_indices:
                    osrm = OSRMProvider(base, client=client)
                    try:
                        merged, isolated = _hybrid_merge(
                            waypoints, result, osrm
                        )
                        if merged:
                            return RouteResult(
                                provider="google+osrm",
                                segments=merged,
                                off_road_indices=isolated,
                            )
                    finally:
                        osrm.close()
                return result
            errors.append("Google returned no route segments")
        except (RoutingError, httpx.HTTPError) as exc:
            logger.warning("Google routing failed; falling back to OSRM: %s", exc)
            errors.append(str(exc))

    osrm = OSRMProvider(base, client=client)
    try:
        result = osrm.compute_route(waypoints)
        if result.segments:
            return result
        errors.append("OSRM returned no route segments")
    except (RoutingError, httpx.HTTPError) as exc:
        errors.append(f"OSRM routing failed: {exc}")
    finally:
        osrm.close()
        if google is not None:
            google.close()

    raise RoutingError("no routing provider succeeded: " + "; ".join(errors))


def reverse_geocode(
    latitude: float,
    longitude: float,
    *,
    api_key: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> str | None:
    """Best-effort reverse geocoding: Google first, Nominatim as fallback.

    Returns the full formatted address string.
    """
    detail = reverse_geocode_detail(
        latitude,
        longitude,
        api_key=api_key,
        user_agent=user_agent,
        client=client,
    )
    return detail["address"] if detail else None


# Name preference for address components (best first).
_GOOGLE_NAME_TYPES = (
    "establishment",
    "point_of_interest",
    "natural_feature",
    "locality",
    "postal_town",
    "administrative_area_level_3",
    "sublocality",
    "administrative_area_level_2",
    "route",
)
_NOMINATIM_NAME_KEYS = (
    "tourism",
    "attraction",
    "amenity",
    "village",
    "town",
    "city",
    "suburb",
    "hamlet",
    "road",
    "county",
)


def reverse_geocode_detail(
    latitude: float,
    longitude: float,
    *,
    api_key: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, str] | None:
    """Reverse geocode and return ``{"name": ..., "address": ...}``.

    ``name`` is the most human-friendly component (landmark, town, or
    village); ``address`` is the full formatted address.
    """
    key = api_key if api_key is not None else os.getenv("GOOGLE_MAPS_API_KEY") or ""
    ua = (
        user_agent
        or os.getenv("NOMINATIM_USER_AGENT")
        or DEFAULT_NOMINATIM_UA
    )
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        if key:
            try:
                resp = client.get(
                    _GOOGLE_GEOCODE_URL,
                    params={"latlng": f"{latitude},{longitude}", "key": key},
                )
                resp.raise_for_status()
                results = resp.json().get("results") or []
                if results and results[0].get("formatted_address"):
                    return _google_result(results[0])
            except httpx.HTTPError:
                logger.warning("Google geocoding failed; trying Nominatim")
        resp = client.get(
            _NOMINATIM_URL,
            params={
                "format": "jsonv2",
                "lat": latitude,
                "lon": longitude,
                "addressdetails": 1,
            },
            headers={"User-Agent": ua},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("display_name"):
            return None
        address = data.get("address") or {}
        name: str | None = None
        for key_name in _NOMINATIM_NAME_KEYS:
            if address.get(key_name):
                name = address[key_name]
                break
        return {
            "name": name or data["display_name"].split(",")[0],
            "address": data["display_name"],
        }
    except httpx.HTTPError as exc:
        logger.warning(
            "reverse geocoding failed for %.5f,%.5f: %s",
            latitude,
            longitude,
            _sanitize_message(str(exc)),
        )
        return None
    finally:
        if owns:
            client.close()


def _google_result(result: dict) -> dict[str, str]:
    address = result.get("formatted_address", "")
    name: str | None = None
    for component in result.get("address_components", []):
        types = component.get("types", [])
        if "plus_code" in types:
            continue  # skip Plus Codes like "9CP77234+XG"
        if any(t in _GOOGLE_NAME_TYPES for t in types):
            name = component.get("long_name")
            break
    if name is None or name.isdigit():
        # Fall back to the first non-Plus-Code part of the address.
        parts = [p.strip() for p in address.split(",") if p.strip()]
        name = next((p for p in parts if "+" not in p), parts[0] if parts else None)
    return {"name": name, "address": address}


# ------------------------------------------------------------------- helpers


def _chunks(seq: Sequence, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]

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

    def compute_route(self, waypoints: Sequence[Waypoint]) -> list[RouteSegment]: ...


@dataclass
class RouteResult:
    provider: str
    segments: list[RouteSegment] = field(default_factory=list)


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

    def compute_route(self, waypoints: Sequence[Waypoint]) -> list[RouteSegment]:
        if len(waypoints) < 2:
            return []
        snapped = self._snap_to_roads(waypoints)
        if not snapped:
            snapped = list(waypoints)  # snap failed — route the raw points
        return self._compute_routes(snapped)

    def _snap_to_roads(self, waypoints: Sequence[Waypoint]) -> list[Waypoint]:
        batches = _chunks(waypoints, _GOOGLE_SNAP_BATCH)
        found: list[tuple[int, float, float]] = []
        for batch in batches:
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
                found.append(
                    (int(idx), float(loc["latitude"]), float(loc["longitude"]))
                )
        if not found:
            return []
        found.sort(key=lambda t: t[0])
        snapped: list[Waypoint] = []
        for idx, lat, lon in found:
            if (
                snapped
                and snapped[-1].latitude == lat
                and snapped[-1].longitude == lon
            ):
                continue  # several photos can snap to the same road point
            base = waypoints[idx]
            snapped.append(
                base.model_copy(update={"latitude": lat, "longitude": lon})
            )
        return snapped

    def _compute_routes(
        self, waypoints: Sequence[Waypoint]
    ) -> list[RouteSegment]:
        segments: list[RouteSegment] = []
        i = 0
        n = len(waypoints)
        while i < n - 1:
            chunk = waypoints[i : i + _GOOGLE_ROUTE_WAYPOINTS]
            segment = self._route_chunk(chunk, start_offset=i)
            if segment:
                segments.append(segment)
            i += len(chunk) - 1  # next chunk reuses the shared endpoint
        return segments

    def _route_chunk(
        self, chunk: Sequence[Waypoint], *, start_offset: int
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
            start_index=start_offset,
            end_index=start_offset + len(chunk) - 1,
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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def compute_route(self, waypoints: Sequence[Waypoint]) -> list[RouteSegment]:
        if len(waypoints) < 2:
            return []
        segments: list[RouteSegment] = []
        i, n = 0, len(waypoints)
        while i < n - 1:
            chunk = waypoints[i : i + _OSRM_WAYPOINTS]
            coord_str = ";".join(
                f"{w.longitude:.6f},{w.latitude:.6f}" for w in chunk
            )
            resp = self._client.get(
                f"{self._base_url}/route/v1/driving/{coord_str}",
                params={"overview": "full", "geometries": "geojson"},
            )
            if resp.status_code != 200:
                raise RoutingError(
                    f"OSRM request failed ({resp.status_code}): "
                    f"{_sanitize_message(resp.text[:300])}"
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
        return segments


# -------------------------------------------------------------- orchestration


def compute_route(
    waypoints: Sequence[Waypoint],
    *,
    api_key: str | None = None,
    osrm_base_url: str | None = None,
    client: httpx.Client | None = None,
) -> RouteResult:
    """Route a waypoint list through the best available provider.

    Google is attempted first when a key is available; any failure falls
    back to OSRM. Raises :class:`RoutingError` when everything fails.
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
            segments = google.compute_route(waypoints)
            if segments:
                return RouteResult(provider=google.name, segments=segments)
            errors.append("Google returned no route segments")
        except (RoutingError, httpx.HTTPError) as exc:
            logger.warning("Google routing failed; falling back to OSRM: %s", exc)
            errors.append(str(exc))

    osrm = OSRMProvider(base, client=client)
    try:
        segments = osrm.compute_route(waypoints)
        if segments:
            return RouteResult(provider=osrm.name, segments=segments)
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
        if any(t in _GOOGLE_NAME_TYPES for t in component.get("types", [])):
            name = component.get("long_name")
            break
    if name is None or name.isdigit():
        name = address.split(",")[0]
    return {"name": name, "address": address}


# ------------------------------------------------------------------- helpers


def _chunks(seq: Sequence, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]

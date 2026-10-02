"""Unit tests for route_recap.core.router (mocked HTTP transports)."""

import json
from datetime import datetime, timedelta

import httpx
import polyline
import pytest

from route_recap.core.models import Waypoint
from route_recap.core.router import (
    DEFAULT_OSRM_URL,
    GoogleRoutesProvider,
    OSRMProvider,
    RoutingError,
    _chunks,
    compute_route,
    reverse_geocode,
)


def wp(i: int) -> Waypoint:
    return Waypoint(
        latitude=63.4 + i * 0.001,
        longitude=-19.0 + i * 0.001,
        timestamp=datetime(2026, 8, 14, 9, 0) + timedelta(minutes=i),
    )


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_chunks():
    assert [list(c) for c in _chunks(list(range(5)), 2)] == [[0, 1], [2, 3], [4]]


def test_google_chunks_route_requests():
    points = [wp(i) for i in range(30)]
    calls = {"snap": 0, "routes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "snapToRoads" in url:
            calls["snap"] += 1
            n = len(request.url.params["path"].split("|"))  # decoded params
            return httpx.Response(
                200,
                json={
                    "snappedPoints": [
                        {
                            "location": {
                                "latitude": 63.4 + i * 0.001,
                                "longitude": -19.0 + i * 0.001,
                            },
                            "originalIndex": i,
                        }
                        for i in range(n)
                    ]
                },
            )
        if "computeRoutes" in url:
            calls["routes"] += 1
            body = json.loads(request.content)
            n = 1 + len(body.get("intermediates", [])) + 1
            encoded = polyline.encode(
                [(63.4 + j * 0.001, -19.0 + j * 0.001) for j in range(n)]
            )
            return httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "distanceMeters": n * 1000,
                            "duration": "600s",
                            "polyline": {"encodedPolyline": encoded},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    provider = GoogleRoutesProvider("test-key", client=make_client(handler))
    try:
        result = provider.compute_route(points)
    finally:
        provider.close()

    assert calls["snap"] == 1
    assert calls["routes"] == 2  # 30 points → chunks of 25 and 6
    segments = result.segments
    assert [(s.start_index, s.end_index) for s in segments] == [
        (0, 24),
        (24, 29),
    ]
    assert segments[0].distance_m == 25000.0
    assert segments[0].duration_s == 600.0
    assert result.off_road_indices == []  # every point snapped


def test_google_offroad_points_excluded_from_route():
    """Points far from a road are routed around, not through."""
    points = [wp(0), wp(1), wp(2)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "snapToRoads" in str(request.url):
            # Snap only the first and last points; middle one is dropped.
            return httpx.Response(
                200,
                json={
                    "snappedPoints": [
                        {
                            "location": {"latitude": 63.4005, "longitude": -19.0005},
                            "originalIndex": 0,
                        },
                        {
                            "location": {"latitude": 63.4025, "longitude": -18.9985},
                            "originalIndex": 2,
                        },
                    ]
                },
            )
        body = json.loads(request.content)
        n = 1 + len(body.get("intermediates", [])) + 1
        return httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "distanceMeters": n * 1000,
                        "duration": "600s",
                        "polyline": {"encodedPolyline": polyline.encode(
                            [(63.4 + j * 0.001, -19.0 + j * 0.001) for j in range(n)]
                        )},
                    }
                ]
            },
        )

    provider = GoogleRoutesProvider("test-key", client=make_client(handler))
    try:
        result = provider.compute_route(points)
    finally:
        provider.close()

    # The off-road middle point is excluded from the route but reported.
    assert result.off_road_indices == [1]
    assert len(result.segments) == 1
    assert (result.segments[0].start_index, result.segments[0].end_index) == (0, 2)
    assert result.segments[0].distance_m == 2000.0  # routed 0 → 2 directly


def test_google_routes_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="API key not valid")

    provider = GoogleRoutesProvider("bad-key", client=make_client(handler))
    with pytest.raises(RoutingError):
        provider.compute_route([wp(0), wp(1), wp(2)])


def test_osrm_geometry_to_polyline_roundtrip():
    coords = [[-19.0, 63.4], [-19.001, 63.401], [-19.002, 63.402]]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "route/v1/driving" in str(request.url)
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "geometry": {"type": "LineString", "coordinates": coords},
                        "distance": 1234.5,
                        "duration": 300.0,
                    }
                ],
            },
        )

    provider = OSRMProvider(client=make_client(handler))
    result = provider.compute_route([wp(0), wp(1)])
    segments = result.segments
    assert len(segments) == 1
    assert result.off_road_indices == []  # OSRM cannot classify
    assert segments[0].distance_m == 1234.5
    assert segments[0].duration_s == 300.0
    decoded = polyline.decode(segments[0].encoded_polyline)
    assert decoded[0] == pytest.approx((63.4, -19.0))
    assert len(decoded) == 3


def test_compute_route_falls_back_to_osrm_on_google_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "googleapis.com" in url:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-19.0, 63.4], [-19.001, 63.401]],
                        },
                        "distance": 1000.0,
                        "duration": 100.0,
                    }
                ],
            },
        )

    client = make_client(handler)
    result = compute_route([wp(0), wp(1)], api_key="bad-key", client=client)
    assert result.provider == "osrm"
    assert len(result.segments) == 1


def test_compute_route_no_key_uses_osrm(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "googleapis.com" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-19.0, 63.4], [-19.001, 63.401]],
                        },
                        "distance": 1000.0,
                        "duration": 100.0,
                    }
                ],
            },
        )

    result = compute_route([wp(0), wp(1)], client=make_client(handler))
    assert result.provider == "osrm"


def test_compute_route_single_point_returns_none_provider():
    result = compute_route([wp(0)])
    assert result.provider == "none"
    assert result.segments == []


def test_reverse_geocode_google_first():
    def handler(request: httpx.Request) -> httpx.Response:
        if "maps.googleapis.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "formatted_address": "Skógafoss, Iceland",
                            "address_components": [
                                {
                                    "long_name": "Skógafoss",
                                    "types": ["natural_feature"],
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"display_name": "nominatim-name"})

    addr = reverse_geocode(63.532, -19.511, api_key="k", client=make_client(handler))
    assert addr == "Skógafoss, Iceland"


def test_reverse_geocode_nominatim_user_agent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json={"display_name": "Vík, Iceland"})

    addr = reverse_geocode(63.4186, -19.0060, client=make_client(handler))
    assert addr == "Vík, Iceland"
    assert seen["ua"] == "route-recap/0.1 (personal use)"


def test_reverse_geocode_detail_nominatim_name_preference():
    """House numbers must not become the stop name."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("addressdetails") == "1"
        return httpx.Response(
            200,
            json={
                "display_name": "10, Austurvegur, Vík, Suðurland, 870, Ísland",
                "address": {
                    "house_number": "10",
                    "road": "Austurvegur",
                    "village": "Vík",
                },
            },
        )

    from route_recap.core.router import reverse_geocode_detail

    detail = reverse_geocode_detail(63.4186, -19.0060, client=make_client(handler))
    assert detail["name"] == "Vík"
    assert detail["address"].startswith("10, Austurvegur")


def test_reverse_geocode_detail_google_component():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "formatted_address": "Skógafoss, 861 Hvolsvöllur, Iceland",
                        "address_components": [
                            {
                                "long_name": "Skógafoss",
                                "types": ["establishment"],
                            }
                        ],
                    }
                ]
            },
        )

    from route_recap.core.router import reverse_geocode_detail

    detail = reverse_geocode_detail(
        63.532, -19.511, api_key="k", client=make_client(handler)
    )
    assert detail["name"] == "Skógafoss"


def test_osrm_default_base_url():
    provider = OSRMProvider()
    assert provider._base_url == DEFAULT_OSRM_URL
    provider.close()

"""Unit tests for route_recap.core.router (mocked HTTP transports)."""

import json
from datetime import datetime, timedelta

import httpx
import polyline
import pytest

from route_recap.core.models import RouteSegment, Waypoint
from route_recap.core.router import (
    DEFAULT_OSRM_URL,
    GoogleRoutesProvider,
    OSRMProvider,
    RouteResult,
    RoutingError,
    _chunks,
    _hybrid_merge,
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

    # Single snapped points form runs of length 1, which Google cannot route
    # on their own — the middle point is off-road, and the route is empty
    # (the orchestration bridges/falls back at a higher level).
    assert result.off_road_indices == [1]
    assert result.segments == []


def _osrm_ok_handler():
    """OSRM mock that routes any coordinate list with a straight polyline."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "route/v1/driving" in str(request.url)
        coords = request.url.path.split("/driving/")[1].split(";")
        pts = [[float(c.split(",")[0]), float(c.split(",")[1])] for c in coords]
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "geometry": {"type": "LineString", "coordinates": pts},
                        "distance": 1000.0 * (len(pts) - 1),
                        "duration": 100.0 * (len(pts) - 1),
                    }
                ],
            },
        )

    return handler


def test_hybrid_merge_bridges_unsnapped_run():
    """A consecutive run Google can't snap is routed via OSRM and merged."""
    points = [wp(i) for i in range(8)]
    google_segments = [
        RouteSegment(start_index=0, end_index=2, encoded_polyline="a", distance_m=1000, duration_s=60),
        RouteSegment(start_index=6, end_index=7, encoded_polyline="b", distance_m=1000, duration_s=60),
    ]
    google_result = RouteResult(
        provider="google", segments=google_segments, off_road_indices=[3, 4, 5]
    )
    osrm = OSRMProvider(client=make_client(_osrm_ok_handler()))
    try:
        merged, isolated = _hybrid_merge(points, google_result, osrm)
    finally:
        osrm.close()

    assert isolated == []  # the run was bridged, nothing left off-road
    # The bridge includes boundary points 2 and 6, so the route is continuous
    # and no distance is double-counted.
    assert [(s.start_index, s.end_index) for s in merged] == [
        (0, 2),
        (2, 6),  # OSRM bridge: 2 → 3 → 4 → 5 → 6
        (6, 7),
    ]


def test_hybrid_merge_bridges_single_points_between_routed():
    """Single unsnapped points between routed neighbors are bridged, not holes."""
    points = [wp(i) for i in range(6)]
    google_segments = [
        RouteSegment(start_index=0, end_index=2, encoded_polyline="a", distance_m=1000, duration_s=60),
        RouteSegment(start_index=4, end_index=5, encoded_polyline="b", distance_m=1000, duration_s=60),
    ]
    google_result = RouteResult(
        provider="google", segments=google_segments, off_road_indices=[1, 3]
    )
    osrm = OSRMProvider(client=make_client(_osrm_ok_handler()))
    try:
        merged, isolated = _hybrid_merge(points, google_result, osrm)
    finally:
        osrm.close()

    # Both single points are bridged (0→1→2 and 2→3→4) — no holes.
    assert isolated == []
    assert [(s.start_index, s.end_index) for s in merged] == [
        (0, 2),
        (0, 2),  # bridge 0→1→2
        (2, 4),  # bridge 2→3→4
        (4, 5),
    ]


def test_hybrid_merge_keeps_boundary_points_off_road():
    """Unsnapped points at the trip start/end have nothing to connect to."""
    points = [wp(i) for i in range(6)]
    google_segments = [
        RouteSegment(start_index=2, end_index=3, encoded_polyline="a", distance_m=1000, duration_s=60),
        RouteSegment(start_index=4, end_index=5, encoded_polyline="b", distance_m=1000, duration_s=60),
    ]
    google_result = RouteResult(
        provider="google", segments=google_segments, off_road_indices=[0, 1]
    )
    osrm = OSRMProvider(client=make_client(_osrm_ok_handler()))
    try:
        merged, isolated = _hybrid_merge(points, google_result, osrm)
    finally:
        osrm.close()

    assert isolated == [0, 1]  # no previous neighbor — can't bridge
    assert [(s.start_index, s.end_index) for s in merged] == [(2, 3), (4, 5)]


def test_hybrid_merge_osrm_failure_keeps_route_continuous():
    """If OSRM can't bridge a run, a straight-line connector keeps it joined."""
    points = [wp(i) for i in range(6)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="osrm down")

    google_segments = [
        RouteSegment(start_index=0, end_index=1, encoded_polyline="a", distance_m=1000, duration_s=60),
        RouteSegment(start_index=4, end_index=5, encoded_polyline="b", distance_m=1000, duration_s=60),
    ]
    google_result = RouteResult(
        provider="google", segments=google_segments, off_road_indices=[2, 3]
    )
    osrm = OSRMProvider(client=make_client(handler), retries=1)
    try:
        merged, isolated = _hybrid_merge(points, google_result, osrm)
    finally:
        osrm.close()

    # The run is bridged with a straight-line connector — route stays joined.
    assert isolated == []
    assert [(s.start_index, s.end_index) for s in merged] == [
        (0, 1),
        (1, 4),  # straight-line connector across the failed OSRM bridge
        (4, 5),
    ]
    # The connector is a real polyline between the boundary points.
    connector = merged[1]
    decoded = polyline.decode(connector.encoded_polyline)
    assert decoded[0] == pytest.approx((63.401, -18.999))  # snapped-ish boundary
    assert decoded[-1] == pytest.approx((63.404, -18.996))


def test_hybrid_merge_uses_snapped_boundary_coords():
    """Bridge boundaries use Google's SNAPPED coords so the route is seamless."""
    points = [wp(i) for i in range(6)]
    google_segments = [
        RouteSegment(start_index=0, end_index=1, encoded_polyline="a", distance_m=1000, duration_s=60),
        RouteSegment(start_index=4, end_index=5, encoded_polyline="b", distance_m=1000, duration_s=60),
    ]
    # Google snapped indices 0,1,4,5 — boundary points 1 and 4 are snapped.
    snap_map = {
        0: (63.4001, -19.0001),
        1: (63.4011, -18.9991),
        4: (63.4041, -18.9961),
        5: (63.4051, -18.9951),
    }
    google_result = RouteResult(
        provider="google",
        segments=google_segments,
        off_road_indices=[2, 3],
        snap_map=snap_map,
    )
    seen_coords = {}

    def handler(request: httpx.Request) -> httpx.Response:
        coords = request.url.path.split("/driving/")[1].split(";")
        seen_coords["bridge"] = coords
        pts = [[float(c.split(",")[0]), float(c.split(",")[1])] for c in coords]
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "geometry": {"type": "LineString", "coordinates": pts},
                        "distance": 1000.0 * (len(pts) - 1),
                        "duration": 100.0 * (len(pts) - 1),
                    }
                ],
            },
        )

    osrm = OSRMProvider(client=make_client(handler))
    try:
        merged, isolated = _hybrid_merge(points, google_result, osrm)
    finally:
        osrm.close()

    assert isolated == []
    # Bridge = [1(snapped), 2, 3, 4(snapped)] — boundaries use snapped coords.
    assert seen_coords["bridge"] == [
        "-18.999100,63.401100",  # snapped index 1
        "-18.998000,63.402000",  # raw index 2 (wp: -19.0 + 0.002)
        "-18.997000,63.403000",  # raw index 3 (wp: -19.0 + 0.003)
        "-18.996100,63.404100",  # snapped index 4
    ]
    assert [(s.start_index, s.end_index) for s in merged] == [(0, 1), (1, 4), (4, 5)]


def test_compute_route_hybrid_provider():
    """End-to-end: Google snaps some points, OSRM bridges the rest."""
    points = [wp(i) for i in range(6)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "snapToRoads" in url:
            # Google only has roads for indices 0,1 and 4,5
            return httpx.Response(
                200,
                json={
                    "snappedPoints": [
                        {
                            "location": {"latitude": 63.4, "longitude": -19.0},
                            "originalIndex": 0,
                        },
                        {
                            "location": {"latitude": 63.401, "longitude": -18.999},
                            "originalIndex": 1,
                        },
                        {
                            "location": {"latitude": 63.404, "longitude": -18.996},
                            "originalIndex": 4,
                        },
                        {
                            "location": {"latitude": 63.405, "longitude": -18.995},
                            "originalIndex": 5,
                        },
                    ]
                },
            )
        if "computeRoutes" in url:
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
        return _osrm_ok_handler()(request)

    result = compute_route(points, api_key="test-key", client=make_client(handler))
    assert result.provider == "google+osrm"
    assert result.off_road_indices == []  # the fjord run was bridged
    # Google routes each snapped run separately (0→1, 4→5); OSRM bridges the
    # unsnapped run with its boundary points (1→2→3→4). No double-counting.
    assert [(s.start_index, s.end_index) for s in result.segments] == [
        (0, 1),
        (1, 4),
        (4, 5),
    ]


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


def test_reverse_geocode_detail_skips_plus_codes():
    """Remote locations return Plus Codes — they must not become the name."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "formatted_address": "9CP77234+XG, Vesturbyggð, Iceland",
                        "address_components": [
                            {
                                "long_name": "9CP77234+XG",
                                "types": ["plus_code"],
                            },
                            {
                                "long_name": "Vesturbyggð",
                                "types": ["locality"],
                            },
                        ],
                    }
                ]
            },
        )

    from route_recap.core.router import reverse_geocode_detail

    detail = reverse_geocode_detail(
        65.5, -23.9, api_key="k", client=make_client(handler)
    )
    assert detail["name"] == "Vesturbyggð"


def test_osrm_default_base_url():
    provider = OSRMProvider()
    assert provider._base_url == DEFAULT_OSRM_URL
    provider.close()

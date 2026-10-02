"""Pydantic schemas for route-recap: media metadata, waypoints, stops, routing."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

KM_PER_MILE = 1.609344


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class DistanceUnit(str, Enum):
    KM = "km"
    MI = "mi"


def _validate_latitude(value: float) -> float:
    if not -90.0 <= value <= 90.0:
        raise ValueError(f"latitude out of range: {value}")
    return value


def _validate_longitude(value: float) -> float:
    if not -180.0 <= value <= 180.0:
        raise ValueError(f"longitude out of range: {value}")
    return value


class MediaMetadata(BaseModel):
    """Metadata extracted from a single travel media file.

    GPS fields may be ``None`` for files without geotags — the pipeline
    skips those gracefully instead of failing.
    """

    path: Path
    media_type: MediaType
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    datetime_original: datetime | None = None
    make: str | None = None
    model: str | None = None
    file_size_bytes: int | None = None

    @field_validator("latitude")
    @classmethod
    def _lat_ok(cls, v: float | None) -> float | None:
        return v if v is None else _validate_latitude(v)

    @field_validator("longitude")
    @classmethod
    def _lon_ok(cls, v: float | None) -> float | None:
        return v if v is None else _validate_longitude(v)

    @property
    def has_gps(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class Waypoint(BaseModel):
    """A chronological location along the trip.

    One waypoint may aggregate several media files after deduplication.
    """

    latitude: float
    longitude: float
    altitude_m: float | None = None
    timestamp: datetime | None = None
    media_count: int = Field(default=1, ge=1)
    source_files: list[Path] = Field(default_factory=list)
    is_stop: bool = False
    stop_index: int | None = None  # index into TripSummary.stops when is_stop

    @field_validator("latitude")
    @classmethod
    def _lat_ok(cls, v: float) -> float:
        return _validate_latitude(v)

    @field_validator("longitude")
    @classmethod
    def _lon_ok(cls, v: float) -> float:
        return _validate_longitude(v)


class Stop(BaseModel):
    """A significant pause identified by the clustering engine."""

    name: str | None = None
    address: str | None = None
    latitude: float
    longitude: float
    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    duration_s: int = Field(default=0, ge=0)
    media_count: int = Field(default=0, ge=0)

    @field_validator("latitude")
    @classmethod
    def _lat_ok(cls, v: float) -> float:
        return _validate_latitude(v)

    @field_validator("longitude")
    @classmethod
    def _lon_ok(cls, v: float) -> float:
        return _validate_longitude(v)


class RouteSegment(BaseModel):
    """A snapped road segment between consecutive waypoints.

    ``start_index`` / ``end_index`` reference positions in the (deduplicated)
    waypoint list the segment covers.
    """

    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)
    encoded_polyline: str = ""
    distance_m: float = Field(default=0.0, ge=0)
    duration_s: float = Field(default=0.0, ge=0)


class TripConfig(BaseModel):
    """User-supplied options for a pipeline run."""

    trip_name: str = "Untitled Trip"
    input_dir: Path = Path("data")
    distance_unit: DistanceUnit = DistanceUnit.KM
    min_stop_minutes: int = Field(default=30, ge=5)
    dedup_radius_m: float = Field(default=50.0, ge=0)
    dedup_window_minutes: int = Field(default=5, ge=1)
    stop_max_distance_m: float = Field(default=500.0, ge=0)
    overnight_hours: int = Field(default=6, ge=2)
    geocode_stops: bool = True


class TripSummary(BaseModel):
    """Final report of a trip: stats, waypoints, stops, and route segments."""

    trip_name: str
    unit: DistanceUnit = DistanceUnit.KM
    started_at: datetime | None = None
    ended_at: datetime | None = None
    distance_km: float = Field(default=0.0, ge=0)
    driving_duration_s: float = Field(default=0.0, ge=0)
    stop_duration_s: float = Field(default=0.0, ge=0)
    media_processed: int = Field(default=0, ge=0)
    media_with_gps: int = Field(default=0, ge=0)
    waypoint_count: int = Field(default=0, ge=0)
    stop_count: int = Field(default=0, ge=0)
    routing_provider: str | None = None
    waypoints: list[Waypoint] = Field(default_factory=list)
    stops: list[Stop] = Field(default_factory=list)
    segments: list[RouteSegment] = Field(default_factory=list)

    @property
    def distance_mi(self) -> float:
        return self.distance_km / KM_PER_MILE

    def display_distance(self) -> str:
        if self.unit is DistanceUnit.MI:
            return f"{self.distance_mi:.1f} mi"
        return f"{self.distance_km:.1f} km"

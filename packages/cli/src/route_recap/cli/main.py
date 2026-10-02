"""Interactive CLI for route-recap: travel media in, HTML map report out."""

from __future__ import annotations

import logging
import os
import re
import time
import webbrowser
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

from route_recap.core import (
    DistanceUnit,
    MediaExtractor,
    RoutingError,
    TripConfig,
    TripSummary,
    Waypoint,
    compute_route,
    deduplicate,
    detect_stops,
    filter_outliers,
    reverse_geocode_detail,
)
from route_recap.generator import build_html

load_dotenv()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    help="Reconstruct a continuous road-trip route from geotagged iPhone media.",
)
console = Console()
logger = logging.getLogger("route-recap")


# ------------------------------------------------------------------- config


def _prompt_name() -> str:
    return Prompt.ask("Trip name", default="Untitled Trip")


def _prompt_input_dir() -> Path:
    raw = Prompt.ask("Media folder", default=str(Path("data")))
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        console.print(f"[red]Folder not found: {path}[/red]")
        raise typer.Exit(1)
    return path


def _prompt_unit() -> DistanceUnit:
    choice = Prompt.ask("Distance unit", choices=["km", "mi"], default="km")
    return DistanceUnit(choice)


def _prompt_min_stop() -> int:
    return int(Prompt.ask("Minimum stop duration (minutes)", default="30"))


def _prompt_geocode() -> bool:
    return Confirm.ask("Reverse-geocode stops into named landmarks?", default=True)


def _gather_config(
    name: str | None,
    input_dir: Path | None,
    unit: DistanceUnit | None,
    min_stop_minutes: int | None,
    no_geocode: bool,
    no_filter_outliers: bool,
) -> TripConfig:
    trip_name = name or _prompt_name()
    folder = input_dir if input_dir is not None else _prompt_input_dir()
    unit = unit if unit is not None else _prompt_unit()
    mins = min_stop_minutes if min_stop_minutes is not None else _prompt_min_stop()
    geocode = False if no_geocode else _prompt_geocode()
    return TripConfig(
        trip_name=trip_name,
        input_dir=folder,
        distance_unit=unit,
        min_stop_minutes=mins,
        geocode_stops=geocode,
        filter_outliers=not no_filter_outliers,
    )


# ------------------------------------------------------------------ pipeline


def _run_pipeline(config: TripConfig) -> TripSummary:
    extractor = MediaExtractor()
    files = extractor.list_media(config.input_dir)
    if not files:
        console.print(
            f"[yellow]No supported media (HEIC/JPEG/MOV/MP4) found in "
            f"{config.input_dir}[/yellow]"
        )
        raise typer.Exit(1)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting GPS metadata", total=len(files))
        result = extractor.extract_files(
            files, on_file=lambda _p: progress.advance(task)
        )

    gps_items = [m for m in result.items if m.has_gps]
    if not gps_items:
        console.print(
            f"[yellow]Scanned {result.total_files} file(s) but none carried GPS "
            f"data — nothing to map.[/yellow]"
        )
        raise typer.Exit(1)

    waypoints = [
        Waypoint(
            latitude=m.latitude,
            longitude=m.longitude,
            altitude_m=m.altitude_m,
            timestamp=m.datetime_original,
            media_count=1,
            source_files=[m.path],
        )
        for m in gps_items
    ]

    excluded_media = 0
    if config.filter_outliers:
        with console.status("Filtering outlier waypoints..."):
            kept, excluded = filter_outliers(
                waypoints,
                gap_minutes=config.outlier_gap_minutes,
                speed_kmh=config.outlier_speed_kmh,
            )
        if excluded:
            excluded_media = sum(w.media_count for w in excluded)
            console.print(
                f"[dim]Excluded {len(excluded)} waypoint(s) "
                f"({excluded_media} photo(s)) that look like a different trip "
                f"(home/airport photos or another country). "
                f"Use --no-filter-outliers to keep them.[/dim]"
            )
        waypoints = kept

    with console.status("Clustering waypoints..."):
        waypoints = deduplicate(
            waypoints,
            radius_m=config.dedup_radius_m,
            window_minutes=config.dedup_window_minutes,
        )
        waypoints, stops = detect_stops(
            waypoints,
            min_stop_minutes=config.min_stop_minutes,
            stop_max_distance_m=config.stop_max_distance_m,
            overnight_hours=config.overnight_hours,
        )

    route_provider = "none"
    segments = []
    with console.status("Reconstructing the route..."):
        try:
            route_result = compute_route(waypoints)
            route_provider = route_result.provider
            segments = route_result.segments
            for idx in route_result.off_road_indices:
                if 0 <= idx < len(waypoints):
                    waypoints[idx].off_road = True
            if route_result.off_road_indices:
                console.print(
                    f"[dim]{len(route_result.off_road_indices)} waypoint(s) too far "
                    f"from roads — kept as separate POIs.[/dim]"
                )
        except RoutingError as exc:
            console.print(f"[yellow]Routing failed: {exc}[/yellow]")
            console.print("[yellow]Report will be generated without a route line.[/yellow]")

    if config.geocode_stops and stops:
        uses_nominatim = not os.getenv("GOOGLE_MAPS_API_KEY")
        with console.status("Naming stops..."):
            for i, stop in enumerate(stops):
                try:
                    if uses_nominatim and i > 0:
                        time.sleep(1.1)  # Nominatim usage policy: ≤ 1 req/s
                    detail = reverse_geocode_detail(stop.latitude, stop.longitude)
                    if detail:
                        stop.name = detail["name"]
                        stop.address = detail["address"]
                except Exception:
                    logger.exception("geocoding failed for stop %d", i)

    timestamps = [w.timestamp for w in waypoints if w.timestamp is not None]
    summary = TripSummary(
        trip_name=config.trip_name,
        unit=config.distance_unit,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        distance_km=sum(s.distance_m for s in segments) / 1000.0,
        driving_duration_s=sum(s.duration_s for s in segments),
        stop_duration_s=sum(s.duration_s for s in stops),
        media_processed=result.total_files,
        media_with_gps=result.with_gps,
        media_excluded=excluded_media,
        waypoint_count=len(waypoints),
        stop_count=len(stops),
        routing_provider=route_provider,
        waypoints=waypoints,
        stops=stops,
        segments=segments,
    )
    return summary


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip().replace(" ", "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "trip"


def _show_summary(summary: TripSummary, output_path: Path) -> None:
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("Distance", summary.display_distance())
    table.add_row("Driving time", f"{summary.driving_duration_s / 3600:.1f} h")
    table.add_row("Stop time", f"{summary.stop_duration_s / 3600:.1f} h")
    table.add_row("Stops", str(summary.stop_count))
    table.add_row("Off-road points", str(summary.off_road_count))
    table.add_row("Media", f"{summary.media_processed} (GPS: {summary.media_with_gps})")
    if summary.media_excluded:
        table.add_row("Excluded outliers", f"{summary.media_excluded} photo(s)")
    table.add_row("Route points", str(summary.waypoint_count))
    table.add_row("Router", summary.routing_provider or "none")
    console.print(Panel(table, title=f"[bold]{summary.trip_name}[/bold]", expand=False))
    console.print(f"[green]Report written to[/green] {output_path}")


# ------------------------------------------------------------------- command


@app.command()
def main(
    name: Annotated[
        Optional[str], typer.Option("--name", "-n", help="Trip name")
    ] = None,
    input_dir: Annotated[
        Optional[Path],
        typer.Option("--input-dir", "-i", help="Folder containing trip media"),
    ] = None,
    unit: Annotated[
        Optional[DistanceUnit],
        typer.Option("--unit", "-u", help="Distance unit: km or mi"),
    ] = None,
    min_stop_minutes: Annotated[
        Optional[int],
        typer.Option("--min-stop-minutes", help="Minimum stop duration (minutes)"),
    ] = None,
    no_geocode: Annotated[
        bool, typer.Option("--no-geocode", help="Skip reverse geocoding of stops")
    ] = False,
    no_filter_outliers: Annotated[
        bool,
        typer.Option(
            "--no-filter-outliers",
            help="Keep waypoints from a different trip (home/airport/foreign photos)",
        ),
    ] = False,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Do not open the report in the browser")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Verbose logging")
    ] = False,
) -> None:
    """Reconstruct a road-trip route from geotagged media and render an HTML map."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = _gather_config(
        name, input_dir, unit, min_stop_minutes, no_geocode, no_filter_outliers
    )
    console.print(
        Panel.fit(
            f"Input: [bold]{config.input_dir}[/bold]\n"
            f"Unit: {config.distance_unit.value} · "
            f"min stop: {config.min_stop_minutes} min",
            title=f"[bold]{config.trip_name}[/bold]",
            subtitle="route-recap",
        )
    )

    summary = _run_pipeline(config)

    output_dir = Path("output") / _slug(config.trip_name)
    carto_key = os.getenv("CARTO_BASEMAP_KEY") or None
    output_path = build_html(summary, output_dir, carto_key=carto_key)
    _show_summary(summary, output_path)
    if not carto_key:
        console.print(
            "[dim]Tip: set CARTO_BASEMAP_KEY in .env to use CARTO basemap tiles.[/dim]"
        )

    if not no_open and Confirm.ask("Open the report in your browser?", default=True):
        webbrowser.open(output_path.resolve().as_uri())


if __name__ == "__main__":
    app()

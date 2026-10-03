"""Interactive CLI for route-recap: travel media in, HTML map report out."""

from __future__ import annotations

import functools
import http.server
import logging
import os
import re
import socket
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    Stop,
    TripConfig,
    TripSummary,
    Waypoint,
    assign_days,
    compute_route,
    deduplicate,
    detect_stops,
    filter_outliers,
    reverse_geocode_detail,
)
from route_recap.generator import build_html, build_single_file, stage_media

#: ``load_dotenv()`` is deliberately NOT called at import time — importing a
#: module should not mutate process-wide state (it leaked a real API key into
#: any process that merely imported the CLI, e.g. the test suite). The entry
#: point below loads ``.env`` when a pipeline run actually starts.

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    invoke_without_command=True,
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
    no_media: bool,
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
        stage_media=not no_media,
    )


# ------------------------------------------------------------------ pipeline

_GEOCODE_WORKERS = 6  # well under Google Geocoding's default 50 QPS quota


def _geocode_apply(stop: Stop, detail: dict | None) -> None:
    if detail:
        stop.name = detail["name"]
        stop.address = detail["address"]


def _geocode_stops_sequential(stops: list[Stop]) -> None:
    """Nominatim fallback — its usage policy allows ~1 request per second."""
    for i, stop in enumerate(stops):
        try:
            if i > 0:
                time.sleep(1.1)
            _geocode_apply(stop, reverse_geocode_detail(stop.latitude, stop.longitude))
        except Exception:
            logger.exception("geocoding failed for stop %d", i)


def _geocode_stops_parallel(stops: list[Stop]) -> None:
    """Google geocoding — parallelize so hundreds of stops finish in seconds."""

    def work(item: tuple[int, Stop]) -> tuple[int, dict | None]:
        i, stop = item
        try:
            return i, reverse_geocode_detail(stop.latitude, stop.longitude)
        except Exception:
            logger.exception("geocoding failed for stop %d", i)
            return i, None

    with ThreadPoolExecutor(max_workers=_GEOCODE_WORKERS) as pool:
        futures = [pool.submit(work, (i, s)) for i, s in enumerate(stops)]
        for future in as_completed(futures):
            i, detail = future.result()
            _geocode_apply(stops[i], detail)


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
                    f"from any road — kept as separate POIs.[/dim]"
                )
            if route_result.provider == "google+osrm":
                console.print(
                    "[dim]Some waypoints were on roads Google lacks — "
                    "bridged via OSRM (OpenStreetMap) so the route stays "
                    "continuous.[/dim]"
                )
        except RoutingError as exc:
            console.print(f"[yellow]Routing failed: {exc}[/yellow]")
            console.print("[yellow]Report will be generated without a route line.[/yellow]")

    if segments:
        segments = assign_days(waypoints, segments)

    if config.geocode_stops and stops:
        uses_nominatim = not os.getenv("GOOGLE_MAPS_API_KEY")
        with console.status("Naming stops..."):
            if uses_nominatim:
                _geocode_stops_sequential(stops)
            else:
                _geocode_stops_parallel(stops)

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


def _lan_ipv4_addresses() -> list[str]:
    """Local IPv4 addresses a phone on the same Wi-Fi can reach.

    Loopback is useless to another device, so it is filtered out. The UDP
    "connect" only asks the OS which interface it *would* use for the internet
    — no packet is sent (TEST-NET-1 is unroutable by design).
    """
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))
        addrs.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(a for a in addrs if not a.startswith("127."))


def _show_summary(summary: TripSummary, output_path: Path) -> None:
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("Distance", summary.display_distance())
    table.add_row("Driving time", f"{summary.driving_duration_s / 3600:.1f} h")
    table.add_row("Stop time", f"{summary.stop_duration_s / 3600:.1f} h")
    table.add_row("Stops", str(summary.stop_count))
    if summary.day_count:
        table.add_row("Days", str(summary.day_count))
    table.add_row("Off-road points", str(summary.off_road_count))
    table.add_row("Media", f"{summary.media_processed} (GPS: {summary.media_with_gps})")
    if summary.media_excluded:
        table.add_row("Excluded outliers", f"{summary.media_excluded} photo(s)")
    table.add_row("Route points", str(summary.waypoint_count))
    table.add_row("Router", summary.routing_provider or "none")
    console.print(Panel(table, title=f"[bold]{summary.trip_name}[/bold]", expand=False))
    console.print(f"[green]Report written to[/green] {output_path}")


def _export_single_file(
    summary: TripSummary,
    output_dir: Path,
    *,
    carto_key: str | None,
    assets: dict[str, dict],
    embed_photos: bool,
    max_embed_mb: float,
    inline_libraries: bool,
    photo_size: int,
    photo_quality: int,
) -> tuple[Path, list[str]]:
    """Write the shareable ``journey.html``.

    Kept as a named helper so the option names the CLI exposes are mapped to
    the builder's parameters in exactly one place — an earlier mismatch between
    the two raised ``TypeError`` the first time the flag was actually used.
    """
    return build_single_file(
        summary,
        output_dir,
        carto_key=carto_key,
        assets=assets,
        embed=embed_photos,
        max_embed_mb=max_embed_mb,
        thumb_size=photo_size,
        thumb_quality=photo_quality,
        inline_libraries=inline_libraries,
    )


# ------------------------------------------------------------------- command


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
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
    no_media: Annotated[
        bool,
        typer.Option(
            "--no-media",
            help="Skip staging media/thumbnails into the report folder",
        ),
    ] = False,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Do not open the report in the browser")
    ] = False,
    single_file: Annotated[
        bool,
        typer.Option(
            "--single-file",
            help="Also write journey.html: one self-contained file you can "
            "AirDrop / email to someone (no server needed).",
        ),
    ] = False,
    embed_photos: Annotated[
        bool,
        typer.Option(
            "--embed-photos/--no-embed-photos",
            help="Embed small photo copies in journey.html (default: yes).",
        ),
    ] = True,
    max_embed_mb: Annotated[
        float,
        typer.Option(
            "--max-embed-mb",
            help="Photo budget for journey.html, in MB.",
        ),
    ] = 20.0,
    inline_libraries: Annotated[
        bool,
        typer.Option(
            "--inline-libraries/--no-inline-libraries",
            help="Inline Leaflet in journey.html so it needs no CDN.",
        ),
    ] = True,
    photo_size: Annotated[
        int,
        typer.Option(
            "--photo-size",
            help="Longest edge, in px, of each photo embedded in journey.html "
            "(bigger = sharper in the lightbox, larger file).",
        ),
    ] = 200,
    photo_quality: Annotated[
        int,
        typer.Option(
            "--photo-quality",
            help="JPEG quality (1-95) for embedded photos.",
        ),
    ] = 70,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Verbose logging")
    ] = False,
) -> None:
    """Reconstruct a road-trip route from geotagged media and render an HTML map.

    Run bare (no subcommand) to process a trip; use `route-recap serve` to
    browse reports with photos, or `--single-file` for one file you can send
    to someone.
    """
    if ctx.invoked_subcommand is not None:
        return
    load_dotenv()  # read .env only when a pipeline run actually starts
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = _gather_config(
        name, input_dir, unit, min_stop_minutes, no_geocode, no_filter_outliers,
        no_media,
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

    assets: dict[str, dict] = {}
    if config.stage_media:
        with console.status("Staging media & thumbnails..."):
            sources: list[Path] = []
            for w in summary.waypoints:
                for f in w.source_files:
                    if f not in sources:
                        sources.append(f)
            assets = stage_media(sources, output_dir)
        if assets:
            no_thumb = sum(
                1
                for a in assets.values()
                if a["kind"] == "video" and not a["thumb"]
            )
            console.print(
                f"[dim]Staged {len(assets)} media file(s) into "
                f"{output_dir / 'media'}.[/dim]"
            )
            if no_thumb:
                console.print(
                    f"[dim]{no_thumb} video(s) have no thumbnail — install "
                    f"ffmpeg to generate video preview frames.[/dim]"
                )

    carto_key = os.getenv("CARTO_BASEMAP_KEY") or None
    output_path = build_html(summary, output_dir, carto_key=carto_key, assets=assets)
    _show_summary(summary, output_path)
    if not carto_key:
        console.print(
            "[dim]Tip: set CARTO_BASEMAP_KEY in .env to use CARTO basemap tiles.[/dim]"
        )

    if single_file:
        with console.status("Building a single shareable file..."):
            share_path, warnings = _export_single_file(
                summary,
                output_dir,
                carto_key=carto_key,
                assets=assets,
                embed_photos=embed_photos,
                max_embed_mb=max_embed_mb,
                inline_libraries=inline_libraries,
                photo_size=photo_size,
                photo_quality=photo_quality,
            )
        for warning in warnings:
            console.print(f"[yellow]{warning}[/yellow]")
        size_mb = share_path.stat().st_size / (1024 * 1024)
        console.print(
            f"\n[green]Shareable file ready[/green] ({size_mb:.1f} MB): "
            f"[bold]{share_path}[/bold]"
        )
        console.print(
            "[dim]AirDrop, email or message this one file — no server, no "
            "setup. The recipient opens it and the journey works.[/dim]"
        )
        if not embed_photos:
            console.print(
                "[dim]Photos were not embedded (--no-embed-photos), so the "
                "file is tiny but has no images.[/dim]"
            )
        elif warnings:
            console.print(
                "[dim]Photos beyond the budget were omitted to keep the file "
                "shareable; raise it with --max-embed-mb.[/dim]"
            )

    if assets:
        console.print(
            "[dim]Full-resolution photos are viewable in the map popups via "
            "the local server:[/dim] [bold]uv run route-recap serve[/bold]"
        )

    if not no_open and Confirm.ask("Open the report in your browser?", default=True):
        webbrowser.open(output_path.resolve().as_uri())


@app.command()
def serve(
    directory: Annotated[
        Path, typer.Argument(help="Directory to serve (default: output)")
    ] = Path("output"),
    port: Annotated[
        int, typer.Option("--port", "-p", help="Port to listen on")
    ] = 8000,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Interface to bind. Use 0.0.0.0 to open reports on your phone.",
        ),
    ] = "127.0.0.1",
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Do not open the browser")
    ] = False,
) -> None:
    """Serve reports + media over HTTP (browsers block file:// images).

    Pass ``--host 0.0.0.0`` to open the report on a phone or tablet on the
    same Wi-Fi; the LAN address to type on the device is printed for you.
    """
    directory = directory.resolve()
    if not directory.is_dir():
        console.print(f"[red]Folder not found: {directory}[/red]")
        raise typer.Exit(1)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    trips = sorted(directory.glob("*/index.html"))
    exposed = host in ("0.0.0.0", "::")
    lan_hosts = _lan_ipv4_addresses() if exposed else []

    console.print(
        f"[green]Serving {directory}[/green] at "
        f"[bold]http://127.0.0.1:{port}/[/bold]"
    )
    if lan_hosts:
        console.print(
            "[green]On your phone (same Wi-Fi):[/green] "
            + "  ".join(f"[bold]http://{ip}:{port}/[/bold]" for ip in lan_hosts)
        )
        console.print(
            "[dim]If it times out, allow Python through the firewall "
            "(Windows: Private networks).[/dim]"
        )
    elif not exposed:
        console.print(
            "[dim]Tip: add [bold]--host 0.0.0.0[/bold] to open this on your "
            "phone.[/dim]"
        )
    else:
        console.print(
            "[yellow]No LAN address detected — run `ipconfig` and check that "
            "Wi-Fi is connected.[/yellow]"
        )

    if trips:
        console.print("Trip reports:")
        for t in trips:
            rel = t.relative_to(directory).as_posix()
            console.print(f"  • http://127.0.0.1:{port}/{rel}")
            for ip in lan_hosts:
                console.print(f"    [dim][arrow] http://{ip}:{port}/{rel}[/dim]")
    else:
        console.print(
            f"[dim]No reports found — expected {directory / '<trip>' / 'index.html'}[/dim]"
        )
    if not no_open and trips:
        webbrowser.open(
            f"http://127.0.0.1:{port}/{trips[0].relative_to(directory).as_posix()}"
        )
    with http.server.ThreadingHTTPServer((host, port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[dim]Server stopped.[/dim]")


if __name__ == "__main__":
    app()

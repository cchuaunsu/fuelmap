"""Command-line interface for operating the engine without a frontend."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from fie.container import build_container


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fie", description="Fuel Intelligence Engine"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    refresh_cmd = sub.add_parser(
        "refresh", help="Run a complete new investigation"
    )
    refresh_cmd.add_argument(
        "--stations", help="Comma-separated station ids (default: all)"
    )
    refresh_cmd.add_argument(
        "--developer", action="store_true",
        help="Include the developer-mode trace (requires FIE_DEVELOPER_MODE=1)",
    )

    sub.add_parser("stations", help="List canonical stations")
    sub.add_parser("prices", help="Show stored verified prices")

    serve_cmd = sub.add_parser("serve", help="Run the REST API + map frontend")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    # Precedence: --port flag, then PORT env (dev-preview harnesses), then 8000.
    serve_cmd.add_argument("--port", type=int, default=None)

    args = parser.parse_args(argv)
    container = build_container()

    if args.command == "stations":
        for station in container.stations.get_all():
            print(
                f"{station.station_id:24s} {station.brand.value:10s} "
                f"{station.official_name} — {station.city}"
            )
        return 0

    if args.command == "prices":
        for stored in container.store.get_all():
            price = f"₱{stored.price:.2f}" if stored.price is not None else "unavailable"
            print(
                f"{stored.station_id:24s} {stored.fuel_type:16s} {price:>12s} "
                f"[{stored.status.value} / {stored.confidence_level}] "
                f"src={stored.source_name or '-'}"
            )
        return 0

    if args.command == "refresh":
        station_ids = args.stations.split(",") if args.stations else None
        report = asyncio.run(
            container.orchestrator.refresh(
                station_ids=station_ids, developer_mode=args.developer
            )
        )
        payload = {
            "run_id": report.run_id,
            "stations_processed": report.stations_processed,
            "stats": report.stats,
            "results": [r.model_dump(mode="json") for r in report.results],
            "store_actions": report.store_actions,
            "provider_errors": report.provider_errors,
        }
        if report.trace is not None:
            payload["trace"] = report.trace
        json.dump(payload, sys.stdout, indent=2, default=str)
        print()
        return 0

    if args.command == "serve":
        import os

        import uvicorn

        from fie.api.app import create_app

        port = args.port or int(os.environ.get("PORT", "8000"))
        uvicorn.run(create_app(container), host=args.host, port=port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

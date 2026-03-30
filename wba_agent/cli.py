"""Command-line entrypoint for running the WBA collector agent."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path
from typing import List

from .config import AgentConfig, load_config
from .events import Event, EventBus
from .site_connection import SiteConnection
from .control_plane import ControlPlaneClient


class AgentRunner:
    """Coordinate multiple site connections based on configuration."""

    def __init__(self, config: AgentConfig, event_bus: EventBus) -> None:
        self.config = config
        self.event_bus = event_bus
        self.connections: List[SiteConnection] = []
        self.control_plane_client: ControlPlaneClient | None = None

        if self.config.control_plane:
            self.control_plane_client = ControlPlaneClient(
                base_url=self.config.control_plane.base_url,
                api_token=self.config.control_plane.api_token,
            )

    def start(self) -> None:
        loop = asyncio.get_event_loop()
        for site in self.config.sites:
            conn = SiteConnection(
                site,
                self.event_bus,
                self.control_plane_client,
                subscriptions=list(self.config.subscriptions),
                log_retention_days=self.config.log_retention_days,
            )
            self.connections.append(conn)
            if site.auto_start:
                loop.create_task(conn.run())

    async def stop(self) -> None:
        await asyncio.gather(*(conn.stop() for conn in self.connections), return_exceptions=True)
        if self.control_plane_client:
            await self.control_plane_client.close()


async def _handle_events(event_bus: EventBus) -> None:
    async def print_event(event: Event) -> None:
        payload = {k: v for k, v in event.payload.items() if k != "site"}
        site = event.payload.get("site")
        print(f"[{event.type.upper()}] {site}: {payload}")

    event_bus.subscribe("status", print_event)
    event_bus.subscribe("connected", print_event)
    event_bus.subscribe("cycle_started", print_event)
    event_bus.subscribe("cycle_completed", print_event)
    event_bus.subscribe("command_success", print_event)
    event_bus.subscribe("command_failed", print_event)
    event_bus.subscribe("snapshot_published", print_event)
    event_bus.subscribe("snapshot_publish_failed", print_event)
    event_bus.subscribe("error", print_event)

    event_bus.start()
    while True:
        await asyncio.sleep(1)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WBA collector agent")
    parser.add_argument("--config", required=True, help="Path to agent configuration file (JSON/YAML)")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file {config_path} not found", file=sys.stderr)
        return 1

    config = load_config(config_path)
    event_bus = EventBus()

    runner = AgentRunner(config, event_bus)
    loop = asyncio.get_event_loop()

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows event loop may not support add_signal_handler
            signal.signal(sig, lambda *_: stop_event.set())

    runner.start()

    async def _run() -> None:
        event_task = asyncio.create_task(_handle_events(event_bus))
        await stop_event.wait()
        await runner.stop()
        await event_bus.stop()
        event_task.cancel()
        try:
            await event_task
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())



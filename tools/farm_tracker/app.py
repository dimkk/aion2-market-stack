"""Standalone farm tracker runtime and loopback JSON control surface."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .contracts import ContractError
from .decoder import EconomyPacketDecoder, ZoneCatalog
from .history import HistoryStore
from .packet_bus import EconomyStreamDecoder, tail_directory
from .quotes import ItemCatalog, MarketPriceProvider
from .session import FarmSessionTracker
from .sync import sync_session


MAX_BODY = 64 * 1024


class Runtime:
    def __init__(
        self,
        *,
        history: Path,
        market_database: Path | None,
        market_server_id: int,
        catalog: Path,
        zones: Path | None,
        packet_bus: Path,
        idle_seconds: int,
        max_quote_age_seconds: int,
        sync_origin: str | None,
        sync_token: str | None,
        client_build: int | None = None,
    ) -> None:
        now_ms = int(time.time() * 1000)
        self.history = HistoryStore(history, now_ms)
        self.catalog = ItemCatalog(catalog)
        self.tracker = FarmSessionTracker(
            self.history,
            MarketPriceProvider(market_database, market_server_id, max_quote_age_seconds),
            self.catalog,
            idle_seconds,
        )
        bus_session = "farm-" + format(now_ms, "x")
        self.stream = EconomyStreamDecoder(
            EconomyPacketDecoder(bus_session, self.catalog, ZoneCatalog(zones, client_build))
        )
        self.packet_bus = packet_bus
        self.sync_origin = sync_origin
        self.sync_token = sync_token
        self.started_at_ms = now_ms
        self.last_record_at_ms: int | None = None
        self.decoded_events = 0
        self.tail_error: str | None = None
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def consume_record(self, record) -> None:
        self.last_record_at_ms = record.captured_at_ms
        for event in self.stream.push(record):
            result = self.tracker.apply(event)
            if result["applied"]:
                self.decoded_events += 1

    def start(self) -> None:
        tail = threading.Thread(
            target=self._tail,
            name="farm-packet-tail",
            daemon=True,
        )
        ticker = threading.Thread(target=self._tick, name="farm-session-ticker", daemon=True)
        self.threads = [tail, ticker]
        for thread in self.threads:
            thread.start()

    def _tail(self) -> None:
        try:
            tail_directory(self.packet_bus, self.consume_record, self.stop_event)
        except Exception:
            self.tail_error = "packet_tail_failed"

    def _tick(self) -> None:
        while not self.stop_event.wait(0.5):
            self.tracker.expire(int(time.time() * 1000))

    def close(self) -> None:
        self.stop_event.set()
        self.tracker.stop(int(time.time() * 1000))
        for thread in self.threads:
            thread.join(timeout=2)

    def health(self) -> dict[str, Any]:
        return {
            "schema": "a2.farm.health/v1",
            "status": "degraded" if self.tail_error else "ready",
            "started_at_ms": self.started_at_ms,
            "last_packet_record_at_ms": self.last_record_at_ms,
            "decoded_events": self.decoded_events,
            "active_session": self.tracker.snapshot() is not None,
            "sync_configured": bool(self.sync_origin and self.sync_token),
            "error": self.tail_error,
        }

    def sync(self, session_id: str) -> dict[str, Any]:
        if not self.sync_origin or not self.sync_token:
            raise ValueError("farm_sync_not_configured")
        return sync_session(self.history, session_id, self.sync_origin, self.sync_token)


def _handler(runtime: Runtime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Aion2Farm/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _json(self, status: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _mutation_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if not origin or not host:
                return False
            parsed = urlsplit(origin)
            return parsed.scheme == "http" and parsed.netloc == host and parsed.hostname in {"127.0.0.1", "localhost", "::1"}

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/farm/health":
                self._json(200, runtime.health())
            elif path == "/api/farm/session":
                self._json(200, {"session": runtime.tracker.snapshot()})
            elif path == "/api/farm/history":
                self._json(200, {"sessions": runtime.history.list()})
            elif path.startswith("/api/farm/history/"):
                session_id = path.removeprefix("/api/farm/history/")
                value = runtime.history.get(session_id)
                self._json(200 if value else 404, {"session": value} if value else {"error": "not_found"})
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if not self._mutation_allowed():
                self._json(403, {"error": "same_origin_required"})
                return
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self._json(413, {"error": "body_invalid"})
                return
            try:
                if path == "/api/farm/events":
                    value = json.loads(self.rfile.read(length))
                    self._json(200, runtime.tracker.apply(value))
                elif path == "/api/farm/session/stop":
                    self._json(200, {"session": runtime.tracker.stop(int(time.time() * 1000))})
                elif path.startswith("/api/farm/history/") and path.endswith("/sync"):
                    session_id = path.removeprefix("/api/farm/history/").removesuffix("/sync")
                    self._json(200, runtime.sync(session_id))
                else:
                    self._json(404, {"error": "not_found"})
            except (ContractError, ValueError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--market-database", type=Path)
    parser.add_argument("--market-server-id", type=int, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--zones", type=Path)
    parser.add_argument("--client-build", type=int)
    parser.add_argument("--packet-bus", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=int, default=15)
    parser.add_argument("--max-quote-age-seconds", type=int, default=86_400)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sync-origin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.listen not in {"127.0.0.1", "::1"} or not 1 <= args.port <= 65_535:
        raise SystemExit("farm_listener_invalid")
    runtime = Runtime(
        history=args.history,
        market_database=args.market_database,
        market_server_id=args.market_server_id,
        catalog=args.catalog,
        zones=args.zones,
        packet_bus=args.packet_bus,
        idle_seconds=args.idle_seconds,
        max_quote_age_seconds=args.max_quote_age_seconds,
        sync_origin=args.sync_origin,
        sync_token=os.environ.get("AION2_FARM_SYNC_TOKEN"),
        client_build=args.client_build,
    )
    runtime.start()
    server = ThreadingHTTPServer((args.listen, args.port), _handler(runtime))
    try:
        print(json.dumps({"status": "farm_tracker_ready", "listen": args.listen, "port": args.port}), flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

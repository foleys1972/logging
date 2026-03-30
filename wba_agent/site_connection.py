"""Async site connection management extracted from the legacy logger."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from hashlib import sha256

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK

from .config import SiteConfig
from .events import Event, EventBus
from .logging_utils import LogRotator, timestamp
from .control_plane import ControlPlaneClient


def _batch_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _batch_meta_from_message(message: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """Inner data first, then envelope (matches etsv3 / some WBA servers)."""
    payload = message.get("data")
    if not isinstance(payload, dict):
        payload = {}
    cur = _batch_int(payload.get("current_batch")) or _batch_int(message.get("current_batch"))
    last = _batch_int(payload.get("last_batch")) or _batch_int(message.get("last_batch"))
    return cur, last


# Keep in sync with wba_logger.WBA_BATCH_MERGE_LIST_KEYS — list keys merged across WBA batch pages.
WBA_BATCH_MERGE_LIST_KEYS = (
    "users",
    "turrets",
    "lines",
    "sharedprofiles",
    "calls",
    "events",
    "tpos",
    "zones",
)


class SiteConnection:
    """Manage websocket connectivity, command execution, and baselines."""

    def __init__(
        self,
        config: SiteConfig,
        event_bus: EventBus,
        control_plane_client: Optional[ControlPlaneClient] = None,
        subscriptions: Optional[List[str]] = None,
        log_retention_days: int = 365,
    ) -> None:
        self.cfg = config
        self.event_bus = event_bus
        self.control_plane_client = control_plane_client
        self.subscriptions = list(subscriptions or [])
        self.subscribed_categories: set[str] = set()
        self._log_retention_days = max(0, min(3650, int(log_retention_days)))

        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.running: bool = False
        self.authenticated: bool = False
        self.connection_alive: bool = False

        self.command_ref: int = 0
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.pending_batches: Dict[str, Dict[str, Any]] = {}

        self.status: str = "Stopped"
        self.restart_attempts: int = 0
        self.max_restart_attempts: int = 3
        self.last_activity: Optional[float] = None

        self.log = LogRotator(
            config.name,
            Path(config.log_dir) if config.log_dir else None,
            retention_days=self._log_retention_days,
        )
        self.baseline = {"zones": None, "tpos": None}

    async def run(self) -> None:
        """Entry point for auto-reconnect loop."""

        self.running = True
        while self.running and self.restart_attempts <= self.max_restart_attempts:
            try:
                if not await self.connect_and_auth():
                    if self.cfg.auto_start and self.restart_attempts < self.max_restart_attempts:
                        self.restart_attempts += 1
                        wait_time = min(10 * self.restart_attempts, 30)
                        await self._notify("reconnect_scheduled", {"wait_seconds": wait_time})
                        await asyncio.sleep(wait_time)
                        continue
                    self.running = False
                    return

                if self.restart_attempts > 0:
                    await self._notify("reconnected", {"attempts": self.restart_attempts})
                self.restart_attempts = 0

                await self._notify("connected", {})
                for category in self.subscriptions:
                    await self.subscribe_to_notifications(category)
                    await asyncio.sleep(0.5)

                receive_task = asyncio.create_task(self._receive_messages())
                try:
                    await self._command_loop()
                finally:
                    receive_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receive_task
                    await self._cleanup_pending()
                    await self._close_websocket()
                    self.authenticated = False
                    self.connection_alive = False
            except Exception as exc:
                await self._notify("error", {"message": str(exc)})
                if self.cfg.auto_start and self.restart_attempts < self.max_restart_attempts:
                    self.restart_attempts += 1
                    wait_time = min(10 * self.restart_attempts, 30)
                    await asyncio.sleep(wait_time)
                else:
                    self.running = False

        if self.restart_attempts > self.max_restart_attempts:
            await self._notify("failed", {"reason": "max_retries"})
        self.running = False

    async def stop(self) -> None:
        self.running = False
        self.restart_attempts = self.max_restart_attempts + 1
        await self._notify("stopping", {})
        await self._cleanup_pending()
        await self._close_websocket()
        self.authenticated = False
        self.connection_alive = False
        await self._notify("stopped", {})

    async def connect_and_auth(self) -> bool:
        try:
            await self._notify("status", {"status": "connecting"})
            if self.websocket:
                await self._close_websocket()
            self.pending_responses.clear()
            self.pending_batches.clear()

            ssl_context = None
            if self.cfg.url.startswith("wss://") and self.cfg.ignore_ssl:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                await self._notify("warning", {"message": "SSL verification disabled"})

            self.websocket = await asyncio.wait_for(
                websockets.connect(
                    self.cfg.url,
                    ssl=ssl_context,
                    max_size=10 * 1024 * 1024,
                    ping_interval=None,
                    ping_timeout=60,
                    close_timeout=10,
                ),
                timeout=10,
            )
            self.connection_alive = True
            await self._notify("status", {"status": "authenticating"})

            self.command_ref += 1
            cmd_ref = f"{self.cfg.name}_{self.command_ref}"
            auth_msg = {"command": "auth", "command_ref": cmd_ref, "args": {"token": self.cfg.token}}

            await self.websocket.send(json.dumps(auth_msg))
            response = await asyncio.wait_for(self.websocket.recv(), timeout=10)
            data = json.loads(response)

            if data.get("success"):
                self.authenticated = True
                self.connection_alive = True
                self.last_activity = asyncio.get_event_loop().time()
                await self._notify("authenticated", {})
                return True

            await self._notify("auth_failed", {"error": data.get("error")})
            self.connection_alive = False
            return False

        except Exception as exc:
            await self._notify("error", {"message": str(exc)})
            self.connection_alive = False
            return False

    @staticmethod
    def _error_code(error: Optional[Dict[str, Any]]) -> Optional[int]:
        if not error:
            return None
        code = error.get("code")
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.isdigit():
            return int(code)
        return None

    def _combine_batches(self, cmd_ref: str, initial_response: Dict[str, Any]) -> Dict[str, Any]:
        """Merge follow-up batches (same keys as extended monitoring + other batched WBA commands)."""
        batch_info = self.pending_batches.get(cmd_ref, {})
        batches = batch_info.get("batches", [])
        if not batches:
            return initial_response

        combined = dict(initial_response)
        data = dict(combined.get("data", {}))

        merged_any = False
        for key in WBA_BATCH_MERGE_LIST_KEYS:
            items_first = list(data[key]) if isinstance(data.get(key), list) else []
            extra: List[Any] = []
            for batch in batches:
                bd = batch.get("data", {})
                if isinstance(bd, dict) and isinstance(bd.get(key), list):
                    extra.extend(bd[key])
            if items_first or extra:
                data[key] = items_first + extra
                merged_any = True

        if merged_any:
            lb = batch_info.get("last_batch")
            if lb is not None:
                data["current_batch"] = lb
        elif batches:
            logging.getLogger(__name__).warning(
                "Multi-batch merge for %s: no known list key in first batch keys=%s",
                cmd_ref,
                list(data.keys()),
            )

        combined["data"] = data
        return combined

    @staticmethod
    def _accumulate_users_from_batch_message(batch_info: Dict[str, Any], message: Dict[str, Any]) -> None:
        pl = message.get("data", {})
        if not isinstance(pl, dict):
            return
        users = pl.get("users")
        if not isinstance(users, list) or not users:
            return
        batch_info.setdefault("accumulated_users", []).extend(users)

    def _apply_accumulated_users_to_response(self, cmd_ref: str, response_data: Dict[str, Any]) -> None:
        bi = self.pending_batches.get(cmd_ref)
        if not bi:
            return
        acc = bi.get("accumulated_users") or []
        if not acc:
            return
        d = response_data.get("data")
        if not isinstance(d, dict):
            response_data["data"] = {"users": list(acc)}
            return
        merged = dict(d)
        merged["users"] = list(acc)
        response_data["data"] = merged

    async def _resubscribe_notifications_after_reauth(self) -> None:
        """WBA spec: after re-authentication, subscribe again to notification categories."""
        for category in self.subscriptions:
            await self.subscribe_to_notifications(category)
            await asyncio.sleep(0.5)

    async def _command_loop(self) -> None:
        interval_seconds = (self.cfg.interval_minutes or 0) or 5 * 60
        interval_seconds = max(interval_seconds, 60)
        last_command_time = 0.0
        health_check_interval = 30.0
        last_health_check = asyncio.get_event_loop().time()

        while self.running:
            current_time = asyncio.get_event_loop().time()

            if current_time - last_command_time >= interval_seconds:
                self.log.retention_days = self._log_retention_days
                self.log.cleanup()
                commands = self.cfg.commands
                await self._notify("cycle_started", {"commands": commands})
                for command in commands:
                    if not self.running or not self.authenticated or not self.connection_alive:
                        await self._notify("cycle_skipped", {"reason": "connection"})
                        break
                    await self.execute_command(command)
                    await asyncio.sleep(1)
                last_command_time = current_time
                await self._notify("cycle_completed", {"next_in_seconds": interval_seconds})

            if current_time - last_health_check >= health_check_interval:
                await self._health_check()
                last_health_check = current_time

            await asyncio.sleep(1)

    async def execute_command(self, command: str) -> bool:
        if not self.authenticated or not self.websocket:
            return False

        base_command = command
        args: Dict[str, Any] = {}
        if ":" in command:
            parts = command.split(":", 1)
            base_command = parts[0]
            if base_command == "get_events":
                args["category"] = parts[1]

        if base_command == "get_users":
            # WBA spec: boolean; strings "true"/"false" are rejected as wrong type
            args["get_lines_info"] = bool(self.cfg.get_lines_info)

        for attempt in range(3):
            cmd_ref = None
            try:
                self.command_ref += 1
                cmd_ref = f"{self.cfg.name}_{self.command_ref}"
                msg = {"command": base_command, "command_ref": cmd_ref, "args": args}

                future: "asyncio.Future[Dict[str, Any]]" = asyncio.Future()
                self.pending_responses[cmd_ref] = future
                self.pending_batches[cmd_ref] = {
                    "batches": [],
                    "last_batch": None,
                    "complete": False,
                    "accumulated_users": [],
                }

                await self.websocket.send(json.dumps(msg))

                try:
                    response = await asyncio.wait_for(future, timeout=30)
                    current_batch, last_batch = _batch_meta_from_message(response)

                    if current_batch is not None and last_batch is not None and last_batch > 1:
                        remaining = max(0, last_batch - current_batch)
                        timeout = max(120.0, 45.0 + (remaining * 45.0))
                        wait_start = asyncio.get_event_loop().time()
                        while not self.pending_batches[cmd_ref]["complete"]:
                            elapsed = asyncio.get_event_loop().time() - wait_start
                            if elapsed > timeout:
                                await self._notify(
                                    "batch_timeout",
                                    {"command": command, "received": len(self.pending_batches[cmd_ref]["batches"])},
                                )
                                break
                            await asyncio.sleep(0.1)
                        if self.pending_batches[cmd_ref]["batches"]:
                            response = self._combine_batches(cmd_ref, response)
                    self._apply_accumulated_users_to_response(cmd_ref, response)
                except asyncio.TimeoutError:
                    self.pending_responses.pop(cmd_ref, None)
                    self.pending_batches.pop(cmd_ref, None)
                    if attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    await self._notify("command_timeout", {"command": command})
                    return False
                finally:
                    self.pending_batches.pop(cmd_ref, None)

                self.last_activity = asyncio.get_event_loop().time()

                if response.get("success"):
                    await self._handle_command_success(command, response)
                    return True

                error = response.get("error", {})
                await self._notify("command_failed", {"command": command, "error": error})
                err_code = self._error_code(error)
                if err_code == 401:
                    if await self.re_authenticate():
                        await self._resubscribe_notifications_after_reauth()
                        continue
                if err_code == 498:
                    raise ConnectionError("Invalid token, reconnection required")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    return False

            except Exception as exc:
                if cmd_ref:
                    self.pending_responses.pop(cmd_ref, None)
                    self.pending_batches.pop(cmd_ref, None)
                await self._notify("command_error", {"command": command, "error": str(exc)})
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    return False

        return False

    async def _receive_messages(self) -> None:
        assert self.websocket is not None
        while self.running:
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=30.0)
                if isinstance(message, bytes):
                    self.last_activity = asyncio.get_event_loop().time()
                    continue

                data = json.loads(message)
                self.last_activity = asyncio.get_event_loop().time()

                if data.get("command") in ("response", "return"):
                    await self._handle_response(data)
                elif data.get("command") == "notify":
                    await self._notify("notification", data.get("data", {}))
                elif data.get("command") == "server notification":
                    await self._handle_server_notification(data)
            except asyncio.TimeoutError:
                if not self.running:
                    break
                continue
            except json.JSONDecodeError:
                self.last_activity = asyncio.get_event_loop().time()
                continue
            except ConnectionClosedOK:
                self.connection_alive = False
                await self._notify("disconnected", {"reason": "closed"})
                break
            except ConnectionClosedError as exc:
                self.connection_alive = False
                await self._notify("disconnected", {"error": str(exc)})
                break
            except ConnectionClosed as exc:
                self.connection_alive = False
                await self._notify("disconnected", {"code": exc.code, "reason": exc.reason})
                break

    async def _handle_response(self, data: Dict[str, Any]) -> None:
        """Route batch 1 via pending_responses; batches 2..N via pending_batches (all batched WBA commands)."""
        cmd_ref = data.get("command_ref")
        if not cmd_ref:
            return
        cur, last = _batch_meta_from_message(data)

        if cmd_ref in self.pending_responses:
            future = self.pending_responses[cmd_ref]
            if cur is not None and last is not None and last > 1:
                batch_info = self.pending_batches.get(cmd_ref)
                if not batch_info:
                    return
                batch_info["last_batch"] = last
                if cur == 1:
                    self._accumulate_users_from_batch_message(batch_info, data)
                    self.pending_responses.pop(cmd_ref, None)
                    if not future.done():
                        future.set_result(data)
                else:
                    self._accumulate_users_from_batch_message(batch_info, data)
                    batch_info["batches"].append(data)
                if cur == last:
                    batch_info["complete"] = True
                return
            if cmd_ref in self.pending_batches:
                self._accumulate_users_from_batch_message(self.pending_batches[cmd_ref], data)
            self.pending_responses.pop(cmd_ref, None)
            if not future.done():
                future.set_result(data)
            return

        if cmd_ref in self.pending_batches:
            batch_info = self.pending_batches[cmd_ref]
            if last is not None:
                batch_info["last_batch"] = last
            self._accumulate_users_from_batch_message(batch_info, data)
            batch_info["batches"].append(data)
            if cur is not None and last is not None and cur == last:
                batch_info["complete"] = True

    async def _handle_server_notification(self, data: Dict[str, Any]) -> None:
        message = data.get("message", "")
        cmd_ref = data.get("command_ref", "")
        await self._notify("server_notification", {"message": message})

        if "re-authenticate" in message.lower() or cmd_ref == "authentication expiry":
            if await self.re_authenticate():
                await self._resubscribe_notifications_after_reauth()
            else:
                self.connection_alive = False

        elif "session is expired" in message.lower() or cmd_ref == "Session expired":
            self.connection_alive = False
            self.authenticated = False

    async def re_authenticate(self) -> bool:
        if not self.websocket:
            return False
        try:
            self.command_ref += 1
            cmd_ref = f"{self.cfg.name}_{self.command_ref}"
            msg = {"command": "auth", "command_ref": cmd_ref, "args": {"token": self.cfg.token}}

            future: "asyncio.Future[Dict[str, Any]]" = asyncio.Future()
            self.pending_responses[cmd_ref] = future
            await self.websocket.send(json.dumps(msg))

            try:
                response = await asyncio.wait_for(future, timeout=10)
            except asyncio.TimeoutError:
                self.pending_responses.pop(cmd_ref, None)
                await self._notify("re_auth_timeout", {})
                return False

            if response.get("success"):
                self.authenticated = True
                self.connection_alive = True
                self.last_activity = asyncio.get_event_loop().time()
                await self._notify("re_authenticated", {})
                return True

            await self._notify("re_auth_failed", {"error": response.get("error")})
            return False
        except Exception as exc:
            await self._notify("re_auth_error", {"error": str(exc)})
            return False

    async def subscribe_to_notifications(self, category: str) -> bool:
        if not self.websocket:
            return False
        try:
            self.command_ref += 1
            cmd_ref = f"{self.cfg.name}_{self.command_ref}"
            msg = {"command": "subscribe", "command_ref": cmd_ref, "args": {"category": category}}

            future: "asyncio.Future[Dict[str, Any]]" = asyncio.Future()
            self.pending_responses[cmd_ref] = future
            await self.websocket.send(json.dumps(msg))

            response = await asyncio.wait_for(future, timeout=10)
            if response.get("success"):
                self.subscribed_categories.add(category)
                await self._notify("subscribed", {"category": category})
                return True

            await self._notify("subscribe_failed", {"category": category, "error": response.get("error")})
            return False
        except Exception as exc:
            await self._notify("subscribe_error", {"category": category, "error": str(exc)})
            return False

    async def _health_check(self) -> None:
        if not self.websocket:
            self.connection_alive = False
            await self._notify("connection_warning", {"message": "websocket_none"})
            raise ConnectionError("WebSocket connection closed")

        if getattr(self.websocket, "closed", False):
            self.connection_alive = False
            await self._notify("connection_warning", {"message": "websocket_closed"})
            raise ConnectionError("WebSocket connection closed")

        if self.last_activity and (asyncio.get_event_loop().time() - self.last_activity) >= 300:
            try:
                pong = await asyncio.wait_for(self.websocket.ping(), timeout=10)
                await asyncio.wait_for(pong, timeout=10)
                self.last_activity = asyncio.get_event_loop().time()
                self.connection_alive = True
            except Exception as exc:
                await self._notify("ping_failed", {"error": str(exc)})

    async def _handle_command_success(self, command: str, response: Dict[str, Any]) -> None:
        await self._notify("command_success", {"command": command, "response": response})

        if self.control_plane_client and self.cfg.control_plane_site_id:
            snapshot = self._build_snapshot_payload(command, response)
            if snapshot:
                try:
                    await self.control_plane_client.send_snapshot(
                        self.cfg.control_plane_site_id,
                        snapshot,
                    )
                    await self._notify("snapshot_published", {"component": snapshot["component"]})
                except Exception as exc:
                    await self._notify("snapshot_publish_failed", {"error": str(exc)})

    async def _cleanup_pending(self) -> None:
        for future in list(self.pending_responses.values()):
            if not future.done():
                future.cancel()
        self.pending_responses.clear()
        self.pending_batches.clear()

    async def _close_websocket(self) -> None:
        if self.websocket:
            with contextlib.suppress(Exception):
                await self.websocket.close()
            self.websocket = None

    async def _notify(self, event_type: str, payload: Dict[str, Any]) -> None:
        await self.event_bus.publish(Event(type=event_type, payload={"site": self.cfg.name, **payload}))

    def _build_snapshot_payload(self, command: str, response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = response.get("data", {})
        if not isinstance(data, dict):
            data = {}

        component = command.split(":", 1)[0]

        list_keys = [
            ("zones", "zone_count"),
            ("tpos", "tpo_count"),
            ("users", "user_count"),
            ("calls", "call_count"),
            ("events", "event_count"),
            ("turrets", "turret_count"),
            ("lines", "line_count"),
            ("sharedprofiles", "profile_count"),
        ]

        count = 0
        metrics: Dict[str, Any] = {}

        for key, metric in list_keys:
            value = data.get(key)
            if isinstance(value, list):
                count = len(value)
                metrics[metric] = count
                if key == "tpos":
                    metrics["alive"] = sum(1 for item in value if isinstance(item, dict) and item.get("alive"))
                if key == "zones":
                    metrics["active_zones"] = sum(
                        1
                        for item in value
                        if isinstance(item, dict) and item.get("status") in {"ACTIVE", "ENABLED"}
                    )
                break

        if count == 0:
            count = int(data.get("count") or 0)

        flags: Dict[str, Any] = {}
        status = data.get("status")
        if isinstance(status, str) and status.lower() != "ok":
            flags["status"] = status

        payload_str = json.dumps(data, sort_keys=True, default=str)
        data_hash = sha256(payload_str.encode("utf-8")).hexdigest()

        payload_dict: Optional[Dict[str, Any]]
        if len(payload_str) <= 5000:
            payload_dict = data
        else:
            payload_dict = None
            flags["truncated_for_ingest"] = True

        snapshot_payload = {
            "component": component,
            "hash": data_hash,
            "count": count,
            "metrics": metrics,
            "flags": flags,
            "payload": payload_dict,
        }

        return snapshot_payload



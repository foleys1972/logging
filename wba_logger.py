"""
TradeSense WBA Logger - Enhanced Version
Complete application with GUI for configuration and control

Features:
- Monitors zones and TPOs with baseline comparison
- Detects changes in TPO currentState (ACTIVE/PASSIVE)
- Real-time alert subscriptions (configurable via Global Settings)
- Periodic command execution
- Automatic multi-batch response handling for get_users, get_calls, get_events
- 10MB WebSocket frame size limit (handles large batches)
- Command responses always logged (with smart 100KB truncation)
- Debug mode logs ALL messages (requests, timing, notifications, etc.)
- Server-managed keepalive (server pings every 5s per API default)
- Passive connection monitoring (checks state, doesn't interfere with server pings)
- Proper re-authentication cycle per API specification
- Graceful shutdown with proper unsubscribe handling
- Per-site custom log directory selection
- Log rotation and configurable retention (delete files older than N days; 0 = disabled)
- SSL certificate verification bypass (for self-signed certs)
- Auto-start sites on application launch
- Auto-reconnect with 3 retry attempts on connection loss
- Per-site debug logging with toggle capability
- Per-site command configuration
- Comprehensive command selection UI
- Baseline reset capability
- Config validation and migration

Alert Notifications:
- Configure in Global Settings > Alert Notifications
- Default: Enabled for all sites
- Requires server setting: application.assure.alerts.enable.wba.notification = true
- Notifications appear in both GUI and log files

Logging Behavior:
- Normal mode: Command responses with full JSON data (truncated if >100KB)
- Debug mode: Everything above + request details, timing, all notifications, detailed errors

Important: The server manages connection keepalive via ping frames (default 5 seconds).
Client responds automatically to server pings. We don't send our own pings to avoid
interfering with the server's re-authentication mechanism.
"""

import asyncio
import websockets
import websockets.exceptions
import json
import ssl
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, List, Tuple
import threading
import time

# Constants
CONFIG_FILE = "wba_config.json"
LOG_BASE_DIR = "logs"
MAX_LOG_SIZE = 50 * 1024 * 1024  # 50MB
DEFAULT_LOG_RETENTION_DAYS = 365
MAX_LOG_RETENTION_DAYS = 3650

# Available WBA commands
AVAILABLE_COMMANDS = {
    "core": [
        ("get_zones", "Monitor zone configuration"),
        ("get_tpos", "Monitor TPO status"),
        ("get_version", "Track component versions"),
        ("get_health_api_report", "System health report"),
    ],
    "extended": [
        ("get_turrets", "Monitor turret devices"),
        ("get_users", "User and line information"),
        ("get_lines", "TPO line details"),
        ("get_shared_profiles", "Shared profile data"),
    ],
    "historical": [
        ("get_events:calls", "Call events history (batched)"),
        ("get_events:presences", "Presence events history (batched)"),
        ("get_calls", "Call records (batched)"),
    ]
}

# TPO fields we track for baseline comparison (WBA get_tpos response)
TPO_COMPARE_FIELDS = (
    "alive",
    "currentState",
    "zone",
    "clusterName",
    "tpoDnsName",
    "tssVersion",
    "ipAddress",
    "recordingServerEnabled",
)

# Default commands for new sites
DEFAULT_COMMANDS = ["get_zones", "get_tpos", "get_version", "get_health_api_report"]

# Extended + historical commands may use "once daily at HH:MM" instead of every global interval
SCHEDULABLE_COMMAND_KEYS = frozenset(
    [c for c, _ in AVAILABLE_COMMANDS["extended"]]
    + [c for c, _ in AVAILABLE_COMMANDS["historical"]]
)


def _parse_daily_time(value) -> str:
    """Normalize time to HH:MM (24h local)."""
    if value is None:
        return "02:00"
    s = str(value).strip()
    parts = s.replace(".", ":").split(":")
    if len(parts) >= 2:
        try:
            h = max(0, min(23, int(parts[0])))
            m = max(0, min(59, int(parts[1])))
            return f"{h:02d}:{m:02d}"
        except ValueError:
            pass
    return "02:00"


def migrate_site_command_settings(site: Dict) -> None:
    """Ensure command_settings exists and is valid for each enabled command."""
    cmds = site.get("commands") or []
    raw = site.get("command_settings")
    settings: Dict = {k: v for k, v in (raw or {}).items() if k in cmds} if isinstance(raw, dict) else {}
    for c in cmds:
        if c not in settings or not isinstance(settings[c], dict):
            settings[c] = {"schedule": "interval"}
            continue
        entry = settings[c]
        sched = entry.get("schedule", "interval")
        if sched not in ("interval", "daily"):
            sched = "interval"
        if sched == "daily" and c not in SCHEDULABLE_COMMAND_KEYS:
            sched = "interval"
        entry["schedule"] = sched
        entry["daily_time"] = _parse_daily_time(entry.get("daily_time"))
        settings[c] = entry
    site["command_settings"] = settings


def split_commands_by_schedule(site_config: Dict) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Returns (interval_command_list, daily_list of (cmd, HH:MM))."""
    migrate_site_command_settings(site_config)
    cmds = site_config.get("commands") or []
    settings = site_config.get("command_settings") or {}
    interval_cmds = []
    daily = []
    for c in cmds:
        s = settings.get(c) or {}
        if s.get("schedule") == "daily" and c in SCHEDULABLE_COMMAND_KEYS:
            daily.append((c, _parse_daily_time(s.get("daily_time"))))
        else:
            interval_cmds.append(c)
    return interval_cmds, daily


class LogRotator:
    """Handles log file rotation"""
    
    def __init__(
        self,
        site_name: str,
        custom_log_dir: Optional[str] = None,
        retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
    ):
        self.site_name = site_name
        self.retention_days = max(0, min(MAX_LOG_RETENTION_DAYS, int(retention_days)))
        if custom_log_dir:
            self.site_dir = Path(custom_log_dir) / site_name
        else:
            self.site_dir = Path(LOG_BASE_DIR) / site_name
        self.site_dir.mkdir(parents=True, exist_ok=True)
        self.current_file = None
        self.current_date = None
        self.rollover_count = 0
        
    def get_log_file(self) -> Path:
        """Get current log file"""
        today = datetime.now(timezone.utc).date()
        
        if self.current_date != today:
            self.current_date = today
            self.rollover_count = 0
            self.current_file = self.site_dir / f"{today.isoformat()}_{self.site_name}.log"
        
        if self.current_file and self.current_file.exists():
            if self.current_file.stat().st_size >= MAX_LOG_SIZE:
                self.rollover_count += 1
                self.current_file = self.site_dir / f"{today.isoformat()}_{self.site_name}_{self.rollover_count:03d}.log"
        
        return self.current_file
    
    def write(self, message: str):
        """Write to log file"""
        log_file = self.get_log_file()
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(message + '\n')
    
    def cleanup_old_logs(self):
        """Remove *.log files whose mtime is older than retention_days (UTC). 0 = disabled."""
        if self.retention_days <= 0:
            return
        cutoff = datetime.now(timezone.utc).timestamp() - (self.retention_days * 86400)
        for log_file in self.site_dir.glob("*.log"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
            except OSError:
                pass


class SiteConnection:
    """Manages connection to a single WBA site"""
    
    def __init__(self, site_config: Dict, app):
        self.site_name = site_config["name"]
        self.url = site_config["url"]
        self.token = site_config["token"]
        self.ignore_ssl = site_config.get("ignore_ssl", False)
        self.auto_start = site_config.get("auto_start", False)
        self.debug_mode = site_config.get("debug_mode", False)
        self.interval_commands, self.daily_commands = split_commands_by_schedule(site_config)
        self._daily_last_run: Dict[str, str] = {}
        self.custom_log_dir = site_config.get("log_dir", None)
        self.app = app
        self.websocket = None
        self.running = False
        self.authenticated = False
        self.command_ref = 0
        self.log = LogRotator(
            self.site_name,
            self.custom_log_dir,
            retention_days=self.app.get_log_retention_days(),
        )
        self.status = "Stopped"
        self.subscribed_categories = set()
        self.restart_attempts = 0
        self.max_restart_attempts = 3
        self.last_activity = None
        self.connection_alive = False
        
        # Response tracking for commands
        self.pending_responses = {}
        
        # Batch tracking for multi-batch responses
        self.pending_batches = {}
        
        # Baseline storage for change detection
        self.baseline = {
            "zones": None,
            "tpos": None
        }

    def _daily_fire(self, cmd: str, time_str: str, now_local: datetime) -> bool:
        """True once per local calendar day when hour:minute matches scheduled time."""
        try:
            h, m = map(int, time_str.split(":"))
        except ValueError:
            return False
        if now_local.hour != h or now_local.minute != m:
            return False
        key = now_local.date().isoformat()
        if self._daily_last_run.get(cmd) == key:
            return False
        self._daily_last_run[cmd] = key
        return True
    
    def _debug_log(self, message: str, data: any = None):
        """Log debug information if debug mode is enabled"""
        if self.debug_mode:
            timestamp = self._timestamp()
            debug_msg = f"[{timestamp}] [DEBUG] {message}"
            self.log.write(debug_msg)
            if data:
                self.log.write(f"[{timestamp}] [DEBUG] Data: {json.dumps(data) if isinstance(data, dict) else str(data)}")
            # Also log to GUI with debug indicator
            self._log_to_gui(f"🐛 DEBUG: {message}")
    
    def _update_status(self, status: str, log_to_gui: bool = False, gui_message: str = None):
        """Update status and optionally log to GUI"""
        self.status = status
        self.app.update_site_status(self.site_name, self.status)
        if log_to_gui:
            self._log_to_gui(gui_message or status)
    
    def _timestamp(self) -> str:
        """Get formatted timestamp"""
        now = datetime.now(timezone.utc)
        local = datetime.now()
        if self.debug_mode:
            # Add milliseconds for debug mode
            return f"{now.isoformat(timespec='milliseconds')}Z ({local.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]} %Z)"
        return f"{now.isoformat()}Z ({local.strftime('%Y-%m-%dT%H:%M:%S %Z')})"
    
    def _write_log(self, log_type: str, message: str, data_summary: str = "", full_data: Optional[Dict] = None):
        """Write single-line log entry with optional full data"""
        timestamp = self._timestamp()
        if data_summary:
            log_message = f"[{timestamp}] [{log_type}] {message} | {data_summary}"
        else:
            log_message = f"[{timestamp}] [{log_type}] {message}"
        
        self.log.write(log_message)
        
        # Write full JSON data for COMMAND responses (always), and for everything else only in debug mode
        should_write_data = False
        if full_data:
            if log_type == "COMMAND":
                # Always write command response data
                should_write_data = True
            elif self.debug_mode:
                # Write all other data types only in debug mode
                should_write_data = True
        
        if should_write_data:
            try:
                json_str = json.dumps(full_data)
                # Limit to 100KB per JSON entry to prevent huge log files
                if len(json_str) > 100000:
                    truncated = {
                        "_note": "Response truncated due to size",
                        "_size_bytes": len(json_str),
                        "command": full_data.get("command"),
                        "success": full_data.get("success"),
                        "data_keys": list(full_data.get("data", {}).keys()) if isinstance(full_data.get("data"), dict) else None
                    }
                    # Add count information if available
                    data = full_data.get("data", {})
                    if "users" in data:
                        truncated["user_count"] = len(data.get("users", []))
                    elif "calls" in data:
                        truncated["call_count"] = len(data.get("calls", []))
                    elif "events" in data:
                        truncated["event_count"] = len(data.get("events", []))
                    elif "turrets" in data:
                        truncated["turret_count"] = len(data.get("turrets", []))
                    elif "zones" in data:
                        truncated["zone_count"] = len(data.get("zones", []))
                    elif "tpos" in data:
                        truncated["tpo_count"] = len(data.get("tpos", []))
                    elif "lines" in data:
                        truncated["line_count"] = len(data.get("lines", []))
                    
                    self.log.write(json.dumps(truncated))
                    self.log.write(f"[Full response size: {len(json_str)} bytes - truncated for log size management]")
                else:
                    self.log.write(json_str)
            except Exception as e:
                self.log.write(f"[Error serializing response: {str(e)}]")
    
    def _log_to_gui(self, message: str):
        """Log message to GUI"""
        self.app.log_activity(f"[{self.site_name}] {message}")
    
    @staticmethod
    def _error_code(error: Optional[dict]) -> Optional[int]:
        """Normalize API error code (spec uses numeric codes; some payloads may use strings)."""
        if not error:
            return None
        code = error.get("code")
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.isdigit():
            return int(code)
        return None

    def _format_error(self, error: dict) -> str:
        """Format error message from API response"""
        if not error:
            return "Unknown error"
        
        code = self._error_code(error)
        if code is None:
            code = error.get("code", "")
        status = error.get("status", "")
        message = error.get("message", "")
        reason = error.get("reason", "")
        
        if code == 401:
            return f"Unauthorized - {reason or 'Invalid credentials'}"  # spec: re-auth + re-subscribe
        elif code == 404:
            return f"Not Found - {reason or message}"
        elif code == 498:
            return f"Invalid Token - {reason or 'Token mismatch, reconnection required'}"
        elif code == 409:
            return f"Conflict - {reason or message}"
        elif code == 500:
            return f"Server Error - {reason or message}"
        else:
            return reason or message or f"Error {code}: {status}"
    
    async def connect_and_auth(self) -> bool:
        """Connect and authenticate"""
        try:
            self._update_status("Connecting...", True, "Connecting...")
            self._debug_log("Starting connection attempt")
            
            # Close existing connection if any
            if self.websocket:
                try:
                    await self.websocket.close()
                    self._debug_log("Closed existing websocket")
                except:
                    pass
            
            # Clear pending responses from previous connection
            for cmd_ref, future in list(self.pending_responses.items()):
                if not future.done():
                    future.cancel()
            self.pending_responses.clear()
            self.pending_batches.clear()
            
            # Configure SSL context if needed
            ssl_context = None
            if self.url.startswith("wss://") and self.ignore_ssl:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                self._log_to_gui("⚠️ SSL certificate verification disabled")
                self._debug_log("SSL verification disabled")
            
            start_time = asyncio.get_event_loop().time()
            # Note: Server sends pings every 5 seconds (default) per API settings
            # We just need to respond (automatic) and set a reasonable timeout
            self.websocket = await asyncio.wait_for(
                websockets.connect(
                    self.url, 
                    ssl=ssl_context,
                    max_size=10 * 1024 * 1024,  # 10MB max frame size
                    ping_interval=None,  # Disable client pings - server handles this
                    ping_timeout=60,  # Timeout if no pong received (server pings every 5s)
                    close_timeout=10
                ), 
                timeout=10
            )
            connect_time = asyncio.get_event_loop().time() - start_time
            self._debug_log(f"WebSocket connected in {connect_time:.3f}s (server handles keepalive pings)")
            
            self._update_status("Authenticating...", True, "Authenticating...")
            
            # Authenticate
            self.command_ref += 1
            auth_msg = {
                "command": "auth",
                "command_ref": f"{self.site_name}_{self.command_ref}",
                "args": {"token": self.token}
            }
            
            self._debug_log("Sending authentication", auth_msg)
            await self.websocket.send(json.dumps(auth_msg))
            
            start_time = asyncio.get_event_loop().time()
            response = await asyncio.wait_for(self.websocket.recv(), timeout=10)
            auth_time = asyncio.get_event_loop().time() - start_time
            
            response_data = json.loads(response)
            self._debug_log(f"Auth response received in {auth_time:.3f}s", response_data)
            
            if response_data.get("success"):
                self.authenticated = True
                self.connection_alive = True
                self.last_activity = asyncio.get_event_loop().time()
                self._write_log("AUTH", "Authentication successful")
                self._update_status("Connected", True, "✓ Connected and authenticated")
                return True
            else:
                self._write_log("ERROR", "Authentication failed", str(response_data.get("error", {})))
                self._update_status("Auth Failed", True, "✗ Authentication failed")
                self.connection_alive = False
                return False
                
        except Exception as e:
            self._write_log("ERROR", f"Connection error: {str(e)}")
            self._debug_log(f"Connection exception: {type(e).__name__}: {str(e)}")
            self._update_status(f"Error: {str(e)[:30]}", True, f"✗ Connection error: {str(e)[:50]}")
            self.connection_alive = False
            return False
    
    async def re_authenticate(self) -> bool:
        """Re-authenticate using existing connection"""
        try:
            self._log_to_gui("Re-authenticating...")
            self._write_log("REAUTH", "Re-authentication requested by server")
            self._debug_log("Starting re-authentication")
            
            self.command_ref += 1
            cmd_ref = f"{self.site_name}_{self.command_ref}"
            
            auth_msg = {
                "command": "auth",
                "command_ref": cmd_ref,
                "args": {"token": self.token}
            }
            
            response_future = asyncio.Future()
            self.pending_responses[cmd_ref] = response_future
            
            self._debug_log("Sending re-auth", auth_msg)
            await self.websocket.send(json.dumps(auth_msg))
            
            try:
                response_data = await asyncio.wait_for(response_future, timeout=10)
                self._debug_log("Re-auth response received", response_data)
            except asyncio.TimeoutError:
                self.pending_responses.pop(cmd_ref, None)
                self._write_log("ERROR", "Re-authentication timeout")
                self._log_to_gui("✗ Re-authentication timeout")
                return False
            
            if response_data.get("success"):
                self.authenticated = True
                self.last_activity = asyncio.get_event_loop().time()
                self._write_log("REAUTH", "Re-authentication successful")
                self._log_to_gui("✓ Re-authenticated successfully")
                return True
            else:
                self._write_log("ERROR", "Re-authentication failed", str(response_data.get("error", {})))
                self._log_to_gui("✗ Re-authentication failed")
                return False
                
        except Exception as e:
            self._write_log("ERROR", f"Re-authentication error: {str(e)}")
            self._debug_log(f"Re-auth exception: {type(e).__name__}: {str(e)}")
            self._log_to_gui(f"✗ Re-authentication error: {str(e)[:50]}")
            return False
    
    def _compare_zones(self, current_zones: list) -> tuple:
        """Compare current zones with baseline"""
        if self.baseline["zones"] is None:
            return False, "Baseline established"
        
        baseline_ids = {z.get("id") for z in self.baseline["zones"]}
        current_ids = {z.get("id") for z in current_zones}
        
        added = current_ids - baseline_ids
        removed = baseline_ids - current_ids
        
        if added or removed:
            changes = []
            if added:
                changes.append(f"Added zones: {list(added)}")
            if removed:
                changes.append(f"Removed zones: {list(removed)}")
            return True, "; ".join(changes)
        
        return False, "No changes"
    
    @staticmethod
    def _tpo_status_row(t: dict) -> dict:
        """Stable, log-friendly subset of one TPO (matches WBA get_tpos fields)."""
        row = {
            "name": t.get("name"),
            "alive": t.get("alive"),
            "currentState": t.get("currentState"),
            "zone": t.get("zone"),
            "clusterName": t.get("clusterName"),
            "tpoDnsName": t.get("tpoDnsName"),
            "tssVersion": t.get("tssVersion"),
            "ipAddress": t.get("ipAddress"),
            "recordingServerEnabled": t.get("recordingServerEnabled"),
        }
        rs = t.get("recordingServer")
        if isinstance(rs, dict):
            row["recordingServerVendor"] = rs.get("vendor")
            row["recordingServerPrimary"] = rs.get("ipAddressPrimary")
        return row

    def _tpo_snapshot_all(self, tpos: list) -> List[dict]:
        """Sorted list of all TPO status rows (current known-good picture)."""
        rows = [self._tpo_status_row(t) for t in tpos if isinstance(t, dict)]
        rows.sort(key=lambda r: (r.get("name") is None, str(r.get("name") or "")))
        return rows

    @staticmethod
    def _tpo_aggregates(tpos: list) -> dict:
        """Roll-up counts for logging and GUI (alive/dead, currentState distribution)."""
        total = len(tpos)
        alive = dead = unset = 0
        by_state: Dict[str, int] = {}
        unnamed = 0
        for t in tpos:
            if not isinstance(t, dict):
                continue
            if not t.get("name"):
                unnamed += 1
            a = t.get("alive")
            if a is True:
                alive += 1
            elif a is False:
                dead += 1
            else:
                unset += 1
            st = t.get("currentState")
            key = st if st is not None else "(unset)"
            by_state[key] = by_state.get(key, 0) + 1
        return {
            "total": total,
            "alive": alive,
            "dead": dead,
            "alive_unset": unset,
            "unnamed_objects": unnamed,
            "by_current_state": by_state,
        }

    @staticmethod
    def _tpo_headline_summary(agg: dict) -> str:
        """Single-line human summary for file and activity log."""
        parts = [
            f"total={agg['total']}",
            f"alive={agg['alive']}",
            f"dead={agg['dead']}",
        ]
        if agg.get("alive_unset"):
            parts.append(f"alive_unset={agg['alive_unset']}")
        if agg.get("unnamed_objects"):
            parts.append(f"unnamed={agg['unnamed_objects']}")
        st = agg.get("by_current_state") or {}
        if st:
            ordered = sorted(st.items(), key=lambda kv: (str(kv[0]).lower(), kv[0]))
            parts.append("states=" + ",".join(f"{k}:{v}" for k, v in ordered))
        return " | ".join(parts)

    def _compare_tpos(self, current_tpos: list) -> tuple:
        """Compare current TPOs with baseline. Returns (changed, summary, change_lines)."""
        if self.baseline["tpos"] is None:
            return False, "First poll (baseline captured after this response)", []

        baseline_dict = {t.get("name"): t for t in self.baseline["tpos"] if t.get("name")}
        current_dict = {t.get("name"): t for t in current_tpos if t.get("name")}

        change_lines: List[str] = []

        baseline_names = set(baseline_dict.keys())
        current_names = set(current_dict.keys())

        added = sorted(current_names - baseline_names)
        removed = sorted(baseline_names - current_names)

        if added:
            change_lines.append(f"Added TPOs: {added}")
        if removed:
            change_lines.append(f"Removed TPOs: {removed}")

        for name in sorted(baseline_names & current_names):
            b = baseline_dict[name]
            c = current_dict[name]
            for field in TPO_COMPARE_FIELDS:
                bv = b.get(field)
                cv = c.get(field)
                if bv != cv:
                    change_lines.append(f"TPO '{name}' {field}: {bv!r} -> {cv!r}")

        if change_lines:
            return True, "; ".join(change_lines), change_lines

        return False, "No changes", []
    
    def reset_baseline(self):
        """Reset baseline for change detection"""
        self.baseline["zones"] = None
        self.baseline["tpos"] = None
        self._write_log("BASELINE", "Baseline reset")
        self._log_to_gui("🔄 Baseline reset - will be re-established on next cycle")
    
    def toggle_debug_mode(self):
        """Toggle debug mode on/off"""
        self.debug_mode = not self.debug_mode
        status = "enabled" if self.debug_mode else "disabled"
        self._write_log("DEBUG", f"Debug mode {status}")
        self._log_to_gui(f"🐛 Debug mode {status}")
        # Update config
        for site in self.app.config["sites"]:
            if site["name"] == self.site_name:
                site["debug_mode"] = self.debug_mode
                break
        self.app.populate_sites()
    
    async def subscribe_to_notifications(self, category: str) -> bool:
        """Subscribe to notification category"""
        if not self.authenticated:
            return False
        
        cmd_ref = None
        try:
            self.command_ref += 1
            cmd_ref = f"{self.site_name}_{self.command_ref}"
            
            msg = {
                "command": "subscribe",
                "command_ref": cmd_ref,
                "args": {"category": category}
            }
            
            response_future = asyncio.Future()
            self.pending_responses[cmd_ref] = response_future
            
            self._debug_log(f"Subscribing to {category}", msg)
            await self.websocket.send(json.dumps(msg))
            
            try:
                response_data = await asyncio.wait_for(response_future, timeout=10)
                self._debug_log(f"Subscribe response for {category}", response_data)
            except asyncio.TimeoutError:
                self.pending_responses.pop(cmd_ref, None)
                self._write_log("ERROR", f"Subscribe to {category} timeout")
                self._log_to_gui(f"✗ Subscribe to {category} timeout")
                return False
            
            if response_data.get("success"):
                self.subscribed_categories.add(category)
                last_id = response_data.get("data", {}).get("last_id", "N/A")
                self._write_log("SUBSCRIBE", f"Subscribed to {category}", f"Last ID: {last_id}")
                self._log_to_gui(f"📡 Subscribed to {category} notifications (last_id: {last_id})")
                
                # Special note for alerts
                if category == "alerts":
                    self._log_to_gui(f"ℹ️ Alert notifications active - ensure server has alerts enabled in TSS")
                
                return True
            else:
                error = response_data.get("error", {})
                error_msg = self._format_error(error)
                self._write_log("ERROR", f"Subscribe to {category} failed", error_msg)
                self._log_to_gui(f"✗ Subscribe to {category} failed: {error_msg}")
                
                # Special handling for alerts subscription failure
                if category == "alerts":
                    self._log_to_gui(f"⚠️ Check if server has: application.assure.alerts.enable.wba.notification = true")
                
                return False
                
        except Exception as e:
            if cmd_ref:
                self.pending_responses.pop(cmd_ref, None)
            self._write_log("ERROR", f"Subscribe to {category} error: {str(e)}")
            self._debug_log(f"Subscribe exception: {type(e).__name__}: {str(e)}")
            self._log_to_gui(f"✗ Subscribe to {category} error: {str(e)[:50]}")
            return False
    
    async def unsubscribe_from_notifications(self, category: str) -> bool:
        """Unsubscribe from notification category"""
        if not self.authenticated or category not in self.subscribed_categories:
            return False
        
        cmd_ref = None
        try:
            self.command_ref += 1
            cmd_ref = f"{self.site_name}_{self.command_ref}"
            
            msg = {
                "command": "unsubscribe",
                "command_ref": cmd_ref,
                "args": {"category": category}
            }
            
            response_future = asyncio.Future()
            self.pending_responses[cmd_ref] = response_future
            
            self._debug_log(f"Unsubscribing from {category}", msg)
            await self.websocket.send(json.dumps(msg))
            
            try:
                response_data = await asyncio.wait_for(response_future, timeout=10)
            except asyncio.TimeoutError:
                self.pending_responses.pop(cmd_ref, None)
                return False
            
            if response_data.get("success"):
                self.subscribed_categories.discard(category)
                self._write_log("UNSUBSCRIBE", f"Unsubscribed from {category}")
                return True
            else:
                return False
                
        except Exception as e:
            if cmd_ref:
                self.pending_responses.pop(cmd_ref, None)
            return False

    async def _resubscribe_notifications_after_reauth(self) -> None:
        """WBA spec: after successful re-authentication, subscribe again to notification categories."""
        subscriptions = self.app.get_subscriptions()
        if not subscriptions:
            return
        for category in subscriptions:
            await self.subscribe_to_notifications(category)
            await asyncio.sleep(0.5)
    
    async def handle_notification(self, notification_data: dict):
        """Handle incoming notifications"""
        category = notification_data.get("category", "unknown")
        
        if category == "alerts":
            events = notification_data.get("events", [])
            self._write_log("NOTIFICATION", f"Received {len(events)} alert notification(s)")
            for event in events:
                alert_msg = (f"ALERT: {event.get('alert_severity')} - "
                           f"{event.get('alert_alarm_name')} | "
                           f"{event.get('alert_message')} | "
                           f"Host: {event.get('hostname')} ({event.get('hostip')})")
                
                self._write_log("ALERT", f"Alert notification received", 
                              f"ID: {event.get('alert_id')}, Severity: {event.get('alert_severity')}", event)
                self._log_to_gui(f"🔴 {alert_msg}")
        
        elif category == "calls":
            events = notification_data.get("events", [])
            for event in events:
                call_info = (f"Call {event.get('call_ref')}: {event.get('state')} | "
                           f"{event.get('login')} | {event.get('local_extension')} -> "
                           f"{event.get('remote_extension')}")
                self._write_log("CALL_EVENT", "Call notification", call_info, event)
                self._log_to_gui(f"📞 {call_info}")
        
        elif category == "presences":
            events = notification_data.get("events", [])
            for event in events:
                presence_info = (f"{event.get('login')} on {event.get('turret')}: "
                               f"{event.get('state')}")
                self._write_log("PRESENCE", "Presence notification", presence_info, event)
                self._log_to_gui(f"👤 {presence_info}")
        
        else:
            # Unknown notification category
            self._write_log("NOTIFICATION", f"Received notification for unknown category: {category}", "", notification_data)
            self._log_to_gui(f"⚠️ Unknown notification category: {category}")
    
    async def execute_command(self, command: str) -> bool:
        """Execute a command"""
        if not self.authenticated or not self.websocket:
            return False
        
        # Parse command and extract category if needed
        base_command = command
        args = {}
        
        if ":" in command:
            parts = command.split(":", 1)
            base_command = parts[0]
            if base_command == "get_events":
                args["category"] = parts[1]
        
        for attempt in range(3):
            cmd_ref = None
            try:
                self.command_ref += 1
                cmd_ref = f"{self.site_name}_{self.command_ref}"
                
                msg = {
                    "command": base_command,
                    "command_ref": cmd_ref,
                    "args": args
                }
                
                response_future = asyncio.Future()
                self.pending_responses[cmd_ref] = response_future
                
                # Initialize batch tracking
                self.pending_batches[cmd_ref] = {
                    "batches": [],
                    "last_batch": None,
                    "complete": False
                }
                
                send_time = asyncio.get_event_loop().time()
                self._debug_log(f"Sending command: {command}", msg)
                await self.websocket.send(json.dumps(msg))
                
                # Wait for all batches to arrive
                try:
                    # Initial timeout for first response
                    response_data = await asyncio.wait_for(response_future, timeout=30)
                    response_time = asyncio.get_event_loop().time() - send_time
                    self._debug_log(f"First batch received for {command} in {response_time:.3f}s")
                    
                    # Check if this is a batched response
                    data = response_data.get("data", {})
                    current_batch = data.get("current_batch")
                    last_batch = data.get("last_batch")
                    
                    if current_batch and last_batch and last_batch > 1:
                        # Multi-batch response expected
                        self._log_to_gui(f"📦 {command} - Receiving batch {current_batch}/{last_batch}")
                        
                        # Wait for remaining batches with extended timeout
                        remaining_batches = last_batch - current_batch
                        timeout = 30 + (remaining_batches * 10)  # 10s per additional batch
                        
                        wait_start = asyncio.get_event_loop().time()
                        while not self.pending_batches[cmd_ref]["complete"]:
                            elapsed = asyncio.get_event_loop().time() - wait_start
                            if elapsed > timeout:
                                self._write_log("WARNING", f"Timeout waiting for all batches of {command}", 
                                              f"Received {len(self.pending_batches[cmd_ref]['batches'])}/{last_batch} batches")
                                self._log_to_gui(f"⚠️ {command} - Timeout, received {len(self.pending_batches[cmd_ref]['batches'])}/{last_batch} batches")
                                break
                            await asyncio.sleep(0.1)
                        
                        # Combine all batches
                        if self.pending_batches[cmd_ref]["batches"]:
                            response_data = self._combine_batches(cmd_ref, response_data)
                            total_time = asyncio.get_event_loop().time() - send_time
                            self._debug_log(f"All {last_batch} batches received for {command} in {total_time:.3f}s")
                            self._log_to_gui(f"✓ {command} - Received all {last_batch} batches")
                    
                except asyncio.TimeoutError:
                    self.pending_responses.pop(cmd_ref, None)
                    self.pending_batches.pop(cmd_ref, None)
                    self._write_log("ERROR", f"Command {command} timeout (attempt {attempt + 1}/3)")
                    if attempt < 2:
                        self._log_to_gui(f"⏳ {command} timeout, retry {attempt + 1}/3")
                        await asyncio.sleep(2)
                        continue
                    else:
                        self._log_to_gui(f"✗ {command} timeout after 3 attempts")
                        return False
                finally:
                    # Cleanup batch tracking
                    self.pending_batches.pop(cmd_ref, None)
                
                self.last_activity = asyncio.get_event_loop().time()
                
                if response_data.get("success"):
                    data = response_data.get("data", {})
                    
                    # Check for data status warnings
                    status = data.get("status", "ok")
                    if status == "Maximum limit exceeded":
                        self._log_to_gui(f"⚠️ {command} - Data limit exceeded, results truncated")
                        self._write_log("WARNING", f"[{command}] Maximum limit exceeded")
                    
                    # Format summary based on command type
                    if base_command == "get_zones":
                        zones = data.get("zones", [])
                        zone_count = len(zones)
                        
                        changed, change_details = self._compare_zones(zones)
                        
                        if changed:
                            self._write_log("CRITICAL", f"[{command}] ZONES CHANGED", 
                                          f"Count: {zone_count} | {change_details}", response_data)
                            self._log_to_gui(f"🔴 CRITICAL: Zones changed - {change_details}")
                        else:
                            self._write_log("COMMAND", f"[{command}]", 
                                          f"Count: {zone_count} | {change_details}", response_data)
                            self._log_to_gui(f"✓ {command} - Zones: {zone_count}")
                        
                        if self.baseline["zones"] is None:
                            self.baseline["zones"] = zones
                            self._log_to_gui(f"📊 Baseline established for zones ({zone_count} zones)")
                    
                    elif base_command == "get_tpos":
                        tpos = data.get("tpos", [])
                        tpo_count = len(tpos)
                        establishing = self.baseline["tpos"] is None
                        changed, change_summary, change_lines = self._compare_tpos(tpos)
                        rows = self._tpo_snapshot_all(tpos)
                        agg = self._tpo_aggregates(tpos)
                        headline = self._tpo_headline_summary(agg)

                        tpo_payload = {
                            "kind": "get_tpos_snapshot",
                            "aggregates": agg,
                            "tpos": rows,
                        }
                        if self.debug_mode:
                            self._debug_log("get_tpos raw data", data)

                        if changed:
                            tpo_payload["changes"] = change_lines
                            tpo_payload["change_summary"] = change_summary
                            self._write_log(
                                "CRITICAL",
                                f"[{command}] TPOs CHANGED",
                                f"{headline} | {change_summary}",
                            )
                            self._write_log(
                                "COMMAND",
                                f"[{command}] TPO change — full snapshot + diff",
                                headline,
                                tpo_payload,
                            )
                            self._log_to_gui(f"🔴 CRITICAL: TPOs changed — {change_summary}")
                        elif establishing:
                            tpo_payload["note"] = "Baseline captured after this entry; future polls compare against this."
                            self._write_log(
                                "COMMAND",
                                f"[{command}] Baseline snapshot (all TPOs)",
                                headline,
                                tpo_payload,
                            )
                            self._log_to_gui(f"📊 {command}: {headline} — baseline stored")
                        else:
                            tpo_payload["note"] = "No differences vs baseline."
                            self._write_log(
                                "COMMAND",
                                f"[{command}] OK — matches baseline",
                                f"{headline} | {change_summary}",
                                tpo_payload,
                            )
                            self._log_to_gui(f"✓ {command}: {headline}")

                        if self.baseline["tpos"] is None:
                            self.baseline["tpos"] = tpos
                    
                    elif base_command == "get_turrets":
                        turrets = data.get("turrets", [])
                        turret_count = len(turrets)
                        alive_count = sum(1 for t in turrets if t.get("alive"))
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Total: {turret_count}, Alive: {alive_count}", response_data)
                        self._log_to_gui(f"✓ {command} - Turrets: {turret_count} (Alive: {alive_count})")
                    
                    elif base_command == "get_users":
                        users = data.get("users", [])
                        user_count = len(users)
                        active_count = sum(1 for u in users if u.get("status") == "ACTIVE")
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Total: {user_count}, Active: {active_count}", response_data)
                        self._log_to_gui(f"✓ {command} - Users: {user_count} (Active: {active_count})")
                    
                    elif base_command == "get_calls":
                        calls = data.get("calls", [])
                        call_count = len(calls)
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Total calls: {call_count}", response_data)
                        self._log_to_gui(f"✓ {command} - {call_count} calls")
                    
                    elif base_command == "get_version":
                        components = len(data.keys())
                        version_summary = ", ".join([f"{k}:{v}" for k, v in list(data.items())[:3]])
                        if len(data.keys()) > 3:
                            version_summary += "..."
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Components: {components} | {version_summary}", response_data)
                        self._log_to_gui(f"✓ {command} - {components} components")
                    
                    elif base_command == "get_health_api_report":
                        platform_name = list(data.keys())[0] if data else "Unknown"
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Platform: {platform_name} | Report received", response_data)
                        self._log_to_gui(f"✓ {command} - Health report received")
                    
                    elif base_command == "get_events":
                        events = data.get("events", [])
                        event_count = len(events)
                        category = args.get("category", "unknown")
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Category: {category}, Count: {event_count}", response_data)
                        self._log_to_gui(f"✓ {command} - {event_count} events")
                    
                    elif base_command == "get_lines":
                        lines = data.get("lines", [])
                        line_count = len(lines)
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Total lines: {line_count}", response_data)
                        self._log_to_gui(f"✓ {command} - {line_count} lines")
                    
                    elif base_command == "get_shared_profiles":
                        profiles = data.get("sharedprofiles", [])
                        profile_count = len(profiles)
                        self._write_log("COMMAND", f"[{command}]", 
                                      f"Total profiles: {profile_count}", response_data)
                        self._log_to_gui(f"✓ {command} - {profile_count} shared profiles")
                    
                    else:
                        # Generic handling for other commands
                        summary = str(data)[:100]
                        self._write_log("COMMAND", f"[{command}]", f"Success | {summary}", response_data)
                        self._log_to_gui(f"✓ {command} completed")
                    
                    return True
                else:
                    error = response_data.get("error", {})
                    error_msg = self._format_error(error)
                    self._write_log("ERROR", f"Command {command} failed", error_msg, response_data)
                    
                    err_code = self._error_code(error)
                    # Spec (Get Calls error handling): 401 → re-authenticate and subscribe to notifications
                    if err_code == 401:
                        self._log_to_gui("401 Unauthorized — re-authenticating and re-subscribing per API spec...")
                        if await self.re_authenticate():
                            try:
                                await self._resubscribe_notifications_after_reauth()
                            except Exception as e:
                                self._write_log("ERROR", f"Re-subscribe after 401: {str(e)}")
                            continue
                    
                    if err_code == 498:
                        self._log_to_gui(f"⚠️ Token error detected, will reconnect...")
                        raise ConnectionError("Invalid token, reconnection required")
                    
                    if attempt < 2:
                        self._log_to_gui(f"⏳ {command} retry {attempt + 1}/3")
                        await asyncio.sleep(2)
                    else:
                        self._log_to_gui(f"✗ {command} failed: {error_msg}")
                    
            except Exception as e:
                if cmd_ref:
                    self.pending_responses.pop(cmd_ref, None)
                    self.pending_batches.pop(cmd_ref, None)
                self._write_log("ERROR", f"Command {command} error: {str(e)}")
                self._debug_log(f"Command exception: {type(e).__name__}: {str(e)}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    self._log_to_gui(f"✗ {command} error: {str(e)[:50]}")
        
        return False
    
    def _combine_batches(self, cmd_ref: str, initial_response: dict) -> dict:
        """Combine multiple batches into a single response"""
        batch_info = self.pending_batches.get(cmd_ref, {})
        batches = batch_info.get("batches", [])
        
        if not batches:
            return initial_response
        
        # Start with the initial response structure
        combined = initial_response.copy()
        data = combined.get("data", {})
        
        # Determine what type of data to combine based on keys present
        if "users" in data:
            all_users = data.get("users", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_users.extend(batch_data.get("users", []))
            data["users"] = all_users
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "calls" in data:
            all_calls = data.get("calls", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_calls.extend(batch_data.get("calls", []))
            data["calls"] = all_calls
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "events" in data:
            all_events = data.get("events", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_events.extend(batch_data.get("events", []))
            data["events"] = all_events
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "turrets" in data:
            all_turrets = data.get("turrets", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_turrets.extend(batch_data.get("turrets", []))
            data["turrets"] = all_turrets
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "lines" in data:
            all_lines = data.get("lines", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_lines.extend(batch_data.get("lines", []))
            data["lines"] = all_lines
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "sharedprofiles" in data:
            all_profiles = data.get("sharedprofiles", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_profiles.extend(batch_data.get("sharedprofiles", []))
            data["sharedprofiles"] = all_profiles
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "tpos" in data:
            all_tpos = data.get("tpos", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_tpos.extend(batch_data.get("tpos", []))
            data["tpos"] = all_tpos
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        elif "zones" in data:
            all_zones = data.get("zones", [])
            for batch in batches:
                batch_data = batch.get("data", {})
                all_zones.extend(batch_data.get("zones", []))
            data["zones"] = all_zones
            data["current_batch"] = batch_info.get("last_batch", 1)
        
        combined["data"] = data
        return combined
    
    async def run(self):
        """Main run loop with auto-reconnect"""
        self.running = True
        
        while self.running and self.restart_attempts <= self.max_restart_attempts:
            try:
                if not await self.connect_and_auth():
                    if self.auto_start and self.restart_attempts < self.max_restart_attempts:
                        self.restart_attempts += 1
                        wait_time = min(10 * self.restart_attempts, 30)
                        self._log_to_gui(f"🔄 Auto-reconnect attempt {self.restart_attempts}/{self.max_restart_attempts} in {wait_time}s...")
                        self._write_log("RECONNECT", f"Auto-reconnect attempt {self.restart_attempts}/{self.max_restart_attempts}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        self.running = False
                        return
                
                if self.restart_attempts > 0:
                    self._log_to_gui(f"✓ Reconnected successfully after {self.restart_attempts} attempts")
                    self._write_log("RECONNECT", f"Reconnected successfully after {self.restart_attempts} attempts")
                self.restart_attempts = 0
                
                # Subscribe to notifications
                subscriptions = self.app.get_subscriptions()
                self._write_log("INFO", f"Notification subscriptions configured: {subscriptions or 'None'}")
                
                if subscriptions:
                    self._log_to_gui(f"📡 Subscribing to {len(subscriptions)} notification category(ies)...")
                    for category in subscriptions:
                        success = await self.subscribe_to_notifications(category)
                        if not success:
                            self._log_to_gui(f"⚠️ Failed to subscribe to {category} - will continue without it")
                        await asyncio.sleep(0.5)
                    
                    # Verify what we successfully subscribed to
                    if self.subscribed_categories:
                        self._log_to_gui(f"✓ Active subscriptions: {', '.join(self.subscribed_categories)}")
                    else:
                        self._log_to_gui("⚠️ Warning: No active subscriptions")
                else:
                    self._log_to_gui("ℹ️ No notification subscriptions configured")
                
                # Site-specific: interval vs daily (extended/historical)
                interval_cmds = self.interval_commands
                daily_specs = self.daily_commands
                self._log_to_gui(
                    f"Started. Interval: {len(interval_cmds)} cmd(s) every {self.app.get_interval()} min · "
                    f"Daily: {len(daily_specs)} cmd(s) at local time"
                )
                self._debug_log(
                    "Command plan",
                    {"interval": interval_cmds, "daily": [f"{c}@{t}" for c, t in daily_specs]},
                )
                
                receive_task = asyncio.create_task(self._receive_messages())
                
                try:
                    last_command_time = 0
                    interval = self.app.get_interval() * 60
                    last_health_check = asyncio.get_event_loop().time()
                    
                    # Note: Server sends ping frames every 5 seconds (configurable via application.global.wba.ping.interval)
                    # The websockets library automatically responds to these pings
                    # We only passively monitor connection state, not actively ping
                    # This avoids interfering with the server's re-authentication mechanism
                    
                    while self.running:
                        current_time = asyncio.get_event_loop().time()
                        now_local = datetime.now()

                        # Once-per-day commands (extended/historical), local clock
                        for cmd, time_str in self.daily_commands:
                            if not self.running:
                                break
                            if self._daily_fire(cmd, time_str, now_local):
                                if self.authenticated and self.connection_alive:
                                    self._log_to_gui(f"📅 Daily run: {cmd} (scheduled {time_str} local)")
                                    await self.execute_command(cmd)
                                    await asyncio.sleep(1)

                        if current_time - last_command_time >= interval:
                            self._log_to_gui(f"=== Starting interval command cycle ===")
                            self.log.retention_days = self.app.get_log_retention_days()
                            self.log.cleanup_old_logs()
                            for cmd in self.interval_commands:
                                if self.running and self.authenticated and self.connection_alive:
                                    await self.execute_command(cmd)
                                    await asyncio.sleep(1)
                                else:
                                    self._log_to_gui("Skipping commands - connection not ready")
                                    break

                            last_command_time = current_time
                            interval = self.app.get_interval() * 60
                            self._log_to_gui(
                                f"=== Interval cycle complete. Next in {self.app.get_interval()} minutes ==="
                            )
                        
                        # Lightweight connection check every 30 seconds
                        if current_time - last_health_check >= 30:
                            # Just verify websocket state without aggressive pinging
                            try:
                                if not self.websocket:
                                    self._write_log("ERROR", "WebSocket object is None")
                                    self._update_status("Disconnected", True, "✗ WebSocket closed")
                                    self.connection_alive = False
                                    raise ConnectionError("WebSocket connection closed")
                                
                                # Check if websocket has closed property and is closed
                                if hasattr(self.websocket, 'closed') and self.websocket.closed:
                                    self._write_log("ERROR", "WebSocket detected as closed")
                                    self._update_status("Disconnected", True, "✗ WebSocket closed")
                                    self.connection_alive = False
                                    raise ConnectionError("WebSocket connection closed")
                                
                                # Alternative check: try to access the state
                                if hasattr(self.websocket, 'state'):
                                    from websockets.protocol import State
                                    if self.websocket.state != State.OPEN:
                                        self._write_log("ERROR", f"WebSocket state is {self.websocket.state}")
                                        self._update_status("Disconnected", True, f"✗ WebSocket state: {self.websocket.state}")
                                        self.connection_alive = False
                                        raise ConnectionError("WebSocket not in OPEN state")
                            except (AttributeError, ImportError) as e:
                                # If we can't check state, just continue - connection will fail naturally if broken
                                self._debug_log(f"Could not check websocket state: {e}")
                            
                            # Only do ping if no activity for 5 minutes (not 2)
                            if self.last_activity and (current_time - self.last_activity) >= 300:
                                try:
                                    pong = await asyncio.wait_for(self.websocket.ping(), timeout=10)
                                    await asyncio.wait_for(pong, timeout=10)
                                    self._write_log("HEALTH", "Keepalive ping successful")
                                    self._debug_log("WebSocket keepalive ping successful")
                                    self.last_activity = current_time
                                    self.connection_alive = True
                                except (asyncio.TimeoutError, Exception) as e:
                                    self._write_log("WARNING", f"Keepalive ping failed: {str(e)}")
                                    self._debug_log(f"WebSocket keepalive ping failed: {str(e)}")
                                    # Don't immediately disconnect - wait for server to notify us
                                    # The server's re-auth mechanism should handle session management
                            
                            last_health_check = current_time
                        
                        await asyncio.sleep(1)
                        
                except Exception as e:
                    self._write_log("ERROR", f"Run loop error: {str(e)}")
                    self._debug_log(f"Run loop exception: {type(e).__name__}: {str(e)}")
                    self._log_to_gui(f"✗ Error: {str(e)[:50]}")
                    
                    if self.auto_start and self.restart_attempts < self.max_restart_attempts:
                        self._log_to_gui("Connection lost, attempting to reconnect...")
                    else:
                        self.running = False
                        
                finally:
                    receive_task.cancel()
                    try:
                        await receive_task
                    except asyncio.CancelledError:
                        pass
                    
                    for cmd_ref, future in list(self.pending_responses.items()):
                        if not future.done():
                            future.cancel()
                    self.pending_responses.clear()
                    self.pending_batches.clear()
                    
                    if self.websocket:
                        try:
                            await self.websocket.close()
                        except:
                            pass
                    
                    self.authenticated = False
                    self.connection_alive = False
                    
                    if not (self.auto_start and self.restart_attempts < self.max_restart_attempts and self.running):
                        self._update_status("Stopped", True, "Stopped")
                        
            except Exception as e:
                self._write_log("ERROR", f"Unexpected error in run loop: {str(e)}")
                self._debug_log(f"Outer run loop exception: {type(e).__name__}: {str(e)}")
                self._log_to_gui(f"✗ Unexpected error: {str(e)[:50]}")
                
                if self.auto_start and self.restart_attempts < self.max_restart_attempts:
                    self.restart_attempts += 1
                    wait_time = min(10 * self.restart_attempts, 30)
                    self._log_to_gui(f"🔄 Auto-reconnect attempt {self.restart_attempts}/{self.max_restart_attempts} in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    self.running = False
        
        if self.restart_attempts > self.max_restart_attempts:
            self._write_log("ERROR", "Max restart attempts reached, giving up")
            self._update_status("Failed (Max retries)", True, 
                              f"✗ Failed after {self.max_restart_attempts} reconnection attempts")
            self.running = False
    
    async def _receive_messages(self):
        """Continuously receive and handle incoming messages"""
        try:
            while self.running:
                try:
                    # Increased timeout since server pings every 5s
                    message = await asyncio.wait_for(self.websocket.recv(), timeout=30.0)
                    
                    # Handle binary/ping frames (should be automatic but just in case)
                    if isinstance(message, bytes):
                        self.last_activity = asyncio.get_event_loop().time()
                        self._debug_log("Received binary frame (ping/pong)")
                        continue
                    
                    data = json.loads(message)
                    self.last_activity = asyncio.get_event_loop().time()
                    self._debug_log("Received message", data)
                    
                    # Handle command responses (some doc examples use "return" for subscribe; servers typically use "response")
                    if data.get("command") in ("response", "return"):
                        cmd_ref = data.get("command_ref")
                        if cmd_ref in self.pending_responses:
                            # Check if this is a batched response
                            response_data = data.get("data", {})
                            current_batch = response_data.get("current_batch")
                            last_batch = response_data.get("last_batch")
                            
                            if current_batch and last_batch and last_batch > 1:
                                # Multi-batch response
                                if cmd_ref in self.pending_batches:
                                    batch_info = self.pending_batches[cmd_ref]
                                    batch_info["last_batch"] = last_batch
                                    
                                    if current_batch == 1:
                                        # First batch - resolve the future so execute_command continues
                                        future = self.pending_responses.pop(cmd_ref)
                                        if not future.done():
                                            future.set_result(data)
                                    else:
                                        # Subsequent batch - store it
                                        batch_info["batches"].append(data)
                                        self._debug_log(f"Received batch {current_batch}/{last_batch} for {cmd_ref}")
                                        self._log_to_gui(f"📦 Batch {current_batch}/{last_batch} received")
                                    
                                    # Check if all batches received
                                    if current_batch == last_batch:
                                        batch_info["complete"] = True
                                        self._debug_log(f"All batches complete for {cmd_ref}")
                            else:
                                # Single response or first batch
                                future = self.pending_responses.pop(cmd_ref)
                                if not future.done():
                                    future.set_result(data)
                    
                    # Handle notifications
                    elif data.get("command") == "notify":
                        await self.handle_notification(data.get("data", {}))
                    
                    # Handle server notifications
                    elif data.get("command") == "server notification":
                        msg = data.get("message", "")
                        cmd_ref = data.get("command_ref", "")
                        self._write_log("SERVER_NOTIFY", "Server notification", msg)
                        self._log_to_gui(f"⚠️ Server: {msg}")
                        
                        if "re-authenticate" in msg.lower() or cmd_ref == "authentication expiry":
                            self._log_to_gui("Re-authentication requested by server")
                            if await self.re_authenticate():
                                subscriptions = self.app.get_subscriptions()
                                if subscriptions:
                                    self._log_to_gui(f"Re-subscribing to {len(subscriptions)} notification categories")
                                try:
                                    await self._resubscribe_notifications_after_reauth()
                                except Exception as e:
                                    self._write_log("ERROR", f"Re-subscribe after re-auth failed: {str(e)}")
                                    self._log_to_gui(f"⚠️ Re-subscribe error: {str(e)[:80]}")
                            else:
                                # Re-authentication failed - the connection will be closed by server
                                self._log_to_gui("Re-authentication failed - server will close connection")
                                self.connection_alive = False
                                # Don't raise exception - let server close the connection properly
                        
                        elif "session is expired" in msg.lower() or cmd_ref == "Session expired":
                            self._log_to_gui("Session expired by server - reconnecting...")
                            self.connection_alive = False
                            self.authenticated = False
                            # Connection will be closed by server, which will trigger reconnection
                            # Don't raise exception here - just mark as not connected
                    
                except asyncio.TimeoutError:
                    # No message received in 30 seconds - this is OK since we get server pings
                    # Just check if we're still supposed to be running
                    if not self.running:
                        break
                    # Check last activity - if no activity for 2 minutes, something might be wrong
                    if self.last_activity:
                        idle_time = asyncio.get_event_loop().time() - self.last_activity
                        if idle_time > 120:
                            self._write_log("WARNING", f"No activity for {int(idle_time)}s (but connection may be OK)")
                    continue
                    
                except json.JSONDecodeError as e:
                    self.last_activity = asyncio.get_event_loop().time()
                    self._debug_log(f"Non-JSON message received: {e}")
                    continue
                    
                except websockets.exceptions.ConnectionClosed as e:
                    self.connection_alive = False
                    self._write_log("ERROR", f"WebSocket connection closed: code={e.code}, reason={e.reason}")
                    self._update_status("Disconnected", True, f"✗ Connection closed (code={e.code})")
                    self._log_to_gui(f"✗ Connection closed by server: {e.reason or 'No reason provided'}")
                    break
                except websockets.exceptions.ConnectionClosedOK:
                    self.connection_alive = False
                    self._write_log("INFO", "WebSocket connection closed normally")
                    self._update_status("Disconnected", True, "Connection closed normally")
                    break
                except websockets.exceptions.ConnectionClosedError as e:
                    self.connection_alive = False
                    self._write_log("ERROR", f"WebSocket connection closed with error: {str(e)}")
                    self._update_status("Disconnected", True, "✗ Connection error")
                    break
                    
        except asyncio.CancelledError:
            self.connection_alive = False
            raise
        except Exception as e:
            self.connection_alive = False
            self._write_log("ERROR", f"Receive messages error: {str(e)}")
            self._debug_log(f"Receive exception: {type(e).__name__}: {str(e)}")
            self._log_to_gui(f"✗ Connection error: {str(e)[:50]}")
            self._update_status("Disconnected", True, "✗ Connection lost")
            raise
    
    async def stop(self):
        """Stop the connection gracefully"""
        self._log_to_gui("Stopping connection gracefully...")
        self.running = False
        self.restart_attempts = self.max_restart_attempts + 1
        
        # Unsubscribe from all notifications before closing
        if self.websocket and self.authenticated and self.connection_alive:
            try:
                self._write_log("SHUTDOWN", "Graceful shutdown initiated - unsubscribing from notifications")
                for category in list(self.subscribed_categories):
                    try:
                        await asyncio.wait_for(
                            self.unsubscribe_from_notifications(category), 
                            timeout=2
                        )
                    except asyncio.TimeoutError:
                        self._write_log("WARNING", f"Timeout unsubscribing from {category}")
                    except Exception as e:
                        self._write_log("WARNING", f"Error unsubscribing from {category}: {str(e)}")
                
                # Small delay to let unsubscribe messages be processed
                await asyncio.sleep(0.5)
            except Exception as e:
                self._write_log("WARNING", f"Error during graceful shutdown: {str(e)}")
        
        # Clean up pending operations
        for cmd_ref, future in list(self.pending_responses.items()):
            if not future.done():
                future.cancel()
        self.pending_responses.clear()
        self.pending_batches.clear()
        
        # Close websocket
        if self.websocket:
            try:
                await self.websocket.close()
                self._write_log("SHUTDOWN", "WebSocket connection closed")
            except Exception as e:
                self._write_log("WARNING", f"Error closing websocket: {str(e)}")
        
        self.authenticated = False
        self.connection_alive = False
        self._update_status("Stopped", True, "✓ Stopped gracefully")


class WBALoggerApp:
    """Main application with GUI"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("WBA Logger - Enhanced")
        self.root.geometry("1000x800")
        
        self.config = self.load_config()
        self.validate_config()
        self.connections: Dict[str, SiteConnection] = {}
        self.loop = None
        self.loop_thread = None
        
        self.create_gui()
        self.populate_sites()
        
        self.start_event_loop()
        self.root.after(1000, self.auto_start_sites)
        
    def load_config(self) -> Dict:
        """Load configuration"""
        if not Path(CONFIG_FILE).exists():
            return {
                "sites": [],
                "interval_minutes": 5,
                "subscriptions": ["alerts"],
                "log_retention_days": DEFAULT_LOG_RETENTION_DAYS,
            }
        
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        if "subscriptions" not in config:
            config["subscriptions"] = ["alerts"]
        if "log_retention_days" not in config:
            config["log_retention_days"] = DEFAULT_LOG_RETENTION_DAYS
        
        # Migrate old configs: move global commands to each site
        if "commands" in config:
            global_commands = config.pop("commands")
            for site in config.get("sites", []):
                if "commands" not in site:
                    site["commands"] = global_commands.copy()
        
        # Ensure each site has commands
        for site in config.get("sites", []):
            if "commands" not in site or not site["commands"]:
                site["commands"] = DEFAULT_COMMANDS.copy()
            migrate_site_command_settings(site)
        
        return config
    
    def validate_config(self):
        """Validate configuration for common errors"""
        errors = []
        
        # Check for duplicate site names
        site_names = [s.get("name", "") for s in self.config.get("sites", [])]
        duplicates = [name for name in site_names if site_names.count(name) > 1]
        if duplicates:
            errors.append(f"Duplicate site names found: {set(duplicates)}")
        
        # Validate sites
        for site in self.config.get("sites", []):
            name = site.get("name", "")
            url = site.get("url", "")
            token = site.get("token", "")
            
            if not name:
                errors.append("Site with empty name found")
            if not url or not (url.startswith("ws://") or url.startswith("wss://")):
                errors.append(f"Site '{name}': Invalid URL (must start with ws:// or wss://)")
            if not token:
                errors.append(f"Site '{name}': Empty token")
        
        # Validate interval
        interval = self.config.get("interval_minutes", 5)
        if interval < 1 or interval > 60:
            errors.append(f"Invalid interval: {interval} (must be 1-60 minutes)")
            self.config["interval_minutes"] = max(1, min(60, interval))
        
        try:
            lr = int(self.config.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS))
        except (TypeError, ValueError):
            lr = DEFAULT_LOG_RETENTION_DAYS
            errors.append("Invalid log_retention_days; reset to default")
        if lr < 0:
            errors.append("log_retention_days cannot be negative (use 0 to disable auto-delete)")
            lr = 0
        if lr > MAX_LOG_RETENTION_DAYS:
            errors.append(f"log_retention_days capped at {MAX_LOG_RETENTION_DAYS}")
            lr = MAX_LOG_RETENTION_DAYS
        self.config["log_retention_days"] = lr
        
        if errors:
            error_msg = "Configuration validation errors:\n\n" + "\n".join(f"• {e}" for e in errors)
            messagebox.showwarning("Configuration Issues", error_msg)
    
    def save_config(self):
        """Save configuration"""
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=2)
        messagebox.showinfo("Saved", "Configuration saved successfully")
    
    def create_gui(self):
        """Create GUI"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Save Configuration", command=self.save_config)
        file_menu.add_command(label="Open Logs Folder", command=self.open_logs_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_closing)
        
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Settings frame (interval only)
        settings_frame = ttk.LabelFrame(main_frame, text="Global Settings", padding="10")
        settings_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Interval setting
        interval_frame = ttk.Frame(settings_frame)
        interval_frame.pack(fill=tk.X)
        
        ttk.Label(interval_frame, text="Command Interval (minutes):").pack(side=tk.LEFT, padx=5)
        self.interval_var = tk.IntVar(value=self.config.get("interval_minutes", 5))
        interval_spin = ttk.Spinbox(interval_frame, from_=1, to=60, textvariable=self.interval_var, width=10)
        interval_spin.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(interval_frame, text="Save global settings", command=self.save_settings).pack(side=tk.LEFT, padx=20)
        
        ttk.Label(interval_frame, text="(Commands are configured per-site)", 
                 font=('TkDefaultFont', 8, 'italic')).pack(side=tk.LEFT, padx=20)
        
        retention_frame = ttk.Frame(settings_frame)
        retention_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(retention_frame, text="Log retention (days):").pack(side=tk.LEFT, padx=5)
        self.log_retention_var = tk.IntVar(value=int(self.config.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)))
        retention_spin = ttk.Spinbox(
            retention_frame,
            from_=0,
            to=MAX_LOG_RETENTION_DAYS,
            textvariable=self.log_retention_var,
            width=10,
        )
        retention_spin.pack(side=tk.LEFT, padx=5)
        ttk.Label(
            retention_frame,
            text="0 = never delete by age · 1+ = delete .log files older than this many days (each site folder)",
            font=("TkDefaultFont", 8, "italic"),
        ).pack(side=tk.LEFT, padx=(10, 0))
        
        # Subscriptions section
        subs_frame = ttk.LabelFrame(settings_frame, text="Alert Notifications", padding="10")
        subs_frame.pack(fill=tk.X, pady=(10, 0))
        
        self.sub_alerts_var = tk.BooleanVar(value="alerts" in self.config.get("subscriptions", []))
        ttk.Checkbutton(subs_frame, text="Subscribe to alert notifications", 
                       variable=self.sub_alerts_var).pack(anchor=tk.W)
        
        ttk.Label(subs_frame, text="↳ Receives real-time alert notifications from TradeSense Assure", 
                 font=('TkDefaultFont', 8, 'italic')).pack(anchor=tk.W, padx=(20, 0))
        
        ttk.Label(subs_frame, text="⚠️ Server must have application.assure.alerts.enable.wba.notification = true", 
                 font=('TkDefaultFont', 8, 'italic'),
                 foreground='red').pack(anchor=tk.W, padx=(20, 0))
        
        ttk.Button(subs_frame, text="Save Subscriptions", command=self.save_subscriptions).pack(anchor=tk.W, pady=(5, 0))
        
        # Sites frame
        sites_frame = ttk.LabelFrame(main_frame, text="Sites", padding="10")
        sites_frame.pack(fill=tk.BOTH, expand=True)
        
        btn_frame = ttk.Frame(sites_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Button(btn_frame, text="Add Site", command=self.add_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Edit Site", command=self.edit_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Remove Site", command=self.remove_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Toggle Debug", command=self.toggle_debug).pack(side=tk.LEFT, padx=20)
        ttk.Button(btn_frame, text="Reset Baseline", command=self.reset_baseline).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Start Site", command=self.start_site).pack(side=tk.LEFT, padx=20)
        ttk.Button(btn_frame, text="Stop Site", command=self.stop_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Start All", command=self.start_all).pack(side=tk.LEFT, padx=20)
        ttk.Button(btn_frame, text="Stop All", command=self.stop_all).pack(side=tk.LEFT, padx=5)
        
        list_frame = ttk.Frame(sites_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        
        columns = ("name", "url", "status", "debug", "commands")
        self.sites_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=8)
        
        self.sites_tree.heading("name", text="Site Name")
        self.sites_tree.heading("url", text="URL")
        self.sites_tree.heading("status", text="Status")
        self.sites_tree.heading("debug", text="Debug")
        self.sites_tree.heading("commands", text="Commands")
        
        self.sites_tree.column("name", width=150)
        self.sites_tree.column("url", width=300)
        self.sites_tree.column("status", width=120)
        self.sites_tree.column("debug", width=60)
        self.sites_tree.column("commands", width=80)
        
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.sites_tree.yview)
        self.sites_tree.configure(yscrollcommand=scrollbar.set)
        
        self.sites_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Log viewer
        log_frame = ttk.LabelFrame(main_frame, text="Activity Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        
        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Button(log_btn_frame, text="Clear Log", command=self.clear_log).pack(side=tk.LEFT)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, width=100, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        info_label = ttk.Label(main_frame, 
                               text="Commands are configured per-site in Add/Edit Site dialog. Extended/historical commands can use the global interval or run once daily at a local HH:MM. Alert subscriptions are global.\n"
                                    "Baseline is established on first run. Changes to Zones/TPOs are logged as CRITICAL.\n"
                                    "Sites with Auto-Start enabled will reconnect automatically (up to 3 attempts). "
                                    "Toggle Debug to enable/disable verbose logging for running sites.\n"
                                    "Connection health is monitored every 30 seconds. Select a site and use File > Open Logs Folder to view site-specific logs.",
                               font=('TkDefaultFont', 8, 'italic'))
        info_label.pack(pady=(5, 0))
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def clear_log(self):
        """Clear the activity log"""
        self.log_text.delete("1.0", tk.END)
        self.log_activity("Activity log cleared")
    
    def reset_baseline(self):
        """Reset baseline for selected site"""
        selection = self.sites_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a site to reset baseline")
            return
        
        index = self.sites_tree.index(selection[0])
        site = self.config["sites"][index]
        
        if site["name"] in self.connections and self.connections[site["name"]].running:
            if messagebox.askyesno("Confirm", f"Reset baseline for '{site['name']}'?\n\nBaseline will be re-established on next command cycle."):
                self.connections[site["name"]].reset_baseline()
        else:
            messagebox.showinfo("Info", "Site must be running to reset baseline")
    
    def toggle_debug(self):
        """Toggle debug mode for selected site"""
        selection = self.sites_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a site to toggle debug mode")
            return
        
        index = self.sites_tree.index(selection[0])
        site = self.config["sites"][index]
        
        if site["name"] in self.connections and self.connections[site["name"]].running:
            self.connections[site["name"]].toggle_debug_mode()
            self.save_config()
        else:
            # Toggle in config for stopped sites
            site["debug_mode"] = not site.get("debug_mode", False)
            status = "enabled" if site["debug_mode"] else "disabled"
            self.populate_sites()
            self.save_config()
            self.log_activity(f"Debug mode {status} for '{site['name']}' (will take effect on next start)")
    
    def save_settings(self):
        """Save global settings (interval, log retention)."""
        self.config["interval_minutes"] = self.interval_var.get()
        try:
            lr = int(self.log_retention_var.get())
        except (tk.TclError, TypeError, ValueError):
            lr = DEFAULT_LOG_RETENTION_DAYS
        self.config["log_retention_days"] = max(0, min(MAX_LOG_RETENTION_DAYS, lr))
        self.log_retention_var.set(self.config["log_retention_days"])
        self.save_config()
        lr_note = (
            "retention off (no age-based delete)"
            if self.config["log_retention_days"] == 0
            else f"delete logs older than {self.config['log_retention_days']} day(s)"
        )
        for site in self.config.get("sites", []):
            try:
                LogRotator(
                    site["name"],
                    site.get("log_dir"),
                    retention_days=self.config["log_retention_days"],
                ).cleanup_old_logs()
            except OSError:
                pass
        self.log_activity(
            f"Settings saved. Interval: {self.interval_var.get()} min · Logs: {lr_note}"
        )
    
    def save_subscriptions(self):
        """Save subscription settings"""
        subscriptions = []
        if self.sub_alerts_var.get():
            subscriptions.append("alerts")
        
        self.config["subscriptions"] = subscriptions
        self.save_config()
        
        if subscriptions:
            self.log_activity(f"✓ Subscriptions saved: {', '.join(subscriptions)}")
            self.log_activity("⚠️ Restart sites for subscription changes to take effect")
        else:
            self.log_activity("Subscriptions saved: None (no alert notifications)")
            self.log_activity("⚠️ Restart sites for subscription changes to take effect")
    
    def log_activity(self, message: str):
        """Log activity to GUI"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        
        lines = self.log_text.get("1.0", tk.END).split("\n")
        if len(lines) > 500:
            self.log_text.delete("1.0", f"{len(lines) - 500}.0")
    
    def open_logs_folder(self):
        """Open logs folder in file explorer"""
        import subprocess
        import os
        
        # Check if a site is selected
        selection = self.sites_tree.selection()
        if selection:
            index = self.sites_tree.index(selection[0])
            site = self.config["sites"][index]
            
            # Use custom log dir if specified
            if site.get("log_dir"):
                logs_path = Path(site["log_dir"]) / site["name"]
            else:
                logs_path = Path(LOG_BASE_DIR) / site["name"]
        else:
            # Open default logs directory
            logs_path = Path(LOG_BASE_DIR).absolute()
        
        logs_path.mkdir(parents=True, exist_ok=True)
        logs_path = logs_path.absolute()
        
        if os.name == 'nt':
            subprocess.Popen(f'explorer "{logs_path}"')
        else:
            subprocess.Popen(['xdg-open', str(logs_path)])
    
    def populate_sites(self):
        """Populate sites list"""
        for item in self.sites_tree.get_children():
            self.sites_tree.delete(item)
        
        for site in self.config.get("sites", []):
            status = "Stopped"
            if site["name"] in self.connections:
                status = self.connections[site["name"]].status
            
            debug_indicator = "🐛" if site.get("debug_mode", False) else ""
            command_count = len(site.get("commands", []))
            
            self.sites_tree.insert("", tk.END, values=(
                site["name"],
                site["url"],
                status,
                debug_indicator,
                f"{command_count} cmd(s)"
            ))
    
    def update_site_status(self, site_name: str, status: str):
        """Update site status in GUI"""
        for item in self.sites_tree.get_children():
            values = self.sites_tree.item(item)["values"]
            if values[0] == site_name:
                self.sites_tree.item(item, values=(values[0], values[1], status, values[3], values[4]))
                break
    
    def add_site(self):
        """Add new site"""
        dialog = SiteDialog(self.root, "Add Site")
        self.root.wait_window(dialog)
        
        if dialog.result:
            names = [s["name"] for s in self.config["sites"]]
            if dialog.result["name"] in names:
                messagebox.showerror("Error", "A site with this name already exists")
                return
            
            self.config["sites"].append(dialog.result)
            self.populate_sites()
            self.save_config()
            self.log_activity(f"Added site: {dialog.result['name']}")
    
    def edit_site(self):
        """Edit selected site"""
        selection = self.sites_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a site to edit")
            return
        
        index = self.sites_tree.index(selection[0])
        site = self.config["sites"][index]
        
        if site["name"] in self.connections and self.connections[site["name"]].running:
            messagebox.showwarning("Site Running", "Please stop the site before editing")
            return
        
        dialog = SiteDialog(self.root, "Edit Site", site)
        self.root.wait_window(dialog)
        
        if dialog.result:
            self.config["sites"][index] = dialog.result
            self.populate_sites()
            self.save_config()
            self.log_activity(f"Updated site: {dialog.result['name']}")
    
    def remove_site(self):
        """Remove selected site"""
        selection = self.sites_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a site to remove")
            return
        
        index = self.sites_tree.index(selection[0])
        site = self.config["sites"][index]
        
        if site["name"] in self.connections and self.connections[site["name"]].running:
            messagebox.showwarning("Site Running", "Please stop the site before removing")
            return
        
        if messagebox.askyesno("Confirm", f"Remove site '{site['name']}'?"):
            self.config["sites"].pop(index)
            self.populate_sites()
            self.save_config()
            self.log_activity(f"Removed site: {site['name']}")
    
    def start_site(self):
        """Start selected site"""
        selection = self.sites_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a site to start")
            return
        
        index = self.sites_tree.index(selection[0])
        site = self.config["sites"][index]
        
        if site["name"] in self.connections and self.connections[site["name"]].running:
            messagebox.showinfo("Info", "Site is already running")
            return
        
        self.log_activity(f"Starting site: {site['name']}")
        
        connection = SiteConnection(site, self)
        self.connections[site["name"]] = connection
        connection.restart_attempts = 0
        
        asyncio.run_coroutine_threadsafe(connection.run(), self.loop)
    
    def auto_start_sites(self):
        """Auto-start sites that have auto_start enabled"""
        auto_start_count = 0
        
        # Log subscription configuration
        subscriptions = self.config.get("subscriptions", [])
        if subscriptions:
            self.log_activity(f"📡 Alert subscriptions enabled: {', '.join(subscriptions)}")
        else:
            self.log_activity("ℹ️ Alert subscriptions: None (configure in Global Settings)")
        
        for site in self.config["sites"]:
            if site.get("auto_start", False):
                if site["name"] in self.connections and self.connections[site["name"]].running:
                    continue
                    
                self.log_activity(f"🔄 Auto-starting site: {site['name']}")
                connection = SiteConnection(site, self)
                self.connections[site["name"]] = connection
                asyncio.run_coroutine_threadsafe(connection.run(), self.loop)
                auto_start_count += 1
        
        if auto_start_count > 0:
            self.log_activity(f"✓ Auto-started {auto_start_count} site(s)")
        else:
            self.log_activity("No sites configured for auto-start")
    
    def stop_site(self):
        """Stop selected site"""
        selection = self.sites_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a site to stop")
            return
        
        index = self.sites_tree.index(selection[0])
        site = self.config["sites"][index]
        
        if site["name"] not in self.connections or not self.connections[site["name"]].running:
            messagebox.showinfo("Info", "Site is not running")
            return
        
        self.log_activity(f"Stopping site: {site['name']}")
        asyncio.run_coroutine_threadsafe(self.connections[site["name"]].stop(), self.loop)
    
    def start_all(self):
        """Start all sites"""
        self.log_activity("=== Starting all sites ===")
        for site in self.config["sites"]:
            if site["name"] not in self.connections or not self.connections[site["name"]].running:
                connection = SiteConnection(site, self)
                self.connections[site["name"]] = connection
                connection.restart_attempts = 0
                asyncio.run_coroutine_threadsafe(connection.run(), self.loop)
    
    def stop_all(self):
        """Stop all sites"""
        self.log_activity("=== Stopping all sites ===")
        for connection in self.connections.values():
            if connection.running:
                asyncio.run_coroutine_threadsafe(connection.stop(), self.loop)
    
    def get_interval(self) -> int:
        """Get command interval"""
        return self.config.get("interval_minutes", 5)
    
    def get_log_retention_days(self) -> int:
        """Days to keep log files (mtime); 0 = do not delete by age."""
        try:
            return max(0, min(MAX_LOG_RETENTION_DAYS, int(self.config.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS))))
        except (TypeError, ValueError):
            return DEFAULT_LOG_RETENTION_DAYS
    
    def get_subscriptions(self) -> list:
        """Get notification categories to subscribe to"""
        return self.config.get("subscriptions", [])
    
    def start_event_loop(self):
        """Start async event loop in background thread"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        
        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()
        
        while self.loop is None:
            time.sleep(0.1)
    
    def on_closing(self):
        """Handle window closing"""
        if any(conn.running for conn in self.connections.values()):
            if not messagebox.askyesno("Confirm", "Sites are still running. Stop all and exit?"):
                return
            self.stop_all()
            time.sleep(2)
        
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        
        self.root.destroy()
    
    def run(self):
        """Run the application"""
        self.root.mainloop()


class SiteDialog(tk.Toplevel):
    """Dialog for adding/editing sites"""
    
    def __init__(self, parent, title, site_data=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("760x820")
        self.result = None
        
        self.transient(parent)
        self.grab_set()
        
        self.site_data = site_data or {
            "name": "",
            "url": "",
            "token": "",
            "ignore_ssl": False,
            "auto_start": False,
            "debug_mode": False,
            "commands": DEFAULT_COMMANDS.copy(),
            "command_settings": {},
            "log_dir": None,
        }
        
        self.create_widgets()
        self.populate_fields()
        
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{x}+{y}")

    def _add_schedulable_command_row(
        self, parent, cmd: str, desc: str, current_commands: list
    ) -> None:
        """Extended/historical: checkbox + interval vs daily + local time."""
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, padx=(10, 0), pady=2)
        settings = self.site_data.get("command_settings") or {}
        raw = settings.get(cmd)
        s = raw if isinstance(raw, dict) else {}
        mode = "daily" if s.get("schedule") == "daily" else "interval"
        var = tk.BooleanVar(value=cmd in current_commands)
        self.command_vars[cmd] = var
        ttk.Checkbutton(row, text=f"{cmd} - {desc}", variable=var).pack(side=tk.LEFT)
        mode_var = tk.StringVar(value=mode)
        time_var = tk.StringVar(value=_parse_daily_time(s.get("daily_time")))
        self.schedule_mode_vars[cmd] = mode_var
        self.daily_time_vars[cmd] = time_var
        ttk.Label(row, text="Run:").pack(side=tk.LEFT, padx=(12, 2))
        combo = ttk.Combobox(
            row, textvariable=mode_var, values=("interval", "daily"), width=9, state="readonly"
        )
        combo.pack(side=tk.LEFT)
        ttk.Label(row, text="local HH:MM:").pack(side=tk.LEFT, padx=(8, 2))
        time_entry = ttk.Entry(row, textvariable=time_var, width=7)
        time_entry.pack(side=tk.LEFT)

        def sync_time_state(*_: object) -> None:
            time_entry.config(state=("normal" if mode_var.get() == "daily" else "disabled"))

        mode_var.trace_add("write", lambda *_: sync_time_state())
        sync_time_state()
    
    def create_widgets(self):
        """Create dialog widgets"""
        # Create a canvas with scrollbar for the dialog
        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        canvas = tk.Canvas(canvas_frame)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Basic info
        info_frame = ttk.LabelFrame(scrollable_frame, text="Site Information", padding="10")
        info_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(info_frame, text="Site Name:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.name_entry = ttk.Entry(info_frame, width=50)
        self.name_entry.grid(row=0, column=1, pady=5, padx=5)
        
        ttk.Label(info_frame, text="WebSocket URL:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.url_entry = ttk.Entry(info_frame, width=50)
        self.url_entry.grid(row=1, column=1, pady=5, padx=5)
        ttk.Label(info_frame, text="Example: wss://tradesense.example.com/api", 
                 font=('TkDefaultFont', 8, 'italic')).grid(row=2, column=1, sticky=tk.W)
        
        ttk.Label(info_frame, text="API Token:").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.token_entry = ttk.Entry(info_frame, width=50, show="*")
        self.token_entry.grid(row=3, column=1, pady=5, padx=5)
        
        self.show_token_var = tk.BooleanVar()
        ttk.Checkbutton(info_frame, text="Show token", 
                       variable=self.show_token_var,
                       command=self.toggle_token).grid(row=4, column=1, sticky=tk.W)
        
        # Connection options
        options_frame = ttk.LabelFrame(scrollable_frame, text="Connection Options", padding="10")
        options_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.auto_start_var = tk.BooleanVar()
        ttk.Checkbutton(options_frame, text="Auto-start when application launches", 
                       variable=self.auto_start_var).pack(anchor=tk.W, pady=2)
        
        ttk.Label(options_frame, text="↳ Will auto-reconnect up to 3 times if connection lost", 
                 font=('TkDefaultFont', 8, 'italic'),
                 foreground='blue').pack(anchor=tk.W, padx=(20, 0))
        
        self.ignore_ssl_var = tk.BooleanVar()
        ttk.Checkbutton(options_frame, text="Ignore SSL certificate errors (self-signed certs)", 
                       variable=self.ignore_ssl_var).pack(anchor=tk.W, pady=(10, 2))
        
        ttk.Label(options_frame, text="⚠️ Only use for development/testing environments", 
                 font=('TkDefaultFont', 8, 'italic'),
                 foreground='red').pack(anchor=tk.W)
        
        self.debug_var = tk.BooleanVar()
        ttk.Checkbutton(options_frame, text="Enable debug logging (verbose)", 
                       variable=self.debug_var).pack(anchor=tk.W, pady=(10, 2))
        
        ttk.Label(options_frame, text="↳ Adds request details, timing info, and all messages to logs", 
                 font=('TkDefaultFont', 8, 'italic'),
                 foreground='orange').pack(anchor=tk.W, padx=(20, 0))
        
        ttk.Label(options_frame, text="↳ Command responses are always logged (debug adds more detail)", 
                 font=('TkDefaultFont', 8, 'italic'),
                 foreground='gray').pack(anchor=tk.W, padx=(20, 0))
        
        # Log directory selection
        ttk.Separator(options_frame, orient='horizontal').pack(fill=tk.X, pady=10)
        
        ttk.Label(options_frame, text="Custom Log Directory (optional):", 
                 font=('TkDefaultFont', 9, 'bold')).pack(anchor=tk.W, pady=(5, 5))
        
        log_dir_frame = ttk.Frame(options_frame)
        log_dir_frame.pack(fill=tk.X, pady=2)
        
        self.log_dir_var = tk.StringVar(value=self.site_data.get("log_dir") or "")
        log_dir_entry = ttk.Entry(log_dir_frame, textvariable=self.log_dir_var, width=40)
        log_dir_entry.pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Button(log_dir_frame, text="Browse...", command=self.browse_log_dir).pack(side=tk.LEFT)
        ttk.Button(log_dir_frame, text="Clear", command=lambda: self.log_dir_var.set("")).pack(side=tk.LEFT, padx=(5, 0))
        
        ttk.Label(options_frame, text="↳ Leave empty to use default location (./logs)", 
                 font=('TkDefaultFont', 8, 'italic'),
                 foreground='gray').pack(anchor=tk.W, padx=(20, 0))
        
        # Command selection
        cmd_frame = ttk.LabelFrame(scrollable_frame, text="Monitoring Commands", padding="10")
        cmd_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        ttk.Label(cmd_frame, text="Select which commands to execute for this site:", 
                 font=('TkDefaultFont', 9)).pack(anchor=tk.W, pady=(0, 10))
        
        self.command_vars = {}
        current_commands = self.site_data.get("commands", [])
        
        # Core commands
        core_label = ttk.Label(cmd_frame, text="Core Monitoring:", font=('TkDefaultFont', 9, 'bold'))
        core_label.pack(anchor=tk.W, pady=(5, 2))
        
        for cmd, desc in AVAILABLE_COMMANDS["core"]:
            var = tk.BooleanVar(value=cmd in current_commands)
            self.command_vars[cmd] = var
            cb = ttk.Checkbutton(cmd_frame, text=f"{cmd} - {desc}", variable=var)
            cb.pack(anchor=tk.W, padx=(10, 0), pady=1)
        
        self.schedule_mode_vars: Dict[str, tk.StringVar] = {}
        self.daily_time_vars: Dict[str, tk.StringVar] = {}

        # Extended commands (optional: daily at fixed local time)
        ext_label = ttk.Label(cmd_frame, text="Extended Monitoring:", font=('TkDefaultFont', 9, 'bold'))
        ext_label.pack(anchor=tk.W, pady=(10, 2))
        ttk.Label(
            cmd_frame,
            text="↳ Each command: run on every global interval, or once per day at the local time you set.",
            font=('TkDefaultFont', 8, 'italic'),
        ).pack(anchor=tk.W, padx=(10, 0))

        for cmd, desc in AVAILABLE_COMMANDS["extended"]:
            self._add_schedulable_command_row(cmd_frame, cmd, desc, current_commands)

        # Historical commands
        hist_label = ttk.Label(cmd_frame, text="Historical Data (High Volume):", font=('TkDefaultFont', 9, 'bold'))
        hist_label.pack(anchor=tk.W, pady=(10, 2))

        for cmd, desc in AVAILABLE_COMMANDS["historical"]:
            self._add_schedulable_command_row(cmd_frame, cmd, desc, current_commands)
        
        ttk.Label(cmd_frame, text="⚠️ Historical commands may retrieve large datasets and increase log size significantly", 
                 font=('TkDefaultFont', 8, 'italic'), foreground='red').pack(anchor=tk.W, pady=(5, 0))
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Buttons at bottom
        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        ttk.Button(button_frame, text="Save", command=self.save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=5)
    
    def populate_fields(self):
        """Populate fields with existing data"""
        self.name_entry.insert(0, self.site_data.get("name", ""))
        self.url_entry.insert(0, self.site_data.get("url", ""))
        self.token_entry.insert(0, self.site_data.get("token", ""))
        self.ignore_ssl_var.set(self.site_data.get("ignore_ssl", False))
        self.auto_start_var.set(self.site_data.get("auto_start", False))
        self.debug_var.set(self.site_data.get("debug_mode", False))
    
    def toggle_token(self):
        """Toggle token visibility"""
        if self.show_token_var.get():
            self.token_entry.config(show="")
        else:
            self.token_entry.config(show="*")
    
    def browse_log_dir(self):
        """Browse for log directory"""
        initial_dir = self.log_dir_var.get() or str(Path.cwd())
        directory = filedialog.askdirectory(
            title="Select Log Directory",
            initialdir=initial_dir
        )
        if directory:
            self.log_dir_var.set(directory)
    
    def save(self):
        """Save site data"""
        name = self.name_entry.get().strip()
        url = self.url_entry.get().strip()
        token = self.token_entry.get().strip()
        
        if not name or not url or not token:
            messagebox.showerror("Error", "Site name, URL, and token are required")
            return
        
        # Get selected commands
        selected_commands = []
        for cmd, var in self.command_vars.items():
            if var.get():
                selected_commands.append(cmd)
        
        if not selected_commands:
            messagebox.showerror("Error", "At least one monitoring command must be selected")
            return

        command_settings: Dict[str, Dict] = {}
        for cmd in selected_commands:
            if cmd in self.schedule_mode_vars and cmd in self.daily_time_vars:
                if self.schedule_mode_vars[cmd].get() == "daily":
                    command_settings[cmd] = {
                        "schedule": "daily",
                        "daily_time": _parse_daily_time(self.daily_time_vars[cmd].get()),
                    }
                else:
                    command_settings[cmd] = {"schedule": "interval"}
            else:
                command_settings[cmd] = {"schedule": "interval"}
        
        log_dir = self.log_dir_var.get().strip() or None
        
        self.result = {
            "name": name,
            "url": url,
            "token": token,
            "ignore_ssl": self.ignore_ssl_var.get(),
            "auto_start": self.auto_start_var.get(),
            "debug_mode": self.debug_var.get(),
            "commands": selected_commands,
            "command_settings": command_settings,
            "log_dir": log_dir
        }
        
        self.destroy()


def main():
    """Main entry point"""
    app = WBALoggerApp()
    app.run()


if __name__ == "__main__":
    main()
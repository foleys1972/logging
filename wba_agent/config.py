"""Configuration models and loader for the WBA collector agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import json


# Shared command catalogue reused from the legacy logger.
AVAILABLE_COMMANDS: Dict[str, List[str]] = {
    "core": [
        "get_zones",
        "get_tpos",
        "get_version",
        "get_health_api_report",
    ],
    "extended": [
        "get_turrets",
        "get_users",
        "get_lines",
        "get_shared_profiles",
    ],
    "historical": [
        "get_events:calls",
        "get_events:presences",
        "get_calls",
    ],
}


DEFAULT_COMMANDS: List[str] = [
    "get_zones",
    "get_tpos",
    "get_version",
    "get_health_api_report",
]


@dataclass
class SiteConfig:
    """Configuration for a single monitored WBA site."""

    name: str
    url: str
    token: str
    commands: List[str] = field(default_factory=lambda: DEFAULT_COMMANDS.copy())
    ignore_ssl: bool = False
    auto_start: bool = False
    debug_mode: bool = False
    get_lines_info: bool = True
    interval_minutes: Optional[int] = None
    log_dir: Optional[str] = None
    control_plane_site_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SiteConfig":
        commands = data.get("commands") or DEFAULT_COMMANDS.copy()
        return cls(
            name=data["name"],
            url=data["url"],
            token=data["token"],
            commands=list(commands),
            ignore_ssl=data.get("ignore_ssl", False),
            auto_start=data.get("auto_start", False),
            debug_mode=data.get("debug_mode", False),
            get_lines_info=bool(data.get("get_lines_info", True)),
            interval_minutes=data.get("interval_minutes"),
            log_dir=data.get("log_dir"),
            control_plane_site_id=data.get("control_plane_site_id"),
        )


@dataclass
class ControlPlaneConfig:
    base_url: str
    api_token: Optional[str] = None


@dataclass
class AgentConfig:
    """Top-level configuration for the collector agent."""

    interval_minutes: int = 5
    log_retention_days: int = 365
    subscriptions: List[str] = field(default_factory=lambda: ["alerts"])
    sites: List[SiteConfig] = field(default_factory=list)
    control_plane: Optional[ControlPlaneConfig] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentConfig":
        interval = data.get("interval_minutes", 5)
        lr = data.get("log_retention_days", 365)
        try:
            log_retention_days = max(0, min(3650, int(lr)))
        except (TypeError, ValueError):
            log_retention_days = 365
        subscriptions = data.get("subscriptions") or ["alerts"]
        sites = [SiteConfig.from_dict(site) for site in data.get("sites", [])]
        control_plane_data = data.get("control_plane")
        control_plane = None
        if control_plane_data:
            control_plane = ControlPlaneConfig(
                base_url=control_plane_data["base_url"],
                api_token=control_plane_data.get("api_token"),
            )
        return cls(
            interval_minutes=int(interval),
            log_retention_days=log_retention_days,
            subscriptions=list(subscriptions),
            sites=sites,
            control_plane=control_plane,
        )


def _load_text_config(path: Path) -> Dict[str, Any]:
    """Load configuration file supporting JSON (and YAML if available)."""

    text = path.read_text(encoding="utf-8")

    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "YAML configuration requested but PyYAML is not installed."
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")

    if not isinstance(data, dict):  # pragma: no cover - defensive programming
        raise ValueError("Configuration file must contain a JSON/YAML object")

    return data


def load_config(path: Path) -> AgentConfig:
    """Load agent configuration from the given path."""

    data = _load_text_config(path)
    return AgentConfig.from_dict(data)


def save_config(config: AgentConfig, path: Path) -> None:
    """Persist agent configuration as JSON."""

    data: Dict[str, Any] = {
        "interval_minutes": config.interval_minutes,
        "log_retention_days": config.log_retention_days,
        "subscriptions": config.subscriptions,
        "sites": [
            {
                "name": site.name,
                "url": site.url,
                "token": site.token,
                "commands": site.commands,
                "ignore_ssl": site.ignore_ssl,
                "auto_start": site.auto_start,
                "debug_mode": site.debug_mode,
                "interval_minutes": site.interval_minutes,
                "log_dir": site.log_dir,
                "control_plane_site_id": site.control_plane_site_id,
            }
            for site in config.sites
        ],
    }

    if config.control_plane:
        data["control_plane"] = {
            "base_url": config.control_plane.base_url,
            "api_token": config.control_plane.api_token,
        }

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")



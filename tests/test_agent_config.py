import pytest

from wba_agent.config import AgentConfig, SiteConfig


def test_site_config_defaults():
    data = {
        "name": "SiteA",
        "url": "wss://example.com",
        "token": "secret",
    }
    site = SiteConfig.from_dict(data)
    assert site.commands  # default commands applied
    assert site.interval_minutes is None
    assert site.control_plane_site_id is None


def test_agent_config_defaults():
    config = AgentConfig.from_dict({"sites": []})
    assert config.interval_minutes == 5
    assert config.subscriptions == ["alerts"]
    assert config.control_plane is None


def test_agent_config_serialization(tmp_path):
    site = SiteConfig(name="A", url="wss://", token="t")
    config = AgentConfig(
        sites=[site],
        control_plane=None,
    )
    path = tmp_path / "config.json"

    from wba_agent.config import save_config, load_config

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.interval_minutes == config.interval_minutes
    assert loaded.sites[0].name == "A"
    assert loaded.control_plane is None


def test_control_plane_config_round_trip(tmp_path):
    site = SiteConfig(name="A", url="wss://", token="t", control_plane_site_id=42)
    from wba_agent.config import ControlPlaneConfig, save_config, load_config

    cp_config = ControlPlaneConfig(base_url="https://control", api_token="token")
    config = AgentConfig(sites=[site], control_plane=cp_config)
    path = tmp_path / "cfg.json"

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.control_plane is not None
    assert loaded.control_plane.base_url == "https://control"
    assert loaded.sites[0].control_plane_site_id == 42



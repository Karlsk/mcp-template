"""Tests for Settings: YAML (structure) + env/.env (secrets) merge."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import Neo4jSettings, Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8000
    assert settings.mcp_log_level == "INFO"


def test_env_overrides_mcp_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9001")
    monkeypatch.setenv("MCP_LOG_LEVEL", "DEBUG")
    settings = Settings()
    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 9001
    assert settings.mcp_log_level == "DEBUG"


def test_secret_loaded_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDN_CONTROLLER_TOKEN", "super-secret")
    settings = Settings()
    assert settings.sdn_controller_token is not None
    assert settings.sdn_controller_token.get_secret_value() == "super-secret"


def test_secret_is_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDN_CONTROLLER_TOKEN", "super-secret")
    settings = Settings()
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings.sdn_controller_token)


def test_yaml_loads_sdn_structure() -> None:
    """The committed config/sdn_controller.yaml populates the nested sdn block."""
    settings = Settings()
    assert settings.sdn.timeout == 30.0
    assert settings.sdn.auth_type == "basic"
    assert settings.sdn.ssl_verify is False
    assert settings.sdn.endpoints.get("login") == "/oauth/token"
    assert (
        settings.sdn.endpoints.get("devices_page")
        == "/api/no/config/terra-pe:peInfos/page"
    )
    assert (
        settings.sdn.endpoints.get("topology")
        == "/api/sr/config/network-topology:network-topology"
    )
    assert settings.sdn.endpoints.get("alerts") == "/monitor/v2/alert/page"
    assert settings.sdn.token_field == "access_token"
    assert settings.sdn.retry.max_retries == 3


def test_sdn_is_configured_false_by_default() -> None:
    """Skeleton mode: empty base_url means not configured."""
    settings = Settings()
    assert settings.sdn.base_url == ""
    assert settings.sdn_is_configured() is False


def test_sdn_is_configured_true_when_base_url_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "cfg.yaml"
    config.write_text("sdn:\n  base_url: 'https://controller.example'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    settings = Settings()
    assert settings.sdn_is_configured() is True


def test_base_url_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDN_CONTROLLER_BASE_URL (env) wins over the YAML base_url."""
    monkeypatch.setenv("SDN_CONTROLLER_BASE_URL", "https://from-env.example")
    settings = Settings()
    assert settings.sdn_base_url == "https://from-env.example"
    assert settings.sdn_is_configured() is True


def test_base_url_falls_back_to_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the env override, base_url comes from the YAML."""
    config = tmp_path / "cfg.yaml"
    config.write_text("sdn:\n  base_url: 'https://from-yaml.example'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    settings = Settings()
    assert settings.sdn_base_url == "https://from-yaml.example"


def test_dns_rebinding_protection_disabled_by_default() -> None:
    """Protection must default OFF so GUI clients (Cherry Studio/Cursor) connect."""
    assert Settings().mcp_dns_rebinding_protection is False


def test_yaml_rejects_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret key in YAML must be refused at load time."""
    config = tmp_path / "cfg.yaml"
    config.write_text("sdn_controller_token: 'should-not-be-here'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    with pytest.raises(ValueError, match="Refusing to load secret"):
        Settings()


def test_custom_config_file_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "cfg.yaml"
    config.write_text(
        "sdn:\n  base_url: 'https://x'\n  timeout: 5.0\n  endpoints:\n    devices: '/api/devices'\n"
    )
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    settings = Settings()
    assert settings.sdn.timeout == 5.0
    assert settings.sdn.endpoints["devices"] == "/api/devices"


# ---------------------------------------------------------------------------
# Neo4j SOP graph configuration (spec-02)
# ---------------------------------------------------------------------------


def test_yaml_rejects_neo4j_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The secret guard must also cover graph credentials."""
    config = tmp_path / "cfg.yaml"
    config.write_text("neo4j_password: 'should-not-be-here'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    with pytest.raises(ValueError, match="Refusing to load secret"):
        Settings()


def test_neo4j_uri_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NEO4J_URI (env) wins over the YAML neo4j.uri."""
    config = tmp_path / "cfg.yaml"
    config.write_text("neo4j:\n  uri: 'bolt://from-yaml:7687'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    monkeypatch.setenv("NEO4J_URI", "bolt://from-env:7687")
    settings = Settings()
    assert settings.neo4j_base_uri == "bolt://from-env:7687"


def test_neo4j_is_configured_two_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings().neo4j_is_configured() is False
    config = tmp_path / "cfg.yaml"
    config.write_text("neo4j:\n  uri: 'bolt://graph.example:7687'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    assert Settings().neo4j_is_configured() is True


def test_neo4j_settings_has_no_db_tag() -> None:
    """The logical database is data, never configuration (spec-02 decision 3)."""
    assert "db_tag" not in Neo4jSettings.model_fields


def test_neo4j_block_forbids_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """username/password under neo4j: must fail loudly (extra='forbid')."""
    config = tmp_path / "cfg.yaml"
    config.write_text("neo4j:\n  uri: 'bolt://x:7687'\n  password: 'oops'\n")
    monkeypatch.setenv("SDN_CONFIG_FILE", str(config))
    with pytest.raises(ValidationError):
        Settings()

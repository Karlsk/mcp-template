"""Tests for Settings: YAML (structure) + env/.env (secrets) merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import Settings


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
    monkeypatch.setenv("SDN_TOKEN", "super-secret")
    settings = Settings()
    assert settings.sdn_token is not None
    assert settings.sdn_token.get_secret_value() == "super-secret"


def test_secret_is_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDN_TOKEN", "super-secret")
    settings = Settings()
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings.sdn_token)


def test_yaml_loads_sdn_structure() -> None:
    """The committed config/sdn_controller.yaml populates the nested sdn block."""
    settings = Settings()
    assert settings.sdn.timeout == 30.0
    assert settings.sdn.auth_type == "no-auth"
    assert settings.sdn.endpoints.get("devices") == "/devices"
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


def test_dns_rebinding_protection_disabled_by_default() -> None:
    """Protection must default OFF so GUI clients (Cherry Studio/Cursor) connect."""
    assert Settings().mcp_dns_rebinding_protection is False


def test_yaml_rejects_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret key in YAML must be refused at load time."""
    config = tmp_path / "cfg.yaml"
    config.write_text("sdn_token: 'should-not-be-here'\n")
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

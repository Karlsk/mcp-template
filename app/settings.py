"""Application settings: merges non-secret YAML structure with env/.env secrets.

The split is intentional:
- ``config/sdn_controller.yaml`` holds NON-SECRET structure (base URL, endpoints,
  timeouts, auth *type*). It is safe to commit and version.
- Environment variables / ``.env`` hold SECRETS (token, username, password) and
  runtime knobs (host/port/log level). Never commit a real ``.env``.

Secrets are *refused* in the YAML file (see :func:`_reject_secrets_in_yaml`) so a
developer cannot accidentally commit a token there. Override the config file path
with the ``SDN_CONFIG_FILE`` environment variable.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# Resolve relative to this module so the default works regardless of process CWD
# (IDE, systemd unit, etc.). Docker/production overrides via SDN_CONFIG_FILE.
DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "sdn_controller.yaml"

# Secret fields that must NEVER be loaded from YAML (env / .env only).
_FORBIDDEN_YAML_KEYS = frozenset(
    {"sdn_controller_token", "sdn_controller_username", "sdn_controller_password"}
)


class RetrySettings(BaseModel):
    """HTTP retry policy (exponential backoff)."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0


class SdnSettings(BaseModel):
    """Non-secret SDN controller configuration (loaded from YAML)."""

    base_url: str = ""
    auth_type: Literal["no-auth", "bearer", "basic"] = "no-auth"
    timeout: float = 30.0
    retry: RetrySettings = Field(default_factory=RetrySettings)
    endpoints: dict[str, str] = Field(default_factory=dict)
    # TLS verification: set false for controllers with self-signed certificates.
    ssl_verify: bool = True
    # JSON key holding the bearer token in the login response (auth_type=basic).
    token_field: str = "access_token"


class Settings(BaseSettings):
    """Runtime configuration for the MCP server and SDN integration."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="", extra="ignore", case_sensitive=False
    )

    # ---- MCP server ----
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # DNS-rebinding protection validates the Host/Origin header and rejects unknown
    # origins with 403. FastMCP enables it by default, which BLOCKS Electron-based
    # GUI clients (Cherry Studio, Cursor) that send an Origin header. Disabled here
    # so local GUI clients connect out-of-the-box; set MCP_DNS_REBINDING_PROTECTION
    # =true when serving behind a reverse proxy / on a public interface.
    mcp_dns_rebinding_protection: bool = False

    # ---- SDN controller connection (env / .env) ----
    # base_url is env-driven so one image runs against lab/prod controllers.
    sdn_controller_base_url: str | None = None
    # Secrets (env / .env only — never in YAML).
    sdn_controller_username: str | None = None
    sdn_controller_password: SecretStr | None = None
    sdn_controller_token: SecretStr | None = None  # bearer mode (fixed api-key)

    # ---- SDN non-secret structure (YAML) ----
    sdn: SdnSettings = Field(default_factory=SdnSettings)

    @property
    def sdn_base_url(self) -> str:
        """Effective controller base URL — env override wins over the YAML value."""
        return self.sdn_controller_base_url or self.sdn.base_url

    def sdn_is_configured(self) -> bool:
        """True when an SDN controller base URL is present."""
        return bool(self.sdn_base_url)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_path = Path(os.getenv("SDN_CONFIG_FILE", str(DEFAULT_CONFIG_FILE)))
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            env_settings,
            dotenv_settings,
        ]
        # YAML source is optional so the server still boots without the file.
        if config_path.exists():
            _reject_secrets_in_yaml(config_path)  # raises if a secret key is present
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_path))
        sources.append(file_secret_settings)
        return tuple(sources)


def _reject_secrets_in_yaml(path: Path) -> None:
    """Refuse to load secret keys from YAML — they must come from env/.env only."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    found = _FORBIDDEN_YAML_KEYS & _collect_keys(raw)
    if found:
        raise ValueError(
            f"Refusing to load secret key(s) {sorted(found)} from YAML ({path}). "
            "Provide SDN_CONTROLLER_TOKEN / SDN_CONTROLLER_USERNAME / "
            "SDN_CONTROLLER_PASSWORD via environment or .env instead."
        )


def _collect_keys(obj: object) -> set[str]:
    """Recursively gather every mapping key in a parsed YAML document."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(str(key))
            keys |= _collect_keys(value)
    elif isinstance(obj, Iterable) and not isinstance(obj, str | bytes):
        for item in obj:
            keys |= _collect_keys(item)
    return keys

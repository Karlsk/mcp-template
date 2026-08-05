"""Command template library: a local YAML lookup table (action x vendor).

The library is human-curated and version-controlled: the server never invents a
command, it only serves what is written in ``config/command_templates.yaml``.
Loaded lazily on first use and cached for the process lifetime — the file is a
static deployment artifact, so there is no reload path (restart to pick up edits).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Final

import yaml

from app.templates.exceptions import TemplateError
from app.templates.models import ActionSummary, CommandTemplate

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "command_templates.yaml"
)

DEFAULT_VENDOR: Final = "default"
MAX_TEMPLATE_RESULTS: Final = 50

# Vendor spellings normalized onto the canonical keys used in the YAML.
_VENDOR_ALIASES: Final[dict[str, str]] = {
    "hw": "huawei",
    "huawei": "huawei",
    "vrp": "huawei",
    "h3c": "h3c",
    "hpe": "h3c",
    "comware": "h3c",
    "cisco": "cisco",
    "ios": "cisco",
    "iosxr": "cisco",
    "ios-xr": "cisco",
    "nxos": "cisco",
    "zte": "zte",
    "ruijie": "ruijie",
}

_PLACEHOLDER_RE: Final = re.compile(r"\{(\w+)\}")


def normalize_vendor(vendor: str) -> str:
    """Map a vendor spelling onto its canonical key (unknown values pass through)."""
    key = vendor.strip().lower().replace(" ", "")
    return _VENDOR_ALIASES.get(key, key)


def _placeholders(command: str) -> list[str]:
    """Extract `{name}` placeholder names, preserving first-seen order."""
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(command):
        seen.setdefault(match.group(1), None)
    return list(seen)


class CommandTemplateRegistry:
    """Indexed, validated view of the command template YAML."""

    def __init__(self, templates: dict[str, dict[str, Any]]) -> None:
        self._templates = templates

    @classmethod
    def load(cls, path: Path | None = None) -> CommandTemplateRegistry:
        """Parse and validate the YAML. Raises TemplateError on any problem."""
        resolved = path or Path(os.getenv("COMMAND_TEMPLATE_FILE", str(DEFAULT_TEMPLATE_FILE)))
        if not resolved.is_file():
            # Path goes to the server log only, never into the model-facing message.
            logger.warning(
                "command template library file is missing", extra={"path": str(resolved)}
            )
            raise TemplateError("command template library file is missing")
        try:
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise TemplateError(f"command template library is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("templates"), dict):
            raise TemplateError("command template library must be a mapping with a 'templates' key")
        return cls(cls._validate(raw["templates"]))

    @staticmethod
    def _validate(templates: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Flatten and validate every entry; any problem is a TemplateError."""
        index: dict[str, dict[str, Any]] = {}
        for action, entry in templates.items():
            vendors_raw = entry.get("vendors") if isinstance(entry, dict) else None
            if not isinstance(vendors_raw, dict) or not vendors_raw:
                raise TemplateError(f"action '{action}' has no vendors")
            name = entry.get("name") if isinstance(entry.get("name"), str) else ""
            observation = (
                entry.get("observation")
                if isinstance(entry.get("observation"), str)
                else ""
            )
            vendors: dict[str, CommandTemplate] = {}
            for vendor, leaf in vendors_raw.items():
                command = leaf.get("command") if isinstance(leaf, dict) else None
                if not isinstance(command, str) or not command.strip():
                    raise TemplateError(
                        f"action '{action}' vendor '{vendor}' has an empty command"
                    )
                canonical = normalize_vendor(vendor)
                if canonical in vendors:
                    raise TemplateError(
                        f"action '{action}' has conflicting vendor entries for '{canonical}'"
                    )
                fields: dict[str, Any] = {
                    "action": action,
                    "vendor": canonical,
                    "command": command,
                    "name": name,
                    "observation": observation,
                    "placeholders": _placeholders(command),
                }
                for key, value in leaf.items():
                    if key != "command" and key not in fields:
                        fields[key] = value
                vendors[canonical] = CommandTemplate(**fields)
            index[action] = {"name": name, "observation": observation, "vendors": vendors}
        return index

    def get(self, action: str, vendor: str) -> CommandTemplate | None:
        """Exact (action, vendor) lookup, falling back to the `default` vendor."""
        entry = self._templates.get(action)
        if entry is None:
            return None
        key = normalize_vendor(vendor)
        vendors: dict[str, CommandTemplate] = entry["vendors"]
        if key in vendors:
            return vendors[key].model_copy(deep=True)
        if DEFAULT_VENDOR in vendors:
            # Echo the requested vendor so the agent sees "no X entry, here is
            # the generic one"; the fallback flag marks the downgrade.
            return vendors[DEFAULT_VENDOR].model_copy(
                deep=True, update={"vendor": key, "fallback": DEFAULT_VENDOR}
            )
        return None

    def get_all_vendors(self, action: str) -> list[CommandTemplate]:
        """Every vendor variant of one action ([] when the action is unknown)."""
        entry = self._templates.get(action)
        if entry is None:
            return []
        return [template.model_copy(deep=True) for template in entry["vendors"].values()]

    def search(
        self,
        keyword: str | None = None,
        vendor: str | None = None,
        limit: int = MAX_TEMPLATE_RESULTS,
    ) -> list[ActionSummary]:
        """List actions matching a keyword and/or covered by a vendor."""
        needle = keyword.strip().lower() if keyword and keyword.strip() else None
        vendor_key = normalize_vendor(vendor) if vendor and vendor.strip() else None
        results: list[ActionSummary] = []
        for action, entry in self._templates.items():
            if vendor_key is not None and vendor_key not in entry["vendors"]:
                continue
            if needle is not None:
                haystack = f"{action} {entry['name']}".lower()
                if needle not in haystack:
                    continue
            results.append(
                ActionSummary(
                    action=action,
                    name=entry["name"],
                    observation=entry["observation"],
                    vendors=list(entry["vendors"].keys()),
                )
            )
            if len(results) >= limit:
                break
        return results

    @property
    def actions(self) -> list[str]:
        return list(self._templates.keys())


_registry: CommandTemplateRegistry | None = None


def get_registry() -> CommandTemplateRegistry:
    """Return the process-wide registry, loading it on first use."""
    global _registry
    if _registry is None:
        _registry = CommandTemplateRegistry.load()
    return _registry


def reset_registry() -> None:
    """Drop the cached registry (tests only)."""
    global _registry
    _registry = None

"""Registry-layer tests for the command template library (spec-04 §7 items 1-9).

Pure unit tests: every case loads a tmp YAML, no MCP session needed.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.templates import (
    ActionSummary,
    CommandTemplate,
    CommandTemplateRegistry,
    TemplateError,
    get_registry,
    normalize_vendor,
    reset_registry,
)


def write_yaml(tmp_path: Path, templates: dict[str, Any]) -> Path:
    path = tmp_path / "templates.yaml"
    payload = yaml.safe_dump({"templates": templates}, sort_keys=False, allow_unicode=True)
    path.write_text(payload, encoding="utf-8")
    return path


def seed_templates() -> dict[str, Any]:
    """2 actions x 3 vendors (spec §7.1 shape)."""
    return {
        "verify_interface_state": {
            "name": "检查接口状态",
            "observation": "oper_state",
            "vendors": {
                "huawei": {"command": "display interface {interface} brief"},
                "h3c": {"command": "display interface {interface} brief"},
                "default": {"command": "show interface {interface}"},
            },
        },
        "check_optical_power": {
            "name": "检查光模块收发光功率",
            "observation": "rx_power",
            "vendors": {
                "huawei": {
                    "command": "display transceiver interface {interface} verbose",
                    "notes": "仅 V8 支持",
                },
                "default": {"command": "show transceiver {interface}"},
            },
        },
    }


@pytest.fixture(autouse=True)
def _isolate_process_cache() -> Iterator[None]:
    """Drop the module-level singleton after every test (spec §7.9)."""
    yield
    reset_registry()


def load_registry(tmp_path: Path) -> CommandTemplateRegistry:
    return CommandTemplateRegistry.load(write_yaml(tmp_path, seed_templates()))


# ---------------------------------------------------------------------------
# §7.1 loading + indexing
# ---------------------------------------------------------------------------


def test_load_indexes_actions_stably_and_get_hits(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    assert registry.actions == ["verify_interface_state", "check_optical_power"]
    template = registry.get("verify_interface_state", "huawei")
    assert template is not None
    assert template.command == "display interface {interface} brief"
    assert template.action == "verify_interface_state"
    assert template.vendor == "huawei"
    assert template.name == "检查接口状态"
    assert template.observation == "oper_state"
    assert isinstance(template, CommandTemplate)


# ---------------------------------------------------------------------------
# §7.2 vendor normalization
# ---------------------------------------------------------------------------


def test_vendor_spellings_normalize_onto_canonical_keys(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    for spelling in ("HW", "vrp", " Huawei "):
        template = registry.get("verify_interface_state", spelling)
        assert template is not None, spelling
        assert template.vendor == "huawei"
        assert template.fallback is None
    assert normalize_vendor("IOS-XR") == "cisco"
    assert normalize_vendor("comware") == "h3c"
    assert normalize_vendor("unknown-co") == "unknown-co"


# ---------------------------------------------------------------------------
# §7.3-4 default fallback semantics
# ---------------------------------------------------------------------------


def test_missing_vendor_falls_back_to_default(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    template = registry.get("verify_interface_state", "zte")
    assert template is not None
    assert template.fallback == "default"
    # The echoed vendor stays the requested (normalized) one.
    assert template.vendor == "zte"
    assert template.command == "show interface {interface}"


def test_missing_vendor_without_default_returns_none(tmp_path: Path) -> None:
    data = seed_templates()
    del data["verify_interface_state"]["vendors"]["default"]
    registry = CommandTemplateRegistry.load(write_yaml(tmp_path, data))
    assert registry.get("verify_interface_state", "zte") is None


# ---------------------------------------------------------------------------
# §7.5-6 placeholders and extra keys
# ---------------------------------------------------------------------------


def test_placeholders_preserve_first_seen_order(tmp_path: Path) -> None:
    data = seed_templates()
    data["verify_interface_state"]["vendors"]["huawei"] = {
        "command": "probe {a} {b} {a}"
    }
    registry = CommandTemplateRegistry.load(write_yaml(tmp_path, data))
    template = registry.get("verify_interface_state", "huawei")
    assert template is not None
    assert template.placeholders == ["a", "b"]

    simple = registry.get("verify_interface_state", "h3c")
    assert simple is not None
    assert simple.placeholders == ["interface"]


def test_extra_vendor_keys_are_echoed_verbatim(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    template = registry.get("check_optical_power", "huawei")
    assert template is not None
    dumped = template.model_dump()
    assert dumped["notes"] == "仅 V8 支持"


# ---------------------------------------------------------------------------
# §7.7 load-time validation (five fail-fast cases, no absolute paths)
# ---------------------------------------------------------------------------


def _expect_template_error(path: Path, *, fragment: str) -> None:
    with pytest.raises(TemplateError) as excinfo:
        CommandTemplateRegistry.load(path)
    assert fragment in str(excinfo.value)
    assert str(path) not in str(excinfo.value), "absolute path leaked into message"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(TemplateError) as excinfo:
        CommandTemplateRegistry.load(tmp_path / "nope.yaml")
    assert "missing" in str(excinfo.value)
    assert str(tmp_path / "nope.yaml") not in str(excinfo.value)


def test_top_level_not_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(TemplateError):
        CommandTemplateRegistry.load(path)


def test_missing_templates_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("other: {}\n", encoding="utf-8")
    with pytest.raises(TemplateError):
        CommandTemplateRegistry.load(path)


def test_action_without_vendors_raises(tmp_path: Path) -> None:
    data = seed_templates()
    del data["verify_interface_state"]["vendors"]
    _expect_template_error(
        write_yaml(tmp_path, data), fragment="action 'verify_interface_state'"
    )


def test_empty_command_raises(tmp_path: Path) -> None:
    data = seed_templates()
    data["verify_interface_state"]["vendors"]["huawei"] = {"command": "   "}
    _expect_template_error(
        write_yaml(tmp_path, data),
        fragment="action 'verify_interface_state' vendor 'huawei'",
    )


def test_vendor_alias_conflict_raises(tmp_path: Path) -> None:
    data = seed_templates()
    data["verify_interface_state"]["vendors"]["hw"] = {"command": "display x"}
    _expect_template_error(
        write_yaml(tmp_path, data), fragment="huawei"
    )


# ---------------------------------------------------------------------------
# §7.8 search()
# ---------------------------------------------------------------------------


def test_search_keyword_matches_action_and_chinese_name(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)

    by_action = registry.search(keyword="optical")
    assert [s.action for s in by_action] == ["check_optical_power"]

    by_name = registry.search(keyword="光模块")
    assert [s.action for s in by_name] == ["check_optical_power"]
    assert isinstance(by_name[0], ActionSummary)


def test_search_vendor_filter_and_combination_and_limit(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)

    covered = registry.search(vendor="h3c")
    assert [s.action for s in covered] == ["verify_interface_state"]

    # keyword AND vendor combine as an intersection.
    both = registry.search(keyword="接口", vendor="h3c")
    assert [s.action for s in both] == ["verify_interface_state"]
    none = registry.search(keyword="光模块", vendor="h3c")
    assert none == []

    limited = registry.search(limit=1)
    assert len(limited) == 1

    summaries = registry.search()
    assert next(s.vendors for s in summaries) == ["huawei", "h3c", "default"]


def test_get_all_vendors_returns_every_variant(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    variants = registry.get_all_vendors("verify_interface_state")
    assert [t.vendor for t in variants] == ["huawei", "h3c", "default"]
    assert registry.get_all_vendors("nope") == []


# ---------------------------------------------------------------------------
# §7.9 COMMAND_TEMPLATE_FILE override + process cache
# ---------------------------------------------------------------------------


def test_env_override_redirects_get_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_yaml(tmp_path, seed_templates())
    monkeypatch.setenv("COMMAND_TEMPLATE_FILE", str(path))

    registry = get_registry()
    assert registry.actions == ["verify_interface_state", "check_optical_power"]
    assert get_registry() is registry  # cached for the process lifetime


def test_env_override_missing_file_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMMAND_TEMPLATE_FILE", str(tmp_path / "absent.yaml"))
    with pytest.raises(TemplateError):
        get_registry()


def test_malformed_yaml_raises_template_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(textwrap.dedent("""\
        templates:
          a: {vendors: {huawei: {command: "x"}
    """), encoding="utf-8")
    with pytest.raises(TemplateError):
        CommandTemplateRegistry.load(path)

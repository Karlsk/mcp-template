"""Cross-spec guardrail: seed template actions follow the SOP Step.action
vocabulary convention (spec-04 §7).

The SOP graph data lives in Neo4j and is unavailable to tests, so the check
degrades to a static one: every seed action name is snake-case lowercase and
every action offers a `default` vendor entry (the premise of the §4.3
fallback semantics).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# Resolved independently from app.templates so this guardrail only asserts
# the seed data itself.
SEED_FILE = Path(__file__).resolve().parent.parent / "config" / "command_templates.yaml"

ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def test_seed_actions_are_snake_case_and_default_covered() -> None:
    with SEED_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    templates = data["templates"]
    assert len(templates) == 9  # spec-04 §2: the first seed batch

    for action, entry in templates.items():
        assert ACTION_NAME_RE.match(action), f"action '{action}' not snake-case"
        vendors = entry["vendors"]
        assert "default" in vendors, f"action '{action}' lacks a default entry"
        for _vendor, leaf in vendors.items():
            assert isinstance(leaf["command"], str) and leaf["command"].strip()

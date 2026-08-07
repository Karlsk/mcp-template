"""Envelope pure functions that shape the search_sop JSON (spec-05 §5).

The rules mirror the reference implementation's ``determine_node_type`` /
``build_node_object``: ``type`` is derived (never stored), empty strings are
omitted, and unknown props never leak into the envelope top level.
"""

from __future__ import annotations

from app.graph.envelope import event_payload, node_to_json, node_type
from app.graph.models import SOPNode


def _node(**fields: object) -> SOPNode:
    return SOPNode.model_validate(fields)


# ---------------------------------------------------------------------------
# node_type: kind == "output" or a non-empty reason -> final_answer
# ---------------------------------------------------------------------------


def test_output_kind_is_final_answer() -> None:
    assert node_type(_node(id="O1", kind="output", reason="done")) == "final_answer"
    # Even without a reason the Output label decides (spec-05 §5 rule 1).
    assert node_type(_node(id="O1", kind="output")) == "final_answer"


def test_non_empty_reason_forces_final_answer_even_for_steps() -> None:
    assert node_type(_node(id="S1", kind="step", reason="x")) == "final_answer"


def test_step_and_event_default_to_function_call() -> None:
    assert node_type(_node(id="S1", kind="step", action="verify")) == "function_call"
    assert node_type(_node(id="E1", kind="event")) == "function_call"
    assert node_type(_node()) == "function_call"


# ---------------------------------------------------------------------------
# node_to_json: fixed keys + empty-string omission
# ---------------------------------------------------------------------------


def test_function_call_node_keeps_action_and_observation() -> None:
    node = _node(
        id="S1", kind="step", label="Step", name="Check interface",
        action="verify_interface_state", observation="oper_state",
    )
    assert node_to_json(node) == {
        "id": "S1",
        "name": "Check interface",
        "type": "function_call",
        "label": "Step",
        "action": "verify_interface_state",
        "observation": "oper_state",
    }


def test_empty_optional_fields_are_omitted() -> None:
    # Event root: usually a function_call without action (spec-05 §5 rule 3).
    payload = node_to_json(_node(id="E1", kind="event", label="Event", name="Link Down"))
    assert payload == {"id": "E1", "name": "Link Down", "type": "function_call",
                       "label": "Event"}
    assert "action" not in payload
    assert "observation" not in payload
    assert "reason" not in payload


def test_final_answer_node_puts_the_wording_in_reason() -> None:
    node = _node(id="O1", kind="output", label="Output", name="",
                 reason="The link is fine.")
    assert node_to_json(node) == {
        "id": "O1", "name": "", "type": "final_answer",
        "label": "Output", "reason": "The link is fine.",
    }


def test_unknown_extra_props_do_not_leak_into_the_envelope() -> None:
    """spec-05 §5: extra props survive on the model but stay out of the JSON."""
    node = _node(id="S1", kind="step", label="Step", vendor_hint="cisco")
    payload = node_to_json(node)
    assert "vendor_hint" not in payload
    assert set(payload) == {"id", "name", "type", "label"}


def test_label_is_echoed_verbatim() -> None:
    assert node_to_json(_node(id="S1", kind="step", label="Step"))["label"] == "Step"
    assert node_to_json(_node(id="O1", kind="output", label="Output"))["label"] == "Output"


# ---------------------------------------------------------------------------
# event_payload: the tree root becomes {event_name, event_id, fault_type, intent}
# ---------------------------------------------------------------------------


def test_event_payload_takes_props_with_empty_defaults() -> None:
    event = _node(
        id="E1", kind="event", name="Link Down",
        fault_type="link down", intent="",
    )
    assert event_payload(event) == {
        "event_name": "Link Down",
        "event_id": "E1",
        "fault_type": "link down",
        "intent": "",
    }


def test_event_payload_missing_props_default_to_empty_string() -> None:
    event = _node(id="E2", kind="event", name="OSPF Down")
    payload = event_payload(event)
    assert payload["fault_type"] == ""
    assert payload["intent"] == ""
    assert payload["event_name"] == "OSPF Down"

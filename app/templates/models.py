"""Pure data models for the command template library."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CommandTemplate(BaseModel):
    """One (action, vendor) command template."""

    model_config = ConfigDict(extra="allow")  # keep notes/parser/... as authored

    action: str
    vendor: str
    command: str
    name: str = ""
    observation: str = ""
    # Set when the requested vendor had no entry and the `default` one was used.
    fallback: str | None = None
    # Placeholder names extracted from `command`, e.g. ["interface"].
    placeholders: list[str] = Field(default_factory=list)


class ActionSummary(BaseModel):
    """One action with the vendors it covers (used by list-style responses)."""

    action: str
    name: str = ""
    observation: str = ""
    vendors: list[str] = Field(default_factory=list)

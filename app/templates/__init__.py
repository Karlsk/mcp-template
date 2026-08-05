"""Command template library integration layer (local YAML data source)."""

from app.templates.exceptions import TemplateError
from app.templates.models import ActionSummary, CommandTemplate
from app.templates.registry import (
    CommandTemplateRegistry,
    get_registry,
    normalize_vendor,
    reset_registry,
)

__all__ = [
    "ActionSummary",
    "CommandTemplate",
    "CommandTemplateRegistry",
    "TemplateError",
    "get_registry",
    "normalize_vendor",
    "reset_registry",
]

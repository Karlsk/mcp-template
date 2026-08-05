"""SOP graph labels, relationship types, and Cypher statement constants.

Aligning with the real graph (should label naming differ) only touches this
file — never the client logic. Statements themselves are added by spec-03.
"""

from __future__ import annotations

from typing import Final

LABEL_EVENT: Final = "Event"
LABEL_STEP: Final = "Step"
LABEL_OUTPUT: Final = "Output"
REL_NEXT: Final = "NEXT"
DB_PROPERTY: Final = "_db"

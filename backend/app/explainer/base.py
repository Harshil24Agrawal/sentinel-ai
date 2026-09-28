"""Explainer interface: turn an incident card into a plain-English explanation."""

from __future__ import annotations

from typing import Protocol

from app.models import Explanation, GroupCard


class Explainer(Protocol):
    """Explains an outage from the evidence on its card; never decides whether one exists.

    Statistics decide *when* something is an incident; an explainer only says *what* it
    looks like. Implementations must be safe to call every time a card is built.
    """

    def explain(self, card: GroupCard) -> Explanation: ...

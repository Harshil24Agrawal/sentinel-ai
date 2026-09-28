"""Pattern templating: mask dynamic tokens, stable pattern IDs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# >= 8 hex chars with at least one letter, so long all-digit IDs fall through to <NUM>
_HEX = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_SPACE = re.compile(r"\s+")

# Order matters: UUID and email contain digits/hex that later steps would otherwise chop up.
_STEPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_UUID, "<UUID>"),
    (_EMAIL, "<EMAIL>"),
    (_IPV4, "<IP>"),
    (_HEX, "<HEX>"),
    (_NUM, "<NUM>"),
)


@dataclass(frozen=True, slots=True)
class Pattern:
    id: str
    template: str


class PatternMiner(Protocol):
    """Interface a drain3-backed miner would implement as a drop-in replacement."""

    def mine(self, message: str) -> Pattern: ...


def template_of(message: str) -> str:
    """Mask dynamic tokens and collapse whitespace. ``[REDACTED]`` stays literal."""
    for regex, token in _STEPS:
        message = regex.sub(token, message)
    return _SPACE.sub(" ", message).strip()


def pattern_id(template: str) -> str:
    """Short stable hash of a template (8 hex chars)."""
    return hashlib.sha1(template.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


class RegexPatternMiner:
    def mine(self, message: str) -> Pattern:
        template = template_of(message)
        return Pattern(id=pattern_id(template), template=template)


_default_miner = RegexPatternMiner()


def mine(message: str) -> Pattern:
    return _default_miner.mine(message)

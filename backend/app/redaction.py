"""PHI redaction; runs before anything is stored, templated, displayed or sent."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.config import get_settings

REDACTED = "[REDACTED]"

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Separators are mandatory so bare 10-digit request IDs are never treated as phone numbers.
_PHONE = re.compile(
    r"(?<![\w-])(?:\+?1[\s.-])?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"
)


class Redactor:
    """Replaces PHI with ``[REDACTED]`` and keeps a running count for the dashboard."""

    def __init__(
        self,
        phi_keys: Iterable[str] | None = None,
        member_id_pattern: str | None = None,
    ) -> None:
        settings = get_settings()
        keys = sorted(
            phi_keys if phi_keys is not None else settings.PHI_KEYS, key=len, reverse=True
        )
        alt = "|".join(re.escape(k) for k in keys)
        already = re.escape(REDACTED)
        # Nearly every line has no PHI key at all; this one search lets it skip the three below.
        self._has_key = re.compile(alt, re.IGNORECASE)
        # key="value" / key='value'
        self._kv_quoted = re.compile(
            rf"""(?P<pre>(?<!\w)(?:{alt})\s*=\s*)(?!["']{already}["'])(?:"[^"]*"|'[^']*')""",
            re.IGNORECASE,
        )
        # key=value; stops at whitespace and at delimiters that could close a JSON string/object
        self._kv_bare = re.compile(
            rf"""(?P<pre>(?<!\w)(?:{alt})\s*=\s*)(?!{already})[^\s,;"'}}\]]+""",
            re.IGNORECASE,
        )
        # "key": "value" and "key": 123
        self._json = re.compile(
            rf'''(?P<pre>"(?:{alt})"\s*:\s*)(?!"{already}")(?:"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)''',
            re.IGNORECASE,
        )
        member_id = (
            member_id_pattern if member_id_pattern is not None else settings.MEMBER_ID_PATTERN
        )
        # SSN, email, member ID and phone in a single pass (this runs on every line)
        self._pattern_based = re.compile(
            f"{_SSN.pattern}|{_EMAIL.pattern}|(?:{member_id})|{_PHONE.pattern}"
        )
        self._total = 0

    @property
    def total(self) -> int:
        """PHI fields redacted so far (for "PHI fields redacted: N")."""
        return self._total

    def reset(self) -> None:
        self._total = 0

    def redact(self, text: str, *, track: bool = True) -> tuple[str, int]:
        """Return ``(redacted_text, redaction_count)``.

        ``track=False`` leaves the global counter alone, for redacting a second copy of the
        same line (parsed message vs raw) without counting the same PHI twice.
        """
        count = 0
        if self._has_key.search(text):
            text, n = self._kv_quoted.subn(lambda m: m["pre"] + REDACTED, text)
            count += n
            text, n = self._kv_bare.subn(lambda m: m["pre"] + REDACTED, text)
            count += n
            text, n = self._json.subn(lambda m: m["pre"] + f'"{REDACTED}"', text)
            count += n
        text, n = self._pattern_based.subn(REDACTED, text)
        count += n
        if track:
            self._total += count
        return text, count


_default: Redactor | None = None


def get_redactor() -> Redactor:
    """Process-wide redactor; its counter feeds the dashboard."""
    global _default
    if _default is None:
        _default = Redactor()
    return _default


def redact(text: str) -> tuple[str, int]:
    return get_redactor().redact(text)


def redacted_total() -> int:
    return get_redactor().total

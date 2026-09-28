"""Dry-run sink: append every notification payload, as one JSON line, to a local file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DryRunWriter:
    """The always-on audit copy of what was (or would have been) sent to AWS."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def write(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, default=str) + "\n")

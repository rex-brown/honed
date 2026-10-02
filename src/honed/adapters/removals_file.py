"""The removal list's file (`[paths] removals_file`, committed: `yardstick/removals.json`; DATASET.md, Removal).

JSON: `{"note": ..., "prs": [{"pr": "owner/name#N", "added": "YYYY-MM-DD"}], "comments": [{"id": "<node id>",
"added": "YYYY-MM-DD"}]}`. `core.removals` parses it; a missing file is an empty list.
"""

from __future__ import annotations

import json
from pathlib import Path

from honed.core.removals import Removals, RemovalsError, parse


def load(path: Path) -> Removals:
    if not path.exists():
        return Removals()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise RemovalsError(f"{path}: not JSON: {error}") from None
    if not isinstance(data, dict):
        raise RemovalsError(f"{path}: expected an object with `prs` and `comments`")
    try:
        return parse(data)
    except RemovalsError as error:
        raise RemovalsError(f"{path}: {error}") from None

"""Elapsed times in current and pre-meditation HnH event files.

The old elapsed_sec column was misnamed: it contains milliseconds. Keep that
interpretation for existing files; new event files explicitly use elapsed_ms.
"""

import math


def elapsed_milliseconds(row: dict) -> float | None:
    value = row.get("elapsed_ms", row.get("elapsed_sec", ""))
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None

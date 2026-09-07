"""Wall-clock pause ranges; never stitch a comparison window across a break."""
import math


def pause_ranges(metadata, end):
    result, previous = [], 0.0
    for item in metadata.get("pauses", []):
        start = float(item["start_sec"])
        stop = end if item.get("end_sec") is None else float(item["end_sec"])
        if not (math.isfinite(start) and math.isfinite(stop)
                and previous <= start <= stop <= end + 1e-6):
            raise ValueError("Invalid session pause range")
        result.append((start, stop))
        previous = stop
    return result


def active_ranges(start, end, pauses):
    cursor = start
    for left, right in pauses:
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            yield cursor, left
        cursor = max(cursor, right)
    if cursor < end:
        yield cursor, end


def active_duration(start, end, pauses):
    return sum(right - left for left, right in active_ranges(start, end, pauses))

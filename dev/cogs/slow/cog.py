"""slow: takes `seconds` (2 by default) to answer, long enough to stop its host mid-step."""

import time


def handle(entry_point, value, **feedback):
    seconds = float((value or {}).get("seconds", 2))
    time.sleep(seconds)
    return {"slept": seconds}

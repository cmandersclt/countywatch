"""
run.py — Railway entrypoint.

Railway cron fires this at 10:00 and 11:00 UTC every day. Only one of those is
6 AM in New York, and which one depends on daylight saving. This gate checks the
actual America/New_York hour and runs the pipeline only in the 6 AM ET window,
so the brief lands at 6 AM Eastern year-round without a DST-dependent cron.

The other invocation exits immediately (a cheap no-op spin). Set FORCE_RUN=1 to
bypass the gate for a manual test.
"""

import os
import runpy
import sys
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

TARGET_HOUR = int(os.environ.get("SEND_HOUR_ET", "6"))

if os.environ.get("FORCE_RUN") == "1":
    print("[run] FORCE_RUN set, bypassing the time gate.")
elif ZoneInfo is None:
    print("[run] zoneinfo unavailable; running without a time gate.")
else:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour != TARGET_HOUR:
        print(f"[run] {now_et:%Y-%m-%d %H:%M} ET is outside the "
              f"{TARGET_HOUR:02d}:00 ET send window; skipping.")
        sys.exit(0)
    print(f"[run] {now_et:%Y-%m-%d %H:%M} ET is in the send window; running.")

# Hand off to the existing pipeline exactly as `python fetch.py` would.
runpy.run_path("fetch.py", run_name="__main__")

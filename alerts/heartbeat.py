#!/usr/bin/env python3
"""Write a small "the check ran" heartbeat next to an alerts feed.

Usage: heartbeat.py <feed.json> <out.json> <label>

The feeds only change when a source publishes something new, and `generated_at`
inside them therefore means "alerts last changed". That is the wrong signal for
"is the pipeline alive": VPTS goes 2-4 weeks between notifications, so a page that
reads generated_at as liveness raises a false "feed has not updated" alarm most of
the time. This file carries the liveness signal instead. The workflows upload it on
EVERY successful run, whether or not the feed itself changed.

Works for both feed shapes: the flat VPTS file ({"alerts": [...]}) and the
multi-region file ({"alerts": [...], "metadata": {"errors": [...]}}).
"""
import json
import sys
from datetime import datetime, timezone


def main(feed_path, out_path, label):
    with open(feed_path, encoding="utf-8") as f:
        feed = json.load(f)

    alerts = feed.get("alerts") or []
    dates = [a.get("date_iso") for a in alerts if a.get("date_iso")]
    errors = (feed.get("metadata") or {}).get("errors") or []

    heartbeat = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "newest_alert": max(dates) if dates else None,
        "count": len(alerts),
        "errors": len(errors),
        "source": label,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(heartbeat, f, indent=2)
    print(f"heartbeat {label}: {heartbeat}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))

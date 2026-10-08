#!/usr/bin/env python3
"""Independent liveness check for the SafeGuide alert pipelines.

Usage: check_heartbeat.py <label> <status_url> <feed_url> <max_age_hours>

Exits 1 (and prints a GitHub ::error:: line) when, for one pipeline, any of these hold:
  * the heartbeat file cannot be fetched or parsed,
  * its `checked_at` is older than <max_age_hours>,
  * the feed it belongs to is not served (non-200), is not JSON, or has no alerts.

What it deliberately does NOT check: how old the newest ALERT is. A quiet source (VPTS goes
2-4 weeks between notices) is normal and must never fail this check; only "the check itself
stopped running" or "production stopped serving the feed" are failures.

Run from .github/workflows/watchdog.yml, which exists so that a stalled pipeline reaches a
human (GitHub emails the person who last edited the schedule when a scheduled run fails)
before it reaches readers as stale safety information.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; safeguide-alerts-watchdog)"


def fetch_json(url):
    req = urllib.request.Request(f"{url}?watchdog={int(datetime.now().timestamp())}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return json.loads(r.read().decode("utf-8", "replace"))


def main(label, status_url, feed_url, max_age_hours):
    problems = []

    try:
        status = fetch_json(status_url)
        stamp = datetime.fromisoformat(status["checked_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
        print(f"[{label}] heartbeat checked_at={status['checked_at']} ({age_h:.1f} h ago, limit {max_age_hours} h), "
              f"newest_alert={status.get('newest_alert')}, errors={status.get('errors')}")
        if age_h > max_age_hours:
            problems.append(f"heartbeat is {age_h:.0f} h old (limit {max_age_hours} h): the scheduled check has stopped running")
    except Exception as e:  # noqa: BLE001
        problems.append(f"heartbeat unreadable ({status_url}): {e}")

    try:
        feed = fetch_json(feed_url)
        n = len(feed.get("alerts") or [])
        print(f"[{label}] feed ok, {n} alerts")
        if n == 0:
            problems.append("feed served but contains no alerts")
    except Exception as e:  # noqa: BLE001
        problems.append(f"feed not served correctly ({feed_url}): {e}")

    for p in problems:
        print(f"::error title=SafeGuide alerts: {label}::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])))

#!/usr/bin/env python3
"""
The Know (national AU aggregator) parser.

The Know is a WordPress site exposing a clean REST API — no HTML scraping needed.
Custom post type `alerts_warnings` at:
    https://theknow.org.au/wp-json/wp/v2/alerts_warnings

Each record carries everything we need:
  - title.rendered            -> headline
  - acf.alert_publish_date    -> "YYYYMMDD" (fall back to `date`)
  - acf.source_organisation   -> e.g. "CanTEST", "NSW Health"
  - acf.alert_source_url       -> original source link (often Instagram)
  - acf.drug_sold_as / reason_for_concern
  - class_list slugs:  drug_taxonomy-<drug>, location_taxonomy-<state>,
                       alert_taxonomy-<type>  (human-readable — no term lookup needed)

Verified live 2026-07-17. The Know is run by Queensland Health (Metro North MH-ADS)
+ NCCRED and is the de-facto public alert channel for QLD/SA/WA/TAS (no standalone
feed) as well as carrying VIC/NSW/ACT. This is the single source that covers the
four "gap" states, so it is high priority.
"""

import html as _html
import importlib.util
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_FN_PATH = os.path.normpath(os.path.join(_HERE, "..", "fetch_notifications.py"))
_spec = importlib.util.spec_from_file_location("tk_fetch_notifications", _FN_PATH)
_fn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fn)  # type: ignore[union-attr]

API = "https://theknow.org.au/wp-json/wp/v2/alerts_warnings"
PER_PAGE = 100
MAX_PAGES = 5   # 200 records at 2026-10-08; hard stop so a pagination bug can never loop

# Retention. Reading only the newest 50 records (the original behaviour) silently cut off
# every alert from the low-volume jurisdictions: at 2026-10-08 The Know held QLD 7 / SA 4 /
# NT 1 alerts, all older than the 50th-newest record, so those states showed nothing at all.
# Keep everything recent, plus each jurisdiction's latest few however quiet it has been,
# so the page can say "newest alert on file for QLD is <date>" instead of staying blank.
# Overridable per source in alert-sources.json.
DEFAULT_RETENTION_DAYS = 270      # about 9 months, matches what "newest 50" used to reach
DEFAULT_MIN_PER_STATE = 2         # latest N per jurisdiction, regardless of age ...
DEFAULT_MAX_AGE_DAYS = 1100       # ... but never older than about 3 years (the date is always shown)

# location_taxonomy slug -> (state_code, region). Unknown slugs fall back to
# uppercased slug so a newly-added state still tags sensibly instead of dropping.
_LOC = {
    "vic": ("VIC", "Victoria"),
    "nsw": ("NSW", "New South Wales"),
    "act": ("ACT", "Australian Capital Territory"),
    "qld": ("QLD", "Queensland"),
    "sa": ("SA", "South Australia"),
    "wa": ("WA", "Western Australia"),
    "nt": ("NT", "Northern Territory"),
    "tas": ("TAS", "Tasmania"),
    "national": ("AU", "Australia (national)"),
}


def _severity(alert_slugs: List[str]) -> str:
    s = " ".join(alert_slugs)
    if "red-" in s:
        return "urgent"
    if "yellow-" in s:
        return "caution"
    if "public-drug-warning" in s or "drug-alert" in s or "drug-advisory" in s:
        return "caution"
    return "general"


def _from_class_list(class_list: List[str], prefix: str) -> List[str]:
    return [c[len(prefix):] for c in class_list if c.startswith(prefix)]


def _publish_date(rec: Dict[str, Any]):
    """acf.alert_publish_date is 'YYYYMMDD'; fall back to the WP `date` field."""
    acf = rec.get("acf") or {}
    raw = (acf.get("alert_publish_date") or "").strip()
    m = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", raw)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        wp = (rec.get("date") or "")[:10]
        try:
            y, mo, d = map(int, wp.split("-"))
        except ValueError:
            return None, None
    try:
        dt = datetime(y, mo, d)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%-d %B %Y")
    except ValueError:
        return None, None


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _fn.UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _fetch_all_records() -> List[Dict[str, Any]]:
    """Walk the WordPress pagination (X-WP-TotalPages) and return every record, newest first."""
    records: List[Dict[str, Any]] = []
    total_pages = 1
    page = 1
    while page <= total_pages and page <= MAX_PAGES:
        url = f"{API}?per_page={PER_PAGE}&page={page}&orderby=date&order=desc"
        req = urllib.request.Request(url, headers={"User-Agent": _fn.UA})
        with urllib.request.urlopen(req, timeout=45) as r:
            batch = json.loads(r.read().decode("utf-8", "replace"))
            try:
                total_pages = int(r.headers.get("X-WP-TotalPages") or 1)
            except ValueError:
                total_pages = 1
        if not isinstance(batch, list):
            raise ValueError("unexpected API response shape")
        records.extend(batch)
        page += 1
    return records


def _apply_retention(alerts: List[Dict[str, Any]], today, retention_days: int,
                     min_per_state: int, max_age_days: int) -> List[Dict[str, Any]]:
    """Keep recent alerts, plus each jurisdiction's latest `min_per_state` (up to max_age_days old)."""
    def age(a):
        try:
            return (today - datetime.strptime(a["date_iso"], "%Y-%m-%d").date()).days
        except (TypeError, ValueError):
            return None   # undated: keep, we cannot prove it is old

    keep_ids = set()
    by_state: Dict[str, List[Dict[str, Any]]] = {}
    for a in alerts:
        by_state.setdefault(a["location"]["state_code"], []).append(a)
        n = age(a)
        if n is None or n <= retention_days:
            keep_ids.add(a["id"])
    for code, items in by_state.items():
        if code == "AU":
            continue   # national / multi-state alerts only stay while recent, never via the floor
        newest = sorted(items, key=lambda a: a["date_iso"] or "", reverse=True)[:min_per_state]
        for a in newest:
            n = age(a)
            if n is None or n <= max_age_days:
                keep_ids.add(a["id"])
    return [a for a in alerts if a["id"] in keep_ids]


def parse(source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    covers = set(source_config.get("covers_state_codes", []))
    try:
        records = _fetch_all_records()
    except Exception as e:  # noqa: BLE001
        _fn.log("The Know API fetch failed:", e)
        return []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: List[Dict[str, Any]] = []

    for rec in records:
        class_list = rec.get("class_list", []) or []
        loc_slugs = _from_class_list(class_list, "location_taxonomy-")
        drug_slugs = _from_class_list(class_list, "drug_taxonomy-")
        alert_slugs = _from_class_list(class_list, "alert_taxonomy-")

        # An alert tagged for several jurisdictions (e.g. a product recall across 5 states)
        # used to be attributed to the first tag only, so it vanished from the other states'
        # views. Treat it as national and keep the full list in `states`.
        states: List[str] = []
        if len(loc_slugs) > 1:
            states = sorted({_LOC.get(sl, (sl.upper(), sl.title()))[0] for sl in loc_slugs})
            state_code, region = "AU", "Multiple states: " + ", ".join(states)
        else:
            loc_slug = loc_slugs[0] if loc_slugs else "national"
            state_code, region = _LOC.get(loc_slug, (loc_slug.upper(), loc_slug.title()))

        # If this source is configured to only surface the "gap" states, skip
        # jurisdictions that already have their own dedicated parser (VIC/NSW/ACT/NT).
        if covers and state_code not in covers:
            continue

        acf = rec.get("acf") or {}
        title = _html.unescape((rec.get("title") or {}).get("rendered", "")).strip()
        iso, human = _publish_date(rec)

        substances = [{"name": s.replace("-", " ").title(), "type": "reported", "confidence": "reported"}
                      for s in drug_slugs] or ([{"name": title[:64], "type": "reported", "confidence": "reported"}] if title else [])

        alerts.append({
            "id": "tk-" + str(rec.get("id")),
            "location": {"region": region, "state_code": state_code, "country": "AU", "city": None},
            "date_iso": iso,
            "date_human": human,
            "severity": _severity(alert_slugs),
            "title": title or "Drug alert",
            "summary": (acf.get("reason_for_concern") or acf.get("drug_sold_as") or title).strip(),
            "substances": substances,
            "source": {
                "name": "The Know" + (f" (via {acf['source_organisation']})" if acf.get("source_organisation") else ""),
                "url": rec.get("link", "https://theknow.org.au/"),
                "type": "aggregator",
                "original_url": acf.get("alert_source_url") or None,
            },
            "pdf_url": None,
            "scope": "national" if state_code == "AU" else "local",
            "states": states,
            "alert_types": alert_slugs,
            "last_updated": now,
        })

    fetched = len(alerts)
    alerts = _apply_retention(
        alerts, datetime.now(timezone.utc).date(),
        int(source_config.get("retention_days", DEFAULT_RETENTION_DAYS)),
        int(source_config.get("min_per_jurisdiction", DEFAULT_MIN_PER_STATE)),
        int(source_config.get("max_age_days", DEFAULT_MAX_AGE_DAYS)),
    )
    _fn.log(f"The Know: {fetched} records fetched, {len(alerts)} kept after retention")
    alerts.sort(key=lambda a: a["date_iso"] or "", reverse=True)
    return alerts

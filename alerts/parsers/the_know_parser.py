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
PER_PAGE = 50

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


def parse(source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    covers = set(source_config.get("covers_state_codes", []))
    try:
        records = _fetch_json(f"{API}?per_page={PER_PAGE}&orderby=date&order=desc")
    except Exception as e:  # noqa: BLE001
        _fn.log("The Know API fetch failed:", e)
        return []
    if not isinstance(records, list):
        _fn.log("The Know: unexpected API response shape.")
        return []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: List[Dict[str, Any]] = []

    for rec in records:
        class_list = rec.get("class_list", []) or []
        loc_slugs = _from_class_list(class_list, "location_taxonomy-")
        drug_slugs = _from_class_list(class_list, "drug_taxonomy-")
        alert_slugs = _from_class_list(class_list, "alert_taxonomy-")

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
            "alert_types": alert_slugs,
            "last_updated": now,
        })

    alerts.sort(key=lambda a: a["date_iso"] or "", reverse=True)
    return alerts

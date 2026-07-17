#!/usr/bin/env python3
"""
VPTS (Victorian Pill Testing Service) parser — unified-fetcher adapter.

IMPORTANT: this does NOT reimplement scraping. The authoritative, robots.txt-
compliant, PDF-caching scraper is `alerts/fetch_notifications.py`, which already
runs as its own GitHub Actions pipeline (infoveaea-arch/safeguide-alerts, 2x/day)
and writes the production `staging-site/alerts/alerts.json`.

This adapter REUSES that module's proven parsing functions and wraps each alert
in the multi-region SafeGuide schema (location / source / substances / scope) so
the Victorian feed can sit alongside other states/countries without duplicating —
and without regressing — any of the original title/summary/severity/date logic.
"""

import hashlib
import importlib.util
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

# Load fetch_notifications.py (a sibling of this parsers/ dir) as a module.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FN_PATH = os.path.normpath(os.path.join(_HERE, "..", "fetch_notifications.py"))
_spec = importlib.util.spec_from_file_location("vpts_fetch_notifications", _FN_PATH)
_fn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fn)  # type: ignore[union-attr]


import re as _re

# Canonical VPTS headline: "<detected> found in a sample expected to contain <expected>".
# Capture detected (before the verb) and expected (after "expected to contain/be"),
# stopping expected at a sentence break or the next narrative clause.
_VPTS_PATTERN = _re.compile(
    r"^(?P<detected>.+?)\s+(?:found|detected|identified)\s+in\s+a\s+sample\s+"
    r"expected\s+to\s+(?:contain|be)\s+(?P<expected>.+?)"
    r"(?:[.;]|\s+(?:An?|The|This|These|Two|One|Samples?|A\s)\b|$)",
    _re.IGNORECASE,
)
# Simpler "<expected> sold as / mis-sold as <detected>" and bare "expected to contain <x>".
_SOLD_AS = _re.compile(r"^(?P<expected>.+?)\s+mis-?sold\s+as\s+(?P<detected>.+?)(?:[.;]|$)", _re.IGNORECASE)


def _clean(name: str) -> str:
    n = _re.sub(r"\s{2,}", " ", (name or "")).strip(" .,-")
    # drop a trailing date that leaked in from a run-on sentence ("ketamine 11 March 2026")
    n = _re.sub(r"\s+\d{1,2}\s+[A-Z][a-z]+\s+20\d{2}.*$", "", n)
    # balance a dangling "(" so we don't emit "Alprazolam (Xanax"
    if n.count("(") > n.count(")"):
        n = n[: n.rindex("(")]
    return n.strip(" .,-")[:64]


def _substances_from(title: str, summary: str) -> List[Dict[str, Any]]:
    """
    Conservative substance extraction from the VPTS summary sentence only.
    Returns a detected/expected pair when the canonical pattern matches, else a
    single detected entry from the title. Never concatenates title + summary
    (that produced malformed names in earlier drafts).
    """
    s = (summary or "").strip()
    subs: List[Dict[str, Any]] = []

    m = _VPTS_PATTERN.match(s) or _SOLD_AS.match(s)
    if m:
        detected = _clean(m.group("detected"))
        expected = _clean(m.group("expected"))
        if detected:
            subs.append({"name": detected, "type": "detected", "confidence": "confirmed"})
        if expected:
            subs.append({"name": expected, "type": "expected", "confidence": "as_sold"})

    if not subs and title:
        subs.append({"name": _clean(title), "type": "detected", "confidence": "reported"})
    return subs


def parse(source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Entry point for the unified fetcher.

    Reuses fetch_notifications' listing + PDF-cache + text extraction, then
    recomputes the same derived fields and wraps them in the multi-region schema.
    Returns [] on any listing failure (never raises into the orchestrator).
    """
    try:
        html = _fn.fetch(_fn.LIST_URL)
    except Exception as e:  # noqa: BLE001
        _fn.log("VPTS listing fetch failed:", e)
        return []

    paths = _fn.list_pdf_links(html)
    if not paths:
        _fn.log("VPTS: no PDF links found — page structure may have changed.")
        return []

    cache = _fn.load_cache()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: List[Dict[str, Any]] = []

    for path in paths:
        url = _fn.BASE + path
        fname = path.rsplit("/", 1)[-1]
        cached = cache.get(url)
        if cached and cached.get("text") is not None:
            text = cached["text"]
        else:
            try:
                pdf = _fn.fetch(url, binary=True)
                text = _fn.pdftotext(pdf)
                cache[url] = {"text": text,
                             "hash": hashlib.sha1(pdf).hexdigest()[:12],
                             "parsed_at": now}
            except Exception as e:  # noqa: BLE001 — one bad PDF must not abort the run
                _fn.log("VPTS ERROR fetching", fname, "->", e)
                continue

        iso, human = _fn.parse_date(text)
        if not iso:
            import urllib.request
            iso, human = _fn.parse_date(urllib.request.unquote(fname))
        summary = _fn.summarise(text)
        title = _fn.title_from_filename(fname)
        if title == "Drug notification" or len(_re_alpha(title)) < 3:
            title = _fn.title_from_summary(summary) or title

        alerts.append({
            "id": "vic-" + hashlib.sha1(url.encode()).hexdigest()[:10],
            "location": {
                "region": source_config.get("region", "Victoria"),
                "state_code": source_config.get("state_code", "VIC"),
                "country": source_config.get("country", "AU"),
                "city": None,
            },
            "date_iso": iso,
            "date_human": human,
            "severity": _fn.severity_of(text),
            "title": title,
            "summary": summary,
            "substances": _substances_from(title, summary),
            "source": {
                "name": source_config.get("name", "Victorian Pill Testing Service (VPTS)"),
                "url": _fn.LIST_URL,
                "type": "institutional",
            },
            "pdf_url": url,
            "scope": "local",
            "last_updated": now,
        })

    # persist the shared text cache so subsequent runs / the original job reuse it
    try:
        _fn.os.makedirs(_fn.STATE_DIR, exist_ok=True)
        import json
        with open(_fn.CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        _fn.log("VPTS: could not write PDF cache:", e)

    alerts.sort(key=lambda a: a["date_iso"] or "", reverse=True)
    return alerts


def _re_alpha(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z]", "", s or "")

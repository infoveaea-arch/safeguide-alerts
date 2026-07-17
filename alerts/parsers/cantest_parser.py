#!/usr/bin/env python3
"""
CanTEST (ACT) parser — unified-fetcher adapter.

CanTEST's drug-notifications page is structurally the SAME as VPTS: a listing
page of PDF links whose substance/summary text lives only inside each PDF. So
this reuses the proven fetch/pdftotext/summarise/severity/title helpers from
`fetch_notifications.py` (same as vpts_parser) and only changes:
  - the listing URL,
  - the PDF-link pattern (WordPress /wp-content/uploads/.../*.pdf),
  - a glued-filename date fallback ("23July2025.pdf") since CanTEST filenames
    have no separators for the existing spaced-date regex.

Source: https://cantest.com.au/drug-notifications/  (verified 2026-07-17)
Note: Years 1-3 of notifications live on Instagram (@cantestcbr), not on-page;
this parser only sees the on-site PDFs (Year 4+).
"""

import hashlib
import importlib.util
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_FN_PATH = os.path.normpath(os.path.join(_HERE, "..", "fetch_notifications.py"))
_spec = importlib.util.spec_from_file_location("cantest_fetch_notifications", _FN_PATH)
_fn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fn)  # type: ignore[union-attr]

BASE = "https://cantest.com.au"
LIST_URL = f"{BASE}/drug-notifications/"

# Glued filename date, e.g. "23July2025" or "8May2026"
_MONTHS = ("January February March April May June July August September "
           "October November December").split()
_GLUED_DATE = re.compile(
    r"(\d{1,2})\s*(" + "|".join(_MONTHS) + r")\s*(20\d{2})", re.IGNORECASE)
_PDF_HREF = re.compile(r'href="([^"]+?\.pdf)"', re.IGNORECASE)


def _glued_date(fname: str):
    """Parse '23July2025.pdf' -> ('2026-07-23','23 July 2025'). Returns (None,None) on miss."""
    m = _GLUED_DATE.search(urllib.parse.unquote(fname))
    if not m:
        return None, None
    day, mon_name, year = int(m.group(1)), m.group(2).capitalize(), int(m.group(3))
    mon = _MONTHS.index(mon_name) + 1 if mon_name in _MONTHS else None
    if not mon:
        return None, None
    try:
        return datetime(year, mon, day).strftime("%Y-%m-%d"), f"{day} {mon_name} {year}"
    except ValueError:
        return None, None


def _declutter(text: str) -> str:
    """Strip the repeated 'CANTEST COMMUNITY NOTICE' banner + org boilerplate."""
    t = re.sub(r"(?i)\bcan?test\s+community\s+notice\b", " ", text or "")
    t = re.sub(r"(?i)\bcan?test\b", " ", t)
    t = re.sub(r"(?i)\b(directions health services|act health|health\.act\.gov\.au|"
               r"drug checking service)\b", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _list_pdf_links(html: str) -> List[str]:
    seen, out = set(), []
    for m in _PDF_HREF.finditer(html):
        url = urllib.parse.urljoin(BASE, m.group(1))
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse(source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        html = _fn.fetch(LIST_URL)
    except Exception as e:  # noqa: BLE001
        _fn.log("CanTEST listing fetch failed:", e)
        return []

    urls = _list_pdf_links(html)
    if not urls:
        _fn.log("CanTEST: no PDF links found — page structure may have changed.")
        return []

    cache = _fn.load_cache()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: List[Dict[str, Any]] = []

    for url in urls:
        fname = url.rsplit("/", 1)[-1]
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
                _fn.log("CanTEST ERROR fetching", fname, "->", e)
                continue

        # Prefer a real date inside the PDF; fall back to the glued filename date.
        iso, human = _fn.parse_date(text)
        if not iso:
            iso, human = _glued_date(fname)
        # CanTEST PDFs repeat a "CANTEST COMMUNITY NOTICE" banner masthead that
        # otherwise dominates the summary/title — strip it before summarising.
        clean = _declutter(text)
        summary = _fn.summarise(clean)
        # CanTEST filenames are just dates, so derive the title from the summary,
        # then strip the leading date the PDF body repeats ("8 MAY 2026 FENTANYL").
        title = _fn.title_from_summary(summary) or "Drug notification"
        title = re.sub(r"^\s*\d{1,2}\s+[A-Za-z]+\.?\s+20\d{2}\s*", "", title).strip() or title

        alerts.append({
            "id": "act-" + hashlib.sha1(url.encode()).hexdigest()[:10],
            "location": {
                "region": source_config.get("region", "Australian Capital Territory"),
                "state_code": source_config.get("state_code", "ACT"),
                "country": source_config.get("country", "AU"),
                "city": "Canberra",
            },
            "date_iso": iso,
            "date_human": human,
            "severity": _fn.severity_of(text),
            "title": title,
            "summary": summary,
            "substances": _substances_from(summary),
            "source": {
                "name": source_config.get("name", "CanTEST Health & Drug Checking Service"),
                "url": LIST_URL,
                "type": "institutional",
            },
            "pdf_url": url,
            "scope": "local",
            "last_updated": now,
        })

    try:
        _fn.os.makedirs(_fn.STATE_DIR, exist_ok=True)
        import json
        with open(_fn.CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        _fn.log("CanTEST: could not write PDF cache:", e)

    alerts.sort(key=lambda a: a["date_iso"] or "", reverse=True)
    return alerts


# Reuse the same conservative substance extractor shape as vpts_parser.
_VPTS_PATTERN = re.compile(
    r"^(?P<detected>.+?)\s+(?:found|detected|identified)\s+in\s+a\s+sample\s+"
    r"expected\s+to\s+(?:contain|be)\s+(?P<expected>.+?)"
    r"(?:[.;]|\s+(?:An?|The|This|These|Two|One|Samples?|A\s)\b|$)",
    re.IGNORECASE)


def _clean(name: str) -> str:
    n = re.sub(r"\s{2,}", " ", (name or "")).strip(" .,-")
    n = re.sub(r"\s+\d{1,2}\s+[A-Z][a-z]+\s+20\d{2}.*$", "", n)
    if n.count("(") > n.count(")"):
        n = n[: n.rindex("(")]
    return n.strip(" .,-")[:64]


def _substances_from(summary: str) -> List[Dict[str, Any]]:
    s = (summary or "").strip()
    m = _VPTS_PATTERN.match(s)
    if m:
        subs = []
        d, e = _clean(m.group("detected")), _clean(m.group("expected"))
        if d:
            subs.append({"name": d, "type": "detected", "confidence": "confirmed"})
        if e:
            subs.append({"name": e, "type": "expected", "confidence": "as_sold"})
        if subs:
            return subs
    return []

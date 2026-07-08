#!/usr/bin/env python3
"""
SafeGuide — VicPillTesting drug-notifications fetcher.

Auto-populates staging-site/alerts/alerts.json from the public VPTS
drug-notifications page so the SafeGuide app can surface current drug
alerts. Deterministic, no LLM, stdlib-only (+ `pdftotext` CLI).

robots.txt compliance: we ONLY fetch the human page (/drug-notifications)
and the public /s/*.pdf files — both allowed for User-agent: *. We never
touch the robots-disallowed ?format=json endpoints.

Source: https://www.vicpilltesting.org.au/drug-notifications
Run by: alerts/run.sh (launchd, daily). Safe to run manually.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.vicpilltesting.org.au"
LIST_URL = f"{BASE}/drug-notifications"
UA = "Mozilla/5.0 (SafeGuide harm-reduction mirror; +https://app.veaea.org/harm-reduction)"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.normpath(os.path.join(HERE, "..", "staging-site", "alerts"))
OUT_JSON = os.path.join(OUT_DIR, "alerts.json")
STATE_DIR = os.path.join(HERE, ".state")
CACHE_JSON = os.path.join(STATE_DIR, "pdf_cache.json")

MONTHS = ("January February March April May June July August September "
          "October November December").split()
MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
MONTH_ABBR = {m[:3].lower(): i + 1 for i, m in enumerate(MONTHS)}
MONTH_RE = "|".join(MONTHS + [m[:3] for m in MONTHS] + ["Sept"])

DATE_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RE})\.?\s+(20\d{{2}})\b",
                     re.IGNORECASE)


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, file=sys.stderr)


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def month_to_num(name):
    n = name.lower().rstrip(".")
    if n == "sept":
        n = "sep"
    return MONTH_NUM.get(n) or MONTH_ABBR.get(n[:3])


def parse_date(text):
    """Return ('YYYY-MM-DD', 'D Month YYYY') from the first date in text, or (None, None)."""
    m = DATE_RE.search(text or "")
    if not m:
        return None, None
    day, mon, year = int(m.group(1)), month_to_num(m.group(2)), int(m.group(3))
    if not mon:
        return None, None
    try:
        iso = datetime(year, mon, day).strftime("%Y-%m-%d")
    except ValueError:
        return None, None
    return iso, f"{day} {MONTHS[mon - 1]} {year}"


GLUED_DATE_RE = re.compile(
    rf"^\d{{1,2}}(?:st|nd|rd|th)?\s*(?:{MONTH_RE})[a-z]*\.?\s*-?\s*20\d{{2}}\b",
    re.IGNORECASE)


def title_from_filename(fname):
    """Turn '11-March-2026-Heroin-in-Ketamine-Public-Notification.pdf' -> 'Heroin in Ketamine'."""
    name = urllib.request.unquote(fname).rsplit(".", 1)[0]
    name = name.replace("+", " ").replace("_", "-")
    name = re.sub(r"[-\s]+", " ", name).strip()
    # strip leading date, both spaced ("11 March 2026") and glued ("5MAR 2026")
    name = DATE_RE.sub("", name, count=1).strip(" -")
    name = GLUED_DATE_RE.sub("", name).strip(" -")
    # strip boilerplate words even when glued to the substance (e.g. "CathinonePublic")
    name = re.sub(r"(?i)(general drug notification|public[\s-]*notification|"
                  r"notification|update|instagram[\s-]*reel|reel|final|draft|copy)", " ", name)
    name = re.sub(r"\b\d{3,}\b", "", name)            # date codes e.g. 1212
    name = re.sub(r"\s-?\s*[0-9a-z]{4,6}\s*$", "", name)  # squarespace hash suffix e.g. kd5g, z6lg
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return name or "Drug notification"


def title_from_summary(summary):
    """Derive a short title from the headline summary when the filename has none."""
    if not summary:
        return ""
    # take the subject before the first finding-verb / punctuation break
    head = re.split(r"\s*(?:\bfound\b|\bmis-?sold\b|\bsold\b|\bdetected\b|"
                    r"\bidentified\b|\bcontain\w*\b|\bexpected\b|[–—:*•])\s*",
                    summary, maxsplit=1, flags=re.IGNORECASE)[0]
    head = re.sub(r"\s{2,}", " ", head).strip(" .,-")
    words = head.split()
    if len(words) > 8:
        head = " ".join(words[:8])
    return head[:64].strip(" .,-")


def severity_of(text):
    head = (text or "")[:600].upper()
    if "HIGH ALERT" in head or "HIGH-ALERT" in head or "URGENT" in head:
        return "high"
    if "WARNING" in head:
        return "warning"
    return "general"


def summarise(text):
    """Best-effort one/two-line headline from page-1 text."""
    if not text:
        return ""
    # collapse the layout into lines, drop the masthead noise
    lines = [ln.strip() for ln in text.splitlines()]
    noise = re.compile(r"(?i)^(general|high alert|warning|urgent|drug|notification|"
                       r"melbourne|naarm|victorian pill testing|vpts|\(grid.*|date|"
                       r"public notification|www\.|the loop|ysas|youth)\b")
    body = " ".join(ln for ln in lines if ln and not noise.match(ln))
    body = re.sub(r"\s{2,}", " ", body).strip()
    # Prefer the canonical VPTS sentence patterns.
    for pat in (r"[^.]*\bfound in a sample expected to contain\b[^.]*\.?",
                r"[^.]*\b(sold as|mis-?sold as|detected|identified|contains?)\b[^.]*\.?"):
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            s = m.group(0).strip(" .")
            return (s[:200] + "…") if len(s) > 200 else s + "."
    return (body[:180] + "…") if len(body) > 180 else body


def pdftotext(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = f.name
    try:
        out = subprocess.run(
            ["pdftotext", "-l", "2", "-layout", path, "-"],
            capture_output=True, timeout=60, text=True)
        return out.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log("pdftotext failed:", e)
        return ""
    finally:
        os.unlink(path)


def list_pdf_links(html):
    """Ordered, de-duplicated /s/*.pdf links from the allowed listing page."""
    seen, out = set(), []
    for m in re.finditer(r'href="(/s/[^"]+?\.pdf)"', html, re.IGNORECASE):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def load_cache():
    try:
        with open(CACHE_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    cache = load_cache()

    log("Fetching listing:", LIST_URL)
    html = fetch(LIST_URL)
    paths = list_pdf_links(html)
    log(f"Found {len(paths)} notification PDF links")
    if not paths:
        log("WARNING: no PDF links found — page structure may have changed. "
            "Leaving existing alerts.json untouched.")
        return 0

    alerts = []
    new_count = 0
    for path in paths:
        url = BASE + path
        fname = path.rsplit("/", 1)[-1]
        cached = cache.get(url)
        if cached and cached.get("text") is not None:
            text = cached["text"]
        else:
            try:
                log("  new:", fname)
                pdf = fetch(url, binary=True)
                text = pdftotext(pdf)
                cache[url] = {
                    "text": text,
                    "hash": hashlib.sha1(pdf).hexdigest()[:12],
                    "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                new_count += 1
            except Exception as e:  # noqa: BLE001 — never let one bad PDF abort the run
                log("  ERROR fetching", fname, "->", e)
                continue
        # Derived fields are recomputed every run from cached text, so parser
        # improvements take effect without re-downloading.
        iso, human = parse_date(text)
        if not iso:
            iso, human = parse_date(urllib.request.unquote(fname))
        summary = summarise(text)
        title = title_from_filename(fname)
        if title == "Drug notification" or len(re.sub(r"[^A-Za-z]", "", title)) < 3:
            title = title_from_summary(summary) or title
        alerts.append({
            "date_iso": iso,
            "date_human": human,
            "severity": severity_of(text),
            "title": title,
            "summary": summary,
            "pdf_url": url,
        })

    # newest first; undated sink to the bottom
    alerts.sort(key=lambda a: a["date_iso"] or "", reverse=True)

    # Always persist the local text cache (not deployed).
    with open(CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    # Only rewrite the deployed JSON when the alert *content* changes, so the
    # daily job doesn't redeploy an identical file every morning. `generated_at`
    # therefore tracks "alerts last changed", which is the right UI semantic.
    try:
        with open(OUT_JSON, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = None

    if existing and existing.get("alerts") == alerts:
        log(f"No content change ({len(alerts)} alerts) — leaving {OUT_JSON} as-is")
        return 0

    out = {
        "source": LIST_URL,
        "source_name": "Victorian Pill Testing Service (VPTS)",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(alerts),
        "alerts": alerts,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    log(f"Wrote {OUT_JSON}: {len(alerts)} alerts ({new_count} newly parsed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

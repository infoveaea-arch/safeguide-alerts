#!/usr/bin/env python3
"""Regression tests for the "one failed fetch must never empty the feed" guards.

Run: python3 -m unittest alerts/test_guards.py   (stdlib only, no network)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import fetch_all_sources as fas  # noqa: E402
import fetch_notifications as fn  # noqa: E402
from parsers import the_know_parser as tk  # noqa: E402


def feed_with(n):
    return {"alerts": [{"id": f"a{i}", "title": f"Alert {i}", "substances": [], "location": {"state_code": "VIC"}}
                       for i in range(n)], "metadata": {}}


class SaveGuard(unittest.TestCase):
    def run_save(self, previous, new, errors=(), allow_empty=False):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "alerts-multi.json"
            if previous is not None:
                out.write_text(json.dumps(feed_with(previous)))
            f = fas.UnifiedAlertFetcher.__new__(fas.UnifiedAlertFetcher)
            f.errors, f.alerts = list(errors), feed_with(new)["alerts"]
            saved = f.save_output(out, allow_empty=allow_empty)
            return saved, len(json.loads(out.read_text())["alerts"]) if out.exists() else None

    def test_empty_never_replaces_non_empty(self):
        self.assertEqual(self.run_save(55, 0), (False, 55))

    def test_shrink_with_source_error_is_refused(self):
        self.assertEqual(self.run_save(55, 20, errors=["timeout"]), (False, 55))

    def test_genuine_shrink_without_errors_is_written(self):
        self.assertEqual(self.run_save(55, 54), (True, 54))

    def test_recovers_from_an_already_empty_feed(self):
        self.assertEqual(self.run_save(0, 55), (True, 55))

    def test_first_run_writes(self):
        self.assertEqual(self.run_save(None, 3), (True, 3))

    def test_allow_empty_override(self):
        self.assertEqual(self.run_save(55, 0, allow_empty=True), (True, 0))


class KnowParser(unittest.TestCase):
    @mock.patch.object(tk.time, "sleep")
    @mock.patch.object(tk.urllib.request, "urlopen", side_effect=TimeoutError("timed out"))
    def test_network_failure_raises_after_retries(self, urlopen, _sleep):
        with self.assertRaises(TimeoutError):
            tk.parse({})
        self.assertEqual(urlopen.call_count, tk.FETCH_ATTEMPTS)

    @mock.patch.object(tk, "_fetch_all_records", return_value=[])
    def test_zero_records_raises(self, _):
        with self.assertRaises(ValueError):
            tk.parse({})


class VptsGuard(unittest.TestCase):
    def run_main(self, published, pairs):
        """Run fn.main() with every PDF download failing; return (exit code, alerts left on disk)."""
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            out = out_dir / "alerts.json"
            out.write_text(json.dumps({"alerts": published}))
            with mock.patch.multiple(fn, OUT_DIR=str(out_dir), OUT_JSON=str(out), STATE_DIR=str(out_dir),
                                     CACHE_JSON=str(out_dir / "cache.json")), \
                    mock.patch.object(fn, "fetch", side_effect=[ "<html>", OSError("timed out"), OSError("timed out")]), \
                    mock.patch.object(fn, "list_pdf_links", return_value=pairs):
                rc = fn.main()
            return rc, json.loads(out.read_text())["alerts"]

    def test_failed_pdf_keeps_published_alert(self):
        published = [{"pdf_url": fn.BASE + "/s/a.pdf", "title": "A", "date_iso": "2026-10-01"},
                     {"pdf_url": fn.BASE + "/s/b.pdf", "title": "B", "date_iso": "2026-09-01"}]
        rc, left = self.run_main(published, [("/s/a.pdf", None), ("/s/b.pdf", None)])
        self.assertEqual((rc, left), (0, published))

    def test_everything_failing_never_writes_an_empty_feed(self):
        published = [{"pdf_url": fn.BASE + "/s/old.pdf", "title": "Old", "date_iso": "2026-10-01"}]
        rc, left = self.run_main(published, [("/s/new1.pdf", None), ("/s/new2.pdf", None)])
        self.assertEqual((rc, left), (1, published))


if __name__ == "__main__":
    unittest.main()

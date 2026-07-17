#!/usr/bin/env python3
"""
SafeGuide Unified Alert Fetcher
Orchestrates all drug alert sources and normalizes output to common schema.
Replaces multiple individual scrapers with single master fetcher.

Usage:
  python fetch_all_sources.py [--config config/alert-sources.json] [--sources vpts,nsw,wedinos]

Environment:
  Copy .env.template to .env and fill in API keys/credentials.
"""

import json
import sys
import os
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
import importlib

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
CONFIG_PATH = Path(__file__).parent / 'config' / 'alert-sources.json'
# NOTE: default output is alerts-multi.json — deliberately NOT alerts.json, which
# is owned by the separate single-region VPTS pipeline (alerts.yml). Writing here
# by default keeps the multi-region feed from ever clobbering the legacy file.
OUTPUT_PATH = Path(__file__).parent.parent / 'staging-site' / 'alerts' / 'alerts-multi.json'
PARSERS_PATH = Path(__file__).parent / 'parsers'


class UnifiedAlertFetcher:
    """Master fetcher that orchestrates all alert sources."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self.config = self._load_config()
        self.errors = []
        self.alerts = []

    def _load_config(self) -> Dict[str, Any]:
        """Load master configuration."""
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            sys.exit(1)

    def _load_parser(self, parser_name: str):
        """Dynamically load parser module."""
        try:
            module_name = self.config['parsers'][parser_name]['module'].replace('/', '.').replace('.py', '')
            return importlib.import_module(module_name)
        except Exception as e:
            logger.warning(f"Failed to load parser {parser_name}: {e}")
            return None

    def fetch_source(self, source_key: str, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Fetch and parse a single source.
        Returns normalized alerts or empty list on error.
        """
        if not source_config.get('enabled', False):
            logger.debug(f"Source {source_key} disabled, skipping")
            return []

        parser_name = source_config.get('parser')
        if not parser_name:
            logger.warning(f"No parser configured for {source_key}")
            return []

        parser_module = self._load_parser(parser_name)
        if not parser_module:
            # A not-yet-built parser is an expected state during rollout, not a
            # failure — skip the source without polluting self.errors (which would
            # otherwise make the CI run exit non-zero on every scheduled run).
            logger.info(f"Parser '{parser_name}' for '{source_key}' not built yet — skipping.")
            return []

        try:
            logger.info(f"Fetching {source_key} via {parser_name}...")

            # Call parser's main function
            alerts = parser_module.parse(source_config)
            logger.info(f"✓ {source_key}: {len(alerts)} alerts")
            return alerts

        except Exception as e:
            error_msg = f"Error fetching {source_key}: {str(e)}"
            self.errors.append(error_msg)
            logger.error(error_msg)
            return []

    def fetch_all(self, source_filter: List[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch all enabled sources and merge.

        Args:
            source_filter: List of source keys to fetch (None = all enabled)

        Returns:
            List of normalized alerts
        """
        all_alerts = []

        # Iterate through all sources
        for region_key, region_sources in self.config['sources'].items():
            for source_key, source_config in region_sources.items():
                # Skip if filter is set and source not in filter
                if source_filter and source_key not in source_filter:
                    continue

                alerts = self.fetch_source(source_key, source_config)
                all_alerts.extend(alerts)

        # Deduplicate by content hash
        self.alerts = self._deduplicate(all_alerts)
        logger.info(f"Total unique alerts: {len(self.alerts)}")

        return self.alerts

    def _deduplicate(self, alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate alerts by content hash."""
        seen = {}
        unique = []

        for alert in alerts:
            # Create hash from key fields
            content_hash = hash((
                alert.get('title'),
                tuple(s['name'] for s in alert.get('substances', [])),
                alert.get('location', {}).get('state_code')
            ))

            if content_hash not in seen:
                seen[content_hash] = True
                unique.append(alert)
            else:
                logger.debug(f"Skipping duplicate: {alert.get('title')}")

        return unique

    def save_output(self, output_path: Path = OUTPUT_PATH) -> bool:
        """Save normalized alerts to JSON."""
        try:
            # Skip rewriting if the alert *content* is unchanged, so the daily CI
            # job doesn't redeploy an identical file every run just because the
            # generated_at timestamp moved (mirrors fetch_notifications.py).
            try:
                with open(output_path, encoding="utf-8") as f:
                    existing = json.load(f)
            except (OSError, ValueError):
                existing = None
            # Compare ignoring the volatile per-alert `last_updated` (fetch time),
            # which would otherwise change every run and defeat change-detection.
            def _stable(alerts):
                return [{k: v for k, v in a.items() if k != "last_updated"} for a in (alerts or [])]
            if existing and _stable(existing.get("alerts")) == _stable(self.alerts):
                logger.info(f"No alert content change ({len(self.alerts)} alerts) — leaving {output_path} as-is")
                return True

            output_data = {
                "alerts": self.alerts,
                "metadata": {
                    "generated_at": datetime.utcnow().isoformat() + 'Z',
                    "total_alerts": len(self.alerts),
                    "by_location": self._count_by_location(),
                    "by_severity": self._count_by_severity(),
                    "errors": self.errors
                }
            }

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                json.dump(output_data, f, indent=2)

            logger.info(f"✓ Saved to {output_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to save output: {e}")
            return False

    def _count_by_location(self) -> Dict[str, int]:
        """Count alerts by location."""
        counts = {}
        for alert in self.alerts:
            location = alert.get('location', {}).get('state_code', 'unknown')
            counts[location] = counts.get(location, 0) + 1
        return counts

    def _count_by_severity(self) -> Dict[str, int]:
        """Count alerts by severity."""
        counts = {}
        for alert in self.alerts:
            severity = alert.get('severity', 'unknown')
            counts[severity] = counts.get(severity, 0) + 1
        return counts


def main():
    parser = argparse.ArgumentParser(description='SafeGuide Unified Alert Fetcher')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH, help='Config file path')
    parser.add_argument('--output', type=Path, default=OUTPUT_PATH, help='Output file path')
    parser.add_argument('--sources', type=str, help='Comma-separated source keys to fetch (default: all)')
    parser.add_argument('--dry-run', action='store_true', help='Parse but do not save')

    args = parser.parse_args()

    source_filter = args.sources.split(',') if args.sources else None

    fetcher = UnifiedAlertFetcher(args.config)
    fetcher.fetch_all(source_filter)

    if args.dry_run:
        logger.info("Dry run: not saving output")
        print(json.dumps(fetcher.alerts[:3], indent=2))  # Print first 3 as preview
    else:
        fetcher.save_output(args.output)

    sys.exit(0 if not fetcher.errors else 1)


if __name__ == '__main__':
    main()

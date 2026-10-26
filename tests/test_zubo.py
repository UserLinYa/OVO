"""Zubo scanner URL expansion tests (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itvpipe.zubo import generate_scan_urls


class ScanUrlTest(unittest.TestCase):
    def test_expand_ip_range_and_paths(self):
        urls = generate_scan_urls(
            ["http://1.2.3.4:8080"],
            ["/a.json", "b.json"],
            octet_start=1,
            octet_end=2,
            max_urls=10,
        )
        self.assertEqual(
            urls,
            [
                "http://1.2.3.1:8080/a.json",
                "http://1.2.3.1:8080/b.json",
                "http://1.2.3.2:8080/a.json",
                "http://1.2.3.2:8080/b.json",
            ],
        )

    def test_respects_cap_and_invalid_bases(self):
        urls = generate_scan_urls(
            ["not-a-host", "http://10.0.0.1"],
            ["/a"],
            octet_start=1,
            octet_end=254,
            max_urls=3,
        )
        self.assertEqual(len(urls), 3)
        self.assertEqual(urls[0], "http://10.0.0.1/a")


if __name__ == "__main__":
    unittest.main()

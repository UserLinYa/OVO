"""Playlist rendering and incremental writer tests."""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itvpipe.config import load_config
from itvpipe.models import ChannelSpec, SourceRecord, Stats
from itvpipe.output import build_m3u, build_txt, write_outputs
from itvpipe.state import OutputTracker


class OutputTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "_tmp" / "output"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.cfg = load_config(str(self.root / "config.toml"))
        self.cfg.logo.enabled = False
        self.cfg.output.max_sources_per_channel = 2
        self.specs = {
            "CCTV1": ChannelSpec("CCTV1", group="央视频道", order=0),
            "湖南卫视": ChannelSpec("湖南卫视", group="卫视频道", order=1),
        }
        self.selected = {
            "CCTV1": [
                SourceRecord(
                    channel="CCTV1",
                    url="http://a/1.m3u8",
                    canonical_url="http://a/1.m3u8",
                    group="央视频道",
                    status="ok",
                    kbps=2000,
                    latency_ms=20,
                    checked_at=1000,
                    last_ok_at=1000,
                ),
                SourceRecord(
                    channel="CCTV1",
                    url="http://a/2.m3u8",
                    canonical_url="http://a/2.m3u8",
                    group="央视频道",
                    status="ok",
                    kbps=1000,
                    latency_ms=30,
                    checked_at=1000,
                    last_ok_at=1000,
                ),
            ],
            "湖南卫视": [
                SourceRecord(
                    channel="湖南卫视",
                    url="http://a/3.m3u8",
                    canonical_url="http://a/3.m3u8",
                    group="卫视频道",
                    status="ok",
                    kbps=500,
                    latency_ms=40,
                    checked_at=1000,
                    last_ok_at=1000,
                )
            ],
        }
        self.grouped = [("央视频道", ["CCTV1"]), ("卫视频道", ["湖南卫视"])]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_m3u_attributes_and_limit(self):
        text = build_m3u(
            self.cfg, self.grouped, self.selected, self.specs, updated_at=1000
        )
        self.assertIn('tvg-id="CCTV-1"', text)
        self.assertIn('group-title="央视频道"', text)
        self.assertEqual(text.count("http://a/1.m3u8"), 1)
        self.assertEqual(text.count("http://a/2.m3u8"), 1)

    def test_update_entry_is_appended(self):
        text = build_m3u(
            self.cfg, self.grouped, self.selected, self.specs, updated_at=1000
        )
        self.assertIn('group-title="更新时间"', text)
        self.assertIn("http://123.MP4", text)
        self.assertTrue(text.rstrip().endswith("http://123.MP4"))

    def test_update_entry_can_be_disabled(self):
        self.cfg.output.append_update_entry = False
        text = build_m3u(
            self.cfg, self.grouped, self.selected, self.specs, updated_at=1000
        )
        self.assertNotIn("http://123.MP4", text)

    def test_txt_groups(self):
        text = build_txt(
            self.cfg, self.grouped, self.selected, self.specs, updated_at=1000
        )
        self.assertIn("央视频道,#genre#", text)
        self.assertIn("CCTV1,http://a/1.m3u8", text)
        self.assertIn("卫视频道,#genre#", text)
        self.assertIn("更新时间,#genre#", text)
        self.assertIn("http://123.MP4", text)
        self.assertTrue(text.rstrip().endswith("http://123.MP4"))

    def test_vlc_headers_are_preserved(self):
        self.selected["CCTV1"][0].headers = {"User-Agent": "CustomUA", "Referer": "http://ref/"}
        text = build_m3u(
            self.cfg, self.grouped, self.selected, self.specs, updated_at=1000
        )
        self.assertIn("#EXTVLCOPT:http-user-agent=CustomUA", text)
        self.assertIn("#EXTVLCOPT:http-referrer=http://ref/", text)

    def test_write_outputs_is_incremental(self):
        tracker = OutputTracker(
            self.root / "state" / "output_hashes.json", self.root
        )
        manifest = write_outputs(
            self.cfg,
            self.selected,
            self.grouped,
            self.specs,
            tracker,
            updated_at=1000,
            stats=Stats(),
        )
        self.assertTrue((self.root / "dist" / "playlist.m3u").is_file())
        self.assertTrue((self.root / "dist" / "groups" / "央视频道.m3u").is_file())
        self.assertIn(
            "http://123.MP4",
            (self.root / "dist" / "playlist.m3u").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "http://123.MP4",
            (self.root / "dist" / "groups" / "央视频道.m3u").read_text(encoding="utf-8"),
        )
        self.assertTrue(manifest["channels"] == 2)
        first_changed = set(tracker.changed)
        tracker.save()
        tracker2 = OutputTracker(
            self.root / "state" / "output_hashes.json", self.root
        )
        write_outputs(
            self.cfg,
            self.selected,
            self.grouped,
            self.specs,
            tracker2,
            updated_at=1000,
            stats=Stats(),
        )
        self.assertNotIn("dist/playlist.m3u", tracker2.changed)
        self.assertIn("dist/playlist.m3u", first_changed)


if __name__ == "__main__":
    unittest.main()

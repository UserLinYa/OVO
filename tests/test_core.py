"""Core normalisation/parsing tests (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itvpipe.config import load_config
from itvpipe.models import ChannelSpec, ParseBundle
from itvpipe.normalize import (
    alias_key,
    build_alias_index,
    canonical_url,
    infer_group,
    is_candidate_url,
    normalize_channel_name,
    normalize_group,
    normalize_playback_url,
    resolve_channel,
)
from itvpipe.parsers import (
    parse_demo_text,
    parse_json_text,
    parse_m3u_text,
    parse_txt_text,
)


class ChannelNormalisationTest(unittest.TestCase):
    def test_cctv_aliases(self):
        cases = {
            "CCTV1": "CCTV1",
            "CCTV-1": "CCTV1",
            "CCTV-01": "CCTV1",
            "CCTV1HD": "CCTV1",
            "CCTV1 综合": "CCTV1",
            "cctv1综合": "CCTV1",
            "中央一套": "CCTV1",
            "中央1台": "CCTV1",
            "CCTV-5+": "CCTV5+",
            "CCTV5+体育赛事": "CCTV5+",
            "CCTV-4K": "CCTV4K",
            "CCTV4K超高清": "CCTV4K",
            "CCTV 13 新闻": "CCTV13",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_channel_name(raw), expected)

    def test_satellite_and_unknown_names(self):
        cases = {
            "湖南卫视高清": "湖南卫视",
            "湖南卫视4K": "湖南卫视",
            "湖南 卫视": "湖南卫视",
            "安徽卫视": "安徽卫视",
            "凤凰卫视中文台": "凤凰卫视中文台",
            "翡翠台": "翡翠台",
            "【CCTV1】": "CCTV1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_channel_name(raw), expected)

    def test_alias_index_and_resolution(self):
        channels = {
            "CCTV1": ChannelSpec("CCTV1", ["中央一套", "CCTV-1"]),
            "湖南卫视": ChannelSpec("湖南卫视", ["湖南卫视高清"]),
        }
        index = build_alias_index(channels)
        self.assertEqual(resolve_channel("CCTV-1", index), "CCTV1")
        self.assertEqual(resolve_channel("中央一套高清", index), "CCTV1")
        self.assertEqual(resolve_channel("湖南卫视4K", index), "湖南卫视")
        self.assertEqual(resolve_channel("CCTV2", index), "CCTV2")

    def test_groups(self):
        self.assertEqual(normalize_group("📺央视频道"), "央视频道")
        self.assertEqual(normalize_group("央视"), "央视频道")
        self.assertEqual(normalize_group("卫视"), "卫视频道")
        self.assertEqual(normalize_group("港台"), "港澳台")
        self.assertEqual(infer_group("CCTV1"), "央视频道")
        self.assertEqual(infer_group("湖南卫视"), "卫视频道")
        self.assertEqual(infer_group("凤凰卫视中文台"), "港澳台")
        self.assertEqual(infer_group("翡翠台"), "港澳台")
        self.assertEqual(infer_group("大象新闻"), "新闻")


class UrlTest(unittest.TestCase):
    def test_canonical_url(self):
        value = canonical_url(
            "HTTP://Example.COM:80/live/a.m3u8?utm_source=x&token=1#frag"
        )
        self.assertEqual(value, "http://example.com/live/a.m3u8?token=1")
        self.assertEqual(
            canonical_url("https://example.com/a b.m3u8"),
            "https://example.com/a%20b.m3u8",
        )

    def test_playback_url_and_options(self):
        self.assertEqual(
            normalize_playback_url("http://example.com/a.m3u8|User-Agent=foo"),
            "http://example.com/a.m3u8",
        )
        self.assertEqual(
            normalize_playback_url("http://example.com/a b.m3u8?x=1"),
            "http://example.com/a%20b.m3u8?x=1",
        )

    def test_candidate_filter(self):
        self.assertTrue(is_candidate_url("http://example.com/live.m3u8")[0])
        self.assertFalse(is_candidate_url("rtp://239.1.1.1:1234")[0])
        self.assertFalse(is_candidate_url("http://239.1.1.1/live.m3u8")[0])
        self.assertFalse(is_candidate_url("http://example.com/paiptv/x.m3u8")[0])
        self.assertFalse(is_candidate_url("not-a-url")[0])


class DemoParserTest(unittest.TestCase):
    def test_demo_whitelist_and_inline_source(self):
        text = """
        # comment
        CCTV1 | CCTV-1;中央一套 | 央视频道
        湖南卫视 | 湖南卫视高清 | 卫视频道 | https://logo/hunan.png
        CCTV2,http://example.com/cctv2.m3u8
        央视频道,#genre#
        CCTV1,http://example.com/cctv1.m3u8
        """
        bundle = parse_demo_text(text)
        self.assertEqual(len(bundle.channels), 3)
        self.assertEqual(bundle.channels["CCTV1"].group, "央视频道")
        self.assertIn("中央一套", bundle.channels["CCTV1"].aliases)
        self.assertEqual(bundle.channels["湖南卫视"].logo, "https://logo/hunan.png")
        self.assertEqual(len(bundle.sources), 2)
        self.assertEqual(bundle.sources[0].name, "CCTV2")
        self.assertEqual(bundle.sources[1].name, "CCTV1")

    def test_demo_duplicate_names_merge(self):
        bundle = parse_demo_text(
            "CCTV1 | CCTV-1 | 央视频道\nCCTV-01 | 中央一套 | 央视频道\n"
        )
        self.assertEqual(list(bundle.channels), ["CCTV1"])
        self.assertIn("CCTV-1", bundle.channels["CCTV1"].aliases)
        self.assertIn("中央一套", bundle.channels["CCTV1"].aliases)


class M3UParserTest(unittest.TestCase):
    def test_attributes_relative_url_and_extgrp(self):
        text = """#EXTM3U x-tvg-url="http://epg/x.xml"
#EXTINF:-1 tvg-id="CCTV-1" tvg-name="CCTV1" tvg-logo="http://logo/1.png" group-title="央视",CCTV1
#EXTGRP:央视频道
http://cdn.example/list/master.m3u8
#EXTINF:-1,湖南卫视高清
#EXTVLCOPT:http-user-agent=custom
relative/hunan.m3u8
"""
        bundle = parse_m3u_text(text, origin="http://origin.example/list/")
        self.assertEqual(bundle.epg_urls, ["http://epg/x.xml"])
        self.assertEqual(len(bundle.sources), 2)
        first, second = bundle.sources
        self.assertEqual(first.name, "CCTV1")
        self.assertEqual(first.group, "央视频道")
        self.assertEqual(first.logo, "http://logo/1.png")
        self.assertEqual(first.url, "http://cdn.example/list/master.m3u8")
        self.assertEqual(second.name, "湖南卫视")
        self.assertEqual(second.url, "http://origin.example/list/relative/hunan.m3u8")
        self.assertEqual(second.headers.get("User-Agent"), "custom")

    def test_pipe_options_are_stripped(self):
        bundle = parse_m3u_text(
            "#EXTM3U\n#EXTINF:-1,CCTV1\nhttp://example.com/a.m3u8|User-Agent=x\n"
        )
        self.assertEqual(bundle.sources[0].url, "http://example.com/a.m3u8")


class TxtJsonParserTest(unittest.TestCase):
    def test_txt_groups(self):
        bundle = parse_txt_text(
            "央视频道,#genre#\nCCTV-1,http://a/1.m3u8\n"
            "卫视频道,#genre#\n湖南卫视|http://a/2.m3u8\n"
        )
        self.assertEqual(len(bundle.sources), 2)
        self.assertEqual(bundle.sources[0].group, "央视频道")
        self.assertEqual(bundle.sources[0].name, "CCTV1")
        self.assertEqual(bundle.sources[1].group, "卫视频道")
        self.assertEqual(bundle.sources[1].name, "湖南卫视")

    def test_zubo_json(self):
        bundle = parse_json_text(
            '{"data":[{"name":"CCTV2","url":"http://a/2.m3u8"},'
            '{"name":"CCTV1","url":"http://a/1.m3u8"}]}',
            origin="json",
        )
        self.assertEqual([s.name for s in bundle.sources], ["CCTV2", "CCTV1"])

    def test_name_to_url_mapping(self):
        bundle = parse_json_text('{"CCTV1":"http://a/1.m3u8"}')
        self.assertEqual(len(bundle.sources), 1)
        self.assertEqual(bundle.sources[0].name, "CCTV1")


class ConfigTest(unittest.TestCase):
    def test_defaults_load(self):
        cfg = load_config(str(Path(__file__).resolve().parent / "none.toml"))
        self.assertEqual(cfg.output.max_sources_per_channel, 3)
        self.assertEqual(cfg.filters.require_demo, True)
        self.assertIn("央视频道", cfg.output.group_order)


if __name__ == "__main__":
    unittest.main()

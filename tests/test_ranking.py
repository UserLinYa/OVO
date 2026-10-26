"""测速排序纯逻辑的单元测试（不依赖 aiohttp / 网络）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ITVlist
from ITVlist import ProbeResult


class ParsePlaylistTest(unittest.TestCase):
    def test_media_playlist_returns_first_segment(self):
        content = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXTINF:2.0,
/live/seg-1.ts
#EXTINF:2.0,
/live/seg-2.ts
"""
        self.assertEqual(ITVlist.parse_playlist(content), ("segment", "/live/seg-1.ts"))

    def test_master_playlist_returns_variant(self):
        content = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080
1080p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000
720p/index.m3u8
"""
        self.assertEqual(ITVlist.parse_playlist(content), ("variant", "1080p/index.m3u8"))

    def test_comments_only_returns_none(self):
        self.assertIsNone(ITVlist.parse_playlist("#EXTM3U\n#EXT-X-ENDLIST\n"))
        self.assertIsNone(ITVlist.parse_playlist(""))


class RankChannelSourcesTest(unittest.TestCase):
    def _result(self, name, url, kbps=None, latency=50.0, ok=True):
        return ProbeResult(name=name, url=url, ok=ok, kbps=kbps, latency_ms=latency)

    def test_sorts_by_speed_desc_within_channel(self):
        results = [
            self._result("CCTV1", "http://a/1.ts", kbps=1200.0, latency=80.0),
            self._result("CCTV1", "http://a/2.ts", kbps=8200.0, latency=20.0),
            self._result("CCTV1", "http://a/3.ts", kbps=300.0, latency=5.0),
        ]
        ranked = ITVlist.rank_channel_sources(results, keep=20)
        self.assertEqual(
            [item.url for item in ranked["CCTV1"]],
            ["http://a/2.ts", "http://a/1.ts", "http://a/3.ts"],
        )

    def test_channels_are_independent(self):
        results = [
            self._result("CCTV1", "http://a/1.ts", kbps=1000.0),
            self._result("CCTV2", "http://b/1.ts", kbps=9000.0),
            self._result("CCTV1", "http://a/2.ts", kbps=9000.0),
            self._result("CCTV2", "http://b/2.ts", kbps=100.0),
        ]
        ranked = ITVlist.rank_channel_sources(results)
        self.assertEqual(ranked["CCTV1"][0].url, "http://a/2.ts")
        self.assertEqual(ranked["CCTV2"][0].url, "http://b/1.ts")

    def test_unreachable_sources_are_dropped(self):
        results = [
            self._result("CCTV1", "http://a/dead.ts", ok=False, latency=999999.0),
            self._result("CCTV1", "http://a/ok.ts", kbps=500.0),
        ]
        ranked = ITVlist.rank_channel_sources(results)
        self.assertEqual([item.url for item in ranked["CCTV1"]], ["http://a/ok.ts"])

    def test_measured_speed_ranks_above_latency_only(self):
        results = [
            self._result("CCTV1", "http://a/only-latency.ts", kbps=None, latency=1.0),
            self._result("CCTV1", "http://a/measured.ts", kbps=50.0, latency=500.0),
        ]
        ranked = ITVlist.rank_channel_sources(results)
        self.assertEqual(ranked["CCTV1"][0].url, "http://a/measured.ts")

    def test_latency_breaks_speed_ties(self):
        results = [
            self._result("CCTV1", "http://a/slow.ts", kbps=1000.0, latency=90.0),
            self._result("CCTV1", "http://a/fast.ts", kbps=1000.0, latency=10.0),
        ]
        ranked = ITVlist.rank_channel_sources(results)
        self.assertEqual(ranked["CCTV1"][0].url, "http://a/fast.ts")

    def test_duplicate_url_kept_once(self):
        results = [
            self._result("CCTV1", "http://a/1.ts", kbps=1000.0),
            self._result("CCTV1", "http://a/1.ts", kbps=1000.0),
        ]
        ranked = ITVlist.rank_channel_sources(results)
        self.assertEqual(len(ranked["CCTV1"]), 1)

    def test_keep_limit(self):
        results = [
            self._result("CCTV1", f"http://a/{i}.ts", kbps=float(i)) for i in range(30)
        ]
        ranked = ITVlist.rank_channel_sources(results, keep=5)
        self.assertEqual(len(ranked["CCTV1"]), 5)
        self.assertEqual(ranked["CCTV1"][0].kbps, 29.0)

    def test_empty_channel_is_omitted(self):
        self.assertEqual(ITVlist.rank_channel_sources([]), {})


class SourceHelpersTest(unittest.TestCase):
    def test_dedupe_sources(self):
        pairs = [("CCTV1", "http://a/1.ts"), ("CCTV1", "http://a/1.ts"),
                 ("CCTV2", "http://a/1.ts")]
        self.assertEqual(
            ITVlist.dedupe_sources(pairs),
            [("CCTV1", "http://a/1.ts"), ("CCTV2", "http://a/1.ts")],
        )

    def test_group_names_by_url(self):
        pairs = [("CCTV1", "http://a/1.ts"), ("CCTV1", "http://a/2.ts"),
                 ("CCTV2", "http://a/1.ts")]
        grouped = ITVlist.group_names_by_url(pairs)
        self.assertEqual(grouped["http://a/1.ts"], ["CCTV1", "CCTV2"])
        self.assertEqual(grouped["http://a/2.ts"], ["CCTV1"])


if __name__ == "__main__":
    unittest.main()

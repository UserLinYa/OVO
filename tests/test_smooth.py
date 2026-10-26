"""流畅度排序的单元测试：测量统计、评分、记录排序、配置。

这些测试不访问网络，用可控的「到达时间序列」模拟不同网络行为。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itvpipe.config import RankingConfig, load_config
from itvpipe.models import ChannelSpec, ProbeResult, SourceRecord
from itvpipe.normalize import canonical_url
from itvpipe.pipeline import select_channels
from itvpipe.probe import PlaylistInfo, parse_playlist_details, probe_one, rank_channel_sources
from itvpipe.state import StateManager
from itvpipe.smooth import (
    RankWeights,
    ScoreOptions,
    analyze_samples,
    bucket_score,
    required_kbps,
    smoothness_score,
    speed_component,
)


CHUNK = 16 * 1024

# 稳定但速度一般：每 0.1s 到 16 KB ≈ 1.3 Mbps，速率几乎没有波动。
STEADY_SPEC = [(0.1, CHUNK)] * 25
# "测速很快、播放很卡"：开头两个大块冲进来（平均速度好看），随后 0.6s 完全
# 没有数据，最后只能低速滴流。总字节/总时间很高，但实际播放会卡。
BURSTY_SPEC = (
    [(0.1, 128 * 1024), (0.05, 128 * 1024), (0.6, CHUNK)]
    + [(0.13, CHUNK)] * 7
)


def _pattern(spec):
    """spec: [(gap_seconds, bytes), ...] -> (samples, first_at, last_at, total)."""

    clock = 100.0
    samples = []
    total = 0
    for gap, size in spec:
        clock += gap
        samples.append((clock, size))
        total += size
    return samples, samples[0][0], samples[-1][0], total


class AnalyzeSamplesTest(unittest.TestCase):
    def test_steady_stream_has_no_jitter_or_stalls(self):
        samples, first_at, last_at, total = _pattern(STEADY_SPEC)
        stats = analyze_samples(samples, first_at=first_at, last_at=last_at)

        self.assertEqual(stats.stalls, 0)
        self.assertLess(stats.jitter, 0.02)
        # 16 KB / 0.1s = 1310 kbps，桶速率应该贴着这个值
        self.assertAlmostEqual(stats.kbps_p50, 1310.7, delta=60)
        self.assertEqual(total, 25 * CHUNK)

    def test_burst_then_stall_shows_jitter_and_stalls(self):
        samples, first_at, last_at, total = _pattern(BURSTY_SPEC)
        stats = analyze_samples(samples, first_at=first_at, last_at=last_at)

        self.assertGreaterEqual(stats.stalls, 1)
        self.assertGreater(stats.jitter, 0.2)
        # 持续速率必须明显低于"总字节/总时间"的平均值
        average_kbps = total * 8 / 1000 / (last_at - first_at)
        self.assertLess(stats.kbps_p50, average_kbps)

    def test_slow_but_steady_source_is_not_flagged_as_stalling(self):
        # 1 Mbps 稳定流：每次读 16 KB 要 0.13s，间隔长但不该算停顿。
        samples, first_at, last_at, _ = _pattern([(0.13, CHUNK)] * 20)
        stats = analyze_samples(samples, first_at=first_at, last_at=last_at)

        self.assertEqual(stats.stalls, 0)
        self.assertLess(stats.jitter, 0.02)
        self.assertAlmostEqual(stats.kbps_p50, 1008, delta=80)

    def test_very_fast_stream_keeps_its_rate(self):
        # 千兆/回环链路：所有读取都挤在最初几十毫秒里，不能因此把持续速率算成 0。
        samples, first_at, last_at, _ = _pattern([(0.001, CHUNK)] * 24)
        stats = analyze_samples(samples, first_at=first_at, last_at=last_at)
        self.assertIsNotNone(stats.kbps_p50)
        self.assertGreater(stats.kbps_p50, 1000)
        self.assertEqual(stats.stalls, 0)

    def test_single_read_sample_has_no_distribution(self):
        # 只读了一次：没有分布可算，排序会退回平均速率。
        stats = analyze_samples([(100.0, 200000)], first_at=100.0, last_at=100.0)
        self.assertIsNone(stats.kbps_p50)
        self.assertEqual(stats.samples, 0)

    def test_empty_samples(self):
        stats = analyze_samples([], first_at=1.0, last_at=1.0)
        self.assertIsNone(stats.kbps_p50)
        self.assertEqual(stats.stalls, 0)
        self.assertEqual(stats.jitter, 0.0)


class ScoreTest(unittest.TestCase):
    def test_score_is_bounded(self):
        for kbps in (None, 0, 1, 500, 5000, 999999):
            score = smoothness_score(kbps=kbps, quality="1080p")
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)

    def test_speed_beyond_requirement_stops_mattering(self):
        enough = speed_component(4000 * 1.2, quality="1080p")
        much_more = speed_component(40000, quality="1080p")
        self.assertAlmostEqual(enough, 1.0, places=3)
        self.assertAlmostEqual(much_more, 1.0, places=3)

    def test_bitrate_below_requirement_is_penalised(self):
        low = speed_component(900, quality="1080p")
        high = speed_component(4000, quality="1080p")
        self.assertLess(low, 0.3)
        self.assertGreater(high, low * 3)

    def test_unknown_quality_uses_default_requirement(self):
        self.assertEqual(required_kbps(""), 2500.0)
        self.assertEqual(required_kbps("1080p"), 4000.0)
        self.assertEqual(required_kbps("unknown"), 2500.0)
        self.assertEqual(required_kbps("", {"": 900.0}), 900.0)

    def test_declared_variant_bitrate_raises_requirement(self):
        # 源声明 4 Mbps 变体，只测到 2 Mbps：就算标称 720p 也会被迫降码率。
        without_declared = speed_component(2000, quality="720p")
        with_declared = speed_component(2000, quality="720p", variant_kbps=4000)
        self.assertAlmostEqual(without_declared, 2000 / 2500 / 1.2, places=4)
        self.assertLess(with_declared, without_declared)

    def test_segment_cannot_be_downloaded_in_real_time(self):
        # 4 秒分片、4 MB 大小：3 Mbps 需要 10.7s 才能下完，必然越播越卡。
        slow = speed_component(
            3000, quality="1080p", segment_seconds=4.0, content_length=4 * 1024 * 1024
        )
        fast = speed_component(
            20000, quality="1080p", segment_seconds=4.0, content_length=4 * 1024 * 1024
        )
        self.assertLess(slow, fast)
        self.assertLess(slow, 0.4)

    def test_bursty_fast_source_loses_to_steady_slower_source(self):
        bursty = smoothness_score(
            kbps=9000, quality="720p", jitter=1.4, stalls=3, ttfb_ms=250, reliability=1.0
        )
        steady = smoothness_score(
            kbps=2600, quality="720p", jitter=0.02, stalls=0, ttfb_ms=120, reliability=0.95
        )
        self.assertGreater(steady, bursty)

    def test_instability_penalties_are_monotonic(self):
        base = dict(kbps=3000, quality="1080p")
        clean = smoothness_score(**base)
        one_stall = smoothness_score(**base, stalls=1)
        many_stalls = smoothness_score(**base, stalls=4)
        jittery = smoothness_score(**base, jitter=0.8)
        tardy = smoothness_score(**base, ttfb_ms=3000)
        flaky = smoothness_score(**base, reliability=0.3)

        self.assertGreater(clean, one_stall)
        self.assertGreater(one_stall, many_stalls)
        self.assertGreater(clean, jittery)
        self.assertGreater(clean, tardy)
        self.assertGreater(clean, flaky)

    def test_reachable_only_is_capped(self):
        score = smoothness_score(kbps=None, quality="1080p", stalls=0, jitter=0.0)
        self.assertLessEqual(score, 30.0)

    def test_score_bucket(self):
        self.assertEqual(bucket_score(80.4), 80)
        self.assertEqual(bucket_score(80.6), 81)
        self.assertEqual(bucket_score(80.4, 5.0), 16)

    def test_custom_weights_change_ordering(self):
        bursty = dict(kbps=9000, quality="720p", jitter=1.4, stalls=3, ttfb_ms=250)
        steady = dict(kbps=2600, quality="720p", jitter=0.02, stalls=0, ttfb_ms=120)
        speed_only = ScoreOptions(
            weights=RankWeights(
                speed=1.0, stability=0.0, stall=0.0, latency=0.0, reliability=0.0
            )
        )
        self.assertGreater(
            smoothness_score(**bursty, options=speed_only),
            smoothness_score(**steady, options=speed_only),
        )


def _record(channel, url, *, kbps, jitter=0.0, stalls=0, latency=100.0, ewma=0.9,
            quality="", p50=None, now=1000.0):
    record = SourceRecord(
        channel=channel,
        url=url,
        canonical_url=canonical_url(url),
        status="ok",
        kbps=kbps,
        kbps_p50=p50,
        jitter=jitter,
        stalls=stalls,
        latency_ms=latency,
        quality=quality,
        success_ewma=ewma,
        checked_at=now,
        last_ok_at=now,
    )
    return record


class RecordRankingTest(unittest.TestCase):
    def test_smoothness_mode_prefers_stable_source(self):
        fast_bursty = _record("CCTV1", "http://a/bursty.ts", kbps=9000, jitter=1.4, stalls=3)
        slower_steady = _record("CCTV1", "http://a/steady.ts", kbps=2600, jitter=0.02, stalls=0)
        ordered = sorted([fast_bursty, slower_steady], key=lambda r: r.rank_key())
        self.assertEqual([r.url for r in ordered], [slower_steady.url, fast_bursty.url])

    def test_speed_mode_restores_legacy_order(self):
        fast_bursty = _record("CCTV1", "http://a/bursty.ts", kbps=9000, jitter=1.4, stalls=3)
        slower_steady = _record("CCTV1", "http://a/steady.ts", kbps=2600, jitter=0.02, stalls=0)
        ordered = sorted(
            [slower_steady, fast_bursty],
            key=lambda r: r.rank_key(ScoreOptions(mode="speed")),
        )
        self.assertEqual([r.url for r in ordered], [fast_bursty.url, slower_steady.url])

    def test_flaky_history_lowers_score(self):
        solid = _record("CCTV1", "http://a/solid.ts", kbps=4000, ewma=1.0)
        flaky = _record("CCTV1", "http://a/flaky.ts", kbps=4000, ewma=0.2)
        self.assertGreater(solid.smooth_score(), flaky.smooth_score())

    def test_p50_is_preferred_over_burst_average(self):
        burst = _record("CCTV1", "http://a/burst.ts", kbps=9000, p50=1800)
        steady = _record("CCTV1", "http://a/steady.ts", kbps=3000)
        self.assertEqual(burst.effective_kbps, 1800)
        self.assertGreater(steady.smooth_score(), burst.smooth_score())

    def test_failed_record_sorts_last(self):
        failed = _record("CCTV1", "http://a/dead.ts", kbps=None)
        failed.status = "failed"
        ok = _record("CCTV1", "http://a/ok.ts", kbps=1000)
        ordered = sorted([failed, ok], key=lambda r: r.rank_key())
        self.assertEqual([r.url for r in ordered], [ok.url, failed.url])


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ovo-smooth-"))
        self.cfg = load_config(str(self.root / "config.toml"))
        self.cfg.output.max_sources_per_channel = 1
        self.state = StateManager(self.cfg)
        self.specs = {"CCTV1": ChannelSpec("CCTV1", group="央视频道", order=0)}

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _add(self, record):
        bucket = self.state.library.setdefault(record.channel, {})
        bucket[record.canonical_url] = record

    def test_selects_smoothest_source(self):
        fast_bursty = _record("CCTV1", "http://a/bursty.ts", kbps=9000, jitter=1.4, stalls=3)
        steady = _record("CCTV1", "http://a/steady.ts", kbps=2600, jitter=0.02, stalls=0)
        self._add(fast_bursty)
        self._add(steady)
        selected, _, _ = select_channels(self.state, self.specs, self.cfg, now=1001.0)
        self.assertEqual([r.url for r in selected["CCTV1"]], [steady.url])

    def test_min_smooth_score_drops_jittery_source(self):
        bursty = _record("CCTV1", "http://a/bursty.ts", kbps=9000, jitter=2.0, stalls=6)
        self._add(bursty)
        self.cfg.ranking.min_smooth_score = 70.0
        selected, _, dropped = select_channels(self.state, self.specs, self.cfg, now=1001.0)
        self.assertNotIn("CCTV1", selected)
        self.assertGreaterEqual(dropped, 1)

    def test_equal_scores_are_ordered_deterministically(self):
        first = _record("CCTV1", "http://a/2.ts", kbps=2600)
        second = _record("CCTV1", "http://a/1.ts", kbps=2600)
        self.cfg.output.max_sources_per_channel = 2
        # 插入顺序相反，输出仍应按 URL 稳定排序
        self._add(first)
        self._add(second)
        selected, _, _ = select_channels(self.state, self.specs, self.cfg, now=1001.0)
        self.assertEqual([r.url for r in selected["CCTV1"]], [second.url, first.url])


class PlaylistDetailsTest(unittest.TestCase):
    def test_master_playlist_reports_declared_bandwidth(self):
        text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n720.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\n1080.m3u8\n"
        )
        info = parse_playlist_details(text)
        self.assertIsInstance(info, PlaylistInfo)
        self.assertEqual((info.kind, info.uri, info.quality), ("variant", "1080.m3u8", "1080p"))
        self.assertEqual(info.bandwidth_kbps, 3000.0)

    def test_media_playlist_reports_bitrate_and_segment_length(self):
        text = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:6\n"
            "#EXT-X-BITRATE:2800\n"
            "#EXTINF:6.006,\n"
            "/seg-1.ts\n"
        )
        info = parse_playlist_details(text)
        self.assertEqual(info.kind, "segment")
        self.assertEqual(info.uri, "/seg-1.ts")
        self.assertEqual(info.bitrate_kbps, 2800.0)
        self.assertAlmostEqual(info.segment_seconds, 6.006, places=3)

    def test_parse_playlist_info_stays_backwards_compatible(self):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nv.m3u8\n"
        info = parse_playlist_details(text)
        self.assertEqual(
            (info.kind, info.uri, info.quality),
            ("variant", "v.m3u8", ""),
        )


class _ShapeClient:
    """按预设到达节奏返回分片，用于验证测量真的能区分"稳"和"忽快忽慢"。"""

    def fetch(self, url, *, max_bytes, timeout=None, range_bytes=True, headers=None):
        from itvpipe.probe import FetchResult

        spec = BURSTY_SPEC if "bursty" in url else STEADY_SPEC
        samples, first_at, last_at, total = _pattern(spec)
        data = b"\x47" * total
        return FetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="video/mp2t",
            data=data,
            start=100.0,
            first_at=first_at,
            last_at=last_at,
            samples=samples,
            content_length=total,
        )


class ProbeShapeTest(unittest.TestCase):
    def setUp(self):
        from itvpipe.config import ProbeConfig

        self.cfg = ProbeConfig(
            sample_bytes=1024 * 1024,
            min_sample_bytes=16384,
            sample_seconds=0.0,
            timeout=5.0,
            max_seconds=10.0,
            retries=0,
        )

    def _probe(self, path):
        return probe_one(f"http://fake/{path}", "CCTV1", self.cfg, _ShapeClient())

    def test_measurements_flag_bursty_source(self):
        steady = self._probe("steady.ts")
        bursty = self._probe("bursty.ts")

        self.assertTrue(steady.ok, steady.error)
        self.assertTrue(bursty.ok, bursty.error)
        # 突发源的平均速度更高，但抖动/停顿更差
        self.assertGreater(bursty.kbps, steady.kbps)
        self.assertGreater(bursty.stalls, steady.stalls)
        self.assertGreater(bursty.jitter, steady.jitter)
        self.assertLess(bursty.kbps_p50, bursty.kbps)
        self.assertGreater(steady.smooth_score(), bursty.smooth_score())

    def test_ranking_uses_smoothness_not_burst_speed(self):
        steady = self._probe("steady.ts")
        bursty = self._probe("bursty.ts")
        ranked = rank_channel_sources([bursty, steady], keep=2)
        self.assertEqual([item.url for item in ranked["CCTV1"]], [steady.url, bursty.url])


class StateRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ovo-state-"))
        self.cfg = load_config(str(self.root / "config.toml"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _add_record(self, state, url="http://a/x.m3u8"):
        return state.upsert_source(
            channel="CCTV1",
            url=url,
            canonical=canonical_url(url),
            group="央视频道",
            logo="",
            tvg_id="",
            source="test",
            priority=0,
            now=1000.0,
        )

    def test_probe_metrics_survive_save_and_load(self):
        from itvpipe.pipeline import apply_probe_results

        state = StateManager(self.cfg)
        record = self._add_record(state)
        result = ProbeResult(
            url=record.url,
            ok=True,
            kbps=3000.0,
            kbps_p50=2800.0,
            kbps_p25=1900.0,
            jitter=0.21,
            stalls=1,
            samples=18,
            content_length=1024 * 1024,
            segment_seconds=4.0,
            variant_kbps=4000.0,
            latency_ms=120.0,
            quality="1080p",
        )
        apply_probe_results(state, {record.canonical_url: result}, self.cfg, now=1000.0)
        self.assertGreater(record.success_ewma, 0.7)
        score = record.smooth_score()
        state.save(now=1000.0)

        reloaded = StateManager(self.cfg)
        again = reloaded.library["CCTV1"][record.canonical_url]
        self.assertEqual(again.kbps_p50, 2800.0)
        self.assertEqual(again.kbps_p25, 1900.0)
        self.assertEqual(again.stalls, 1)
        self.assertEqual(again.samples, 18)
        self.assertEqual(again.variant_kbps, 4000.0)
        self.assertEqual(again.content_length, 1024 * 1024)
        self.assertAlmostEqual(again.jitter, 0.21, places=4)
        # 评分由原始指标重算，权重调整后无需重新测速
        self.assertAlmostEqual(again.smooth_score(), score, places=4)

    def test_legacy_state_without_new_fields_still_loads(self):
        import json

        legend = {
            "version": 1,
            "channels": {
                "CCTV1": {
                    "http://a/x.m3u8": {
                        "url": "http://a/x.m3u8",
                        "canonical_url": "http://a/x.m3u8",
                        "status": "ok",
                        "kbps": 2600,
                        "latency_ms": 120,
                        "quality": "",
                        "checked_at": 1000,
                        "last_ok_at": 1000,
                    }
                }
            },
        }
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "library.json").write_text(
            json.dumps(legend), encoding="utf-8"
        )
        state = StateManager(self.cfg)
        record = state.library["CCTV1"]["http://a/x.m3u8"]
        self.assertIsNone(record.kbps_p50)
        self.assertEqual(record.stalls, 0)
        self.assertGreater(record.smooth_score(), 0.0)

    def test_failures_lower_reliability_more_than_successes_raise_it(self):
        from itvpipe.pipeline import apply_probe_results

        state = StateManager(self.cfg)
        record = self._add_record(state)
        ok = ProbeResult(url=record.url, ok=True, kbps=3000.0, latency_ms=100.0)
        bad = ProbeResult(url=record.url, ok=False, error="HTTP 500")

        for _ in range(2):
            apply_probe_results(state, {record.canonical_url: ok}, self.cfg, now=1000.0)
        after_success = record.success_ewma
        self.assertGreater(after_success, 0.85)

        apply_probe_results(state, {record.canonical_url: bad}, self.cfg, now=1001.0)
        self.assertLess(record.success_ewma, after_success - 0.2)
        self.assertIsNone(record.kbps_p50)


    def test_retried_probe_counts_as_half_success(self):
        from itvpipe.pipeline import apply_probe_results

        state = StateManager(self.cfg)
        solid = self._add_record(state, "http://a/solid.ts")
        flaky = self._add_record(state, "http://a/flaky.ts")
        for _ in range(2):
            apply_probe_results(
                state,
                {
                    solid.canonical_url: ProbeResult(url=solid.url, ok=True, kbps=3000.0),
                    flaky.canonical_url: ProbeResult(
                        url=flaky.url, ok=True, kbps=3000.0, retries=1
                    ),
                },
                self.cfg,
                now=1000.0,
            )
        self.assertGreater(solid.success_ewma, flaky.success_ewma)
        self.assertGreater(solid.smooth_score(), flaky.smooth_score())


class RankingConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = RankingConfig()
        self.assertEqual(cfg.mode, "smoothness")
        self.assertAlmostEqual(sum(cfg.weights().normalized()), 1.0, places=6)

    def test_config_section_is_parsed(self):
        root = Path(tempfile.mkdtemp(prefix="ovo-ranking-"))
        path = root / "config.toml"
        path.write_text(
            '[ranking]\nmode = "smoothness"\nw_speed = 0.5\nw_stall = 0.5\n'
            'w_stability = 0\nw_latency = 0\nw_reliability = 0\n'
            'min_smooth_score = 55\nrequired_kbps = { "1080p" = 6000 }\n',
            encoding="utf-8",
        )
        cfg = load_config(str(path))
        self.assertEqual(cfg.ranking.min_smooth_score, 55.0)
        self.assertEqual(cfg.ranking.requirements()["1080p"], 6000.0)
        weights = cfg.ranking.weights().normalized()
        self.assertAlmostEqual(weights[0], 0.5)
        self.assertAlmostEqual(weights[2], 0.5)

    def test_invalid_mode_is_rejected(self):
        root = Path(tempfile.mkdtemp(prefix="ovo-ranking-"))
        path = root / "config.toml"
        path.write_text('[ranking]\nmode = "random"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(str(path))


if __name__ == "__main__":
    unittest.main()

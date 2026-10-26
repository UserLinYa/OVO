"""Pipeline/state/output tests using local fixtures (no external network)."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itvpipe.config import load_config
from itvpipe.models import ChannelSpec, ProbeResult, Stats
from itvpipe.normalize import canonical_url
from itvpipe.pipeline import (
    _ensure_channel_specs,
    apply_probe_results,
    build_library,
    build_probe_plan,
    canonicalize_sources,
    load_demo,
    load_local_sources,
    run_pipeline,
    select_channels,
)
from itvpipe.state import StateManager


class Workspace:
    def __init__(self, name: str):
        self.root = Path(__file__).resolve().parent / "_tmp" / name
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def config(self, extra: str = "") -> "object":
        self.write(
            "config.toml",
            """
[app]
demo_file = "Demo.txt"
state_dir = "state"
output_dir = "dist"
legacy_txt_file = "itvlist.txt"
[output]
formats = ["m3u", "txt"]
per_group = true
max_sources_per_channel = 2
[probe]
enabled = false
[sources]
files = ["sources"]
[sources.zubo]
enabled = false
[logo]
enabled = false
"""
            + extra,
        )
        return load_config(str(self.root / "config.toml"))

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self.id().split(".")[-1])

    def tearDown(self):
        self.ws.cleanup()

    def _demo(self):
        self.ws.write(
            "Demo.txt",
            "CCTV1 | CCTV-1;中央一套 | 央视频道\n"
            "湖南卫视 | 湖南卫视高清 | 卫视频道\n",
        )

    def _sources(self, body: str):
        self.ws.write("sources/list.m3u", body)

    def test_canonicalize_filters_demo_aliases_and_dedupes(self):
        self._demo()
        self._sources(
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV-1\nhttp://a.example/cctv1.m3u8\n"
            "#EXTINF:-1,中央一套\nhttp://a.example/cctv1.m3u8\n"
            "#EXTINF:-1,CCTV1\nhttp://b.example/cctv1.m3u8\n"
            "#EXTINF:-1,湖南卫视高清\nhttp://a.example/hunan.m3u8\n"
            "#EXTINF:-1,CCTV2\nhttp://a.example/cctv2.m3u8\n"
        )
        cfg = self.ws.config()
        stats = Stats()
        demo = load_demo(cfg)
        bundle = load_local_sources(cfg)
        specs = _ensure_channel_specs(demo, cfg, stats=stats)
        sources = canonicalize_sources(bundle, cfg, specs, stats=stats, now=1000)
        self.assertEqual(set(specs), {"CCTV1", "湖南卫视"})
        self.assertEqual(len(sources), 3)
        self.assertEqual(stats.filtered_sources, 1)
        self.assertEqual(stats.duplicate_sources, 1)
        self.assertEqual({s.name for s in sources}, {"CCTV1", "湖南卫视"})
        self.assertEqual(len([s for s in sources if s.name == "CCTV1"]), 2)

    def test_plan_uses_cache_and_global_budget(self):
        self._demo()
        cfg = self.ws.config()
        cfg.probe.max_probes_per_run = 10
        now = 1000.0
        state = StateManager(cfg)
        specs = {
            "CCTV1": ChannelSpec("CCTV1", group="央视频道", order=0),
            "湖南卫视": ChannelSpec("湖南卫视", group="卫视频道", order=1),
        }
        for channel, url in (
            ("CCTV1", "http://a.example/1.m3u8"),
            ("CCTV1", "http://a.example/2.m3u8"),
            ("湖南卫视", "http://a.example/3.m3u8"),
        ):
            state.upsert_source(
                channel=channel,
                url=url,
                canonical=canonical_url(url),
                group=specs[channel].group,
                logo="",
                tvg_id="",
                source="test",
                priority=0,
                now=now,
            )
        # First CCTV1 source is fresh in cache; the others need probing.
        cached = state.library["CCTV1"][canonical_url("http://a.example/1.m3u8")]
        state.put_probe(
            cached.canonical_url,
            cached.url,
            ProbeResult(url=cached.url, ok=True, kbps=1200, latency_ms=30),
            now=now,
        )
        state.apply_cache(cached, now)
        plan, _, hits, _ = build_probe_plan(state, specs, cfg, now=now + 1)
        self.assertNotIn(canonical_url("http://a.example/1.m3u8"), plan)
        self.assertIn(canonical_url("http://a.example/2.m3u8"), plan)
        self.assertIn(canonical_url("http://a.example/3.m3u8"), plan)
        self.assertGreaterEqual(hits, 1)

    def test_failed_source_is_removed_from_output(self):
        self._demo()
        cfg = self.ws.config()
        now = 1000.0
        state = StateManager(cfg)
        spec = ChannelSpec("CCTV1", group="央视频道", order=0)
        specs = {"CCTV1": spec}
        url = "http://a.example/dead.m3u8"
        state.upsert_source(
            channel="CCTV1",
            url=url,
            canonical=canonical_url(url),
            group="央视频道",
            logo="",
            tvg_id="",
            source="test",
            priority=0,
            now=now,
        )
        key = canonical_url(url)
        failure = ProbeResult(url=url, ok=False, error="HTTP 404")
        apply_probe_results(state, {key: failure}, cfg, now=now)
        selected, _, _ = select_channels(state, specs, cfg, now=now + 1)
        self.assertNotIn("CCTV1", selected)
        apply_probe_results(state, {key: failure}, cfg, now=now + 2)
        record = state.library["CCTV1"][key]
        self.assertEqual(record.fail_count, 2)
        self.assertGreater(record.quarantined_until, now)
        selected, _, _ = select_channels(state, specs, cfg, now=now + 3)
        self.assertNotIn("CCTV1", selected)

    def test_selection_sorts_speed_and_limits(self):
        self._demo()
        cfg = self.ws.config()
        now = 1000.0
        state = StateManager(cfg)
        spec = ChannelSpec("CCTV1", group="央视频道", order=0)
        specs = {"CCTV1": spec}
        for index, kbps in enumerate((100, 5000, 1000)):
            url = f"http://a.example/{index}.m3u8"
            record = state.upsert_source(
                channel="CCTV1",
                url=url,
                canonical=canonical_url(url),
                group="央视频道",
                logo="",
                tvg_id="",
                source="test",
                priority=0,
                now=now,
            )
            record.status = "ok"
            record.kbps = kbps
            record.latency_ms = 100 - index
            record.checked_at = now
            record.last_ok_at = now
        selected, _, _ = select_channels(state, specs, cfg, now=now + 1)
        self.assertEqual([r.kbps for r in selected["CCTV1"]], [5000, 1000])

    def test_run_pipeline_writes_outputs_and_is_idempotent(self):
        self._demo()
        self._sources(
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV1\nhttp://a.example/cctv1.m3u8\n"
            "#EXTINF:-1,湖南卫视\nhttp://a.example/hunan.m3u8\n"
            "#EXTINF:-1,CCTV2\nhttp://a.example/cctv2.m3u8\n"
        )
        cfg = self.ws.config()
        cfg.probe.assume_ok_without_probe = True
        first = asyncio.run(run_pipeline(cfg))
        self.assertEqual(set(first.selected), {"CCTV1", "湖南卫视"})
        self.assertTrue((self.ws.root / "dist" / "playlist.m3u").is_file())
        self.assertTrue((self.ws.root / "dist" / "iptv.txt").is_file())
        self.assertTrue((self.ws.root / "itvlist.txt").is_file())
        self.assertTrue((self.ws.root / "dist" / "groups" / "央视频道.m3u").is_file())
        state_files = [
            self.ws.root / "state" / "library.json",
            self.ws.root / "state" / "run_state.json",
            self.ws.root / "state" / "output_hashes.json",
        ]
        before = {path.name: path.read_bytes() for path in state_files}
        second = asyncio.run(run_pipeline(cfg))
        self.assertNotIn(
            "dist/playlist.m3u",
            second.stats.changed_files,
        )
        self.assertNotIn(
            "dist/iptv.txt",
            second.stats.changed_files,
        )
        after = {path.name: path.read_bytes() for path in state_files}
        diff_names = [name for name in before if before[name] != after[name]]
        self.assertEqual(before, after, msg=f"changed state files: {diff_names}")
        content = (self.ws.root / "dist" / "playlist.m3u").read_text(encoding="utf-8")
        self.assertIn('tvg-id="CCTV-1"', content)
        self.assertIn('group-title="央视频道"', content)
        self.assertNotIn("CCTV2", content)

    def test_groups_filter(self):
        self._demo()
        self._sources(
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV1\nhttp://a.example/cctv1.m3u8\n"
            "#EXTINF:-1,湖南卫视\nhttp://a.example/hunan.m3u8\n"
        )
        cfg = self.ws.config()
        cfg.probe.assume_ok_without_probe = True
        cfg.filters.include_groups = ["卫视频道"]
        result = asyncio.run(run_pipeline(cfg))
        self.assertEqual(set(result.selected), {"湖南卫视"})

    def test_probe_flow_and_cache(self):
        self._demo()
        self._sources(
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV1\nhttp://a.example/cctv1.m3u8\n"
            "#EXTINF:-1,湖南卫视\nhttp://a.example/hunan.m3u8\n"
        )
        cfg = self.ws.config()
        cfg.probe.enabled = True
        calls = []

        class FakeRunner:
            def __init__(self, *args, **kwargs):
                pass

            async def probe_many(self, urls, progress=None, headers_by_url=None):
                calls.append(list(urls))
                return {
                    url: ProbeResult(
                        url=url,
                        ok=True,
                        kbps=1000.0 if "cctv1" in url else 500.0,
                        latency_ms=10.0,
                        checked_at=time.time(),
                    )
                    for url in urls
                }

            def close(self):
                pass

        with patch("itvpipe.pipeline.ProbeRunner", FakeRunner):
            first = asyncio.run(run_pipeline(cfg))
            self.assertEqual(first.stats.probes_run, 2)
            self.assertEqual(set(first.selected), {"CCTV1", "湖南卫视"})
            self.assertEqual(len(calls), 1)
            second = asyncio.run(run_pipeline(cfg))
            self.assertEqual(second.stats.probes_run, 0)
            self.assertEqual(len(calls), 1)
            self.assertGreaterEqual(second.stats.cache_hits, 2)


if __name__ == "__main__":
    unittest.main()

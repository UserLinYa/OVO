"""Local HTTP probe tests: HLS resolution, throughput and dead sources."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itvpipe.config import ProbeConfig
from itvpipe.probe import (
    FetchResult,
    ProbeRunner,
    parse_playlist,
    parse_playlist_info,
    probe_one,
    rank_channel_sources,
)

SEGMENT_SIZE = 200 * 1024
CHUNK = 16 * 1024


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes after each response; avoids keep-alive resets on flaky
    # sockets and matches the one-request-per-source probe semantics.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send_playlist(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_segment(self, slow: bool):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(SEGMENT_SIZE))
        self.end_headers()
        payload = b"\x47" * CHUNK
        remaining = SEGMENT_SIZE
        try:
            while remaining > 0:
                size = min(CHUNK, remaining)
                self.wfile.write(payload[:size])
                self.wfile.flush()
                remaining -= size
                if slow:
                    time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/master.m3u8":
            self._send_playlist(
                b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\n/media.m3u8\n"
            )
        elif path == "/media.m3u8":
            self._send_playlist(
                b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n/fast.ts\n#EXT-X-ENDLIST\n"
            )
        elif path == "/slow.m3u8":
            self._send_playlist(
                b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n/slow.ts\n#EXT-X-ENDLIST\n"
            )
        elif path in ("/fast.ts", "/slow.ts"):
            self._send_segment(slow=path == "/slow.ts")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class PlaylistParseTest(unittest.TestCase):
    def test_master_and_media(self):
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n720.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\n1080.m3u8\n"
        )
        kind, uri, quality = parse_playlist_info(master)
        self.assertEqual((kind, uri, quality), ("variant", "1080.m3u8", "1080p"))
        self.assertEqual(
            parse_playlist("#EXTM3U\n#EXTINF:2,\n/a.ts\n"), ("segment", "/a.ts")
        )

    def test_comments_only(self):
        self.assertIsNone(parse_playlist("#EXTM3U\n#EXT-X-ENDLIST\n"))


class _FakeClient:
    """Deterministic probe transport for environments with flaky sockets."""

    def fetch(self, url, *, max_bytes, timeout=None, range_bytes=True, headers=None):
        playlist_type = "application/vnd.apple.mpegurl"
        if url.endswith("/master.m3u8"):
            body = (
                b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\n"
                b"/media.m3u8\n"
            )
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                content_type=playlist_type,
                data=body,
                start=1.0,
                first_at=1.01,
                last_at=1.02,
            )
        if url.endswith("/media.m3u8"):
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                content_type=playlist_type,
                data=b"#EXTM3U\n#EXTINF:2,\n/fast.ts\n",
                start=2.0,
                first_at=2.01,
                last_at=2.02,
            )
        if url.endswith("/slow.m3u8"):
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                content_type=playlist_type,
                data=b"#EXTM3U\n#EXTINF:2,\n/slow.ts\n",
                start=2.0,
                first_at=2.01,
                last_at=2.02,
            )
        if url.endswith("/fast.ts"):
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                content_type="video/mp2t",
                data=b"\x47" * (128 * 1024),
                start=3.0,
                first_at=3.0,
                last_at=3.1,
            )
        if url.endswith("/slow.ts"):
            return FetchResult(
                url=url,
                final_url=url,
                status=200,
                content_type="video/mp2t",
                data=b"\x47" * (128 * 1024),
                start=3.0,
                first_at=3.0,
                last_at=4.0,
            )
        return FetchResult(url=url, status=404, error="HTTP 404")


class ProbeLogicTest(unittest.TestCase):
    def setUp(self):
        self.cfg = ProbeConfig(
            sample_bytes=131072,
            min_sample_bytes=16384,
            timeout=2.0,
            max_seconds=5.0,
            retries=0,
            tls_verify=False,
        )

    def test_fake_hls_fast_slow(self):
        client = _FakeClient()
        fast = probe_one("http://fake/master.m3u8", "CCTV1", self.cfg, client)
        slow = probe_one("http://fake/slow.m3u8", "CCTV1", self.cfg, client)
        self.assertTrue(fast.ok, fast.error)
        self.assertTrue(slow.ok, slow.error)
        self.assertEqual(fast.quality, "1080p")
        self.assertGreater(fast.kbps, slow.kbps)
        ranked = rank_channel_sources([slow, fast], keep=2)
        self.assertEqual([item.url for item in ranked["CCTV1"]], [fast.url, slow.url])

    def test_fake_dead(self):
        result = probe_one("http://fake/dead.ts", "CCTV1", self.cfg, _FakeClient())
        self.assertFalse(result.ok)
        self.assertIn("404", result.error)


@unittest.skipUnless(
    os.environ.get("IPTV_RUN_LOCAL_HTTP") == "1",
    "本地回环 HTTP 集成测试默认跳过；设置 IPTV_RUN_LOCAL_HTTP=1 可运行",
)
class ProbeIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _QuietServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.cfg = ProbeConfig(
            concurrency=4,
            per_host=2,
            sample_bytes=131072,
            min_sample_bytes=16384,
            timeout=5.0,
            max_seconds=8.0,
            retries=0,
            tls_verify=False,
        )

    def test_hls_probe_speed_and_quality(self):
        from itvpipe.probe import HttpClient

        client = HttpClient(self.cfg)
        fast = probe_one(f"{self.base}/master.m3u8", "CCTV1", self.cfg, client)
        slow = probe_one(f"{self.base}/slow.m3u8", "CCTV1", self.cfg, client)
        self.assertTrue(fast.ok, fast.error)
        self.assertTrue(slow.ok, slow.error)
        self.assertEqual(fast.via, "hls")
        self.assertEqual(fast.quality, "1080p")
        self.assertIsNotNone(fast.kbps)
        self.assertGreater(fast.kbps, slow.kbps)

        ranked = rank_channel_sources([slow, fast], keep=2)
        self.assertEqual([item.url for item in ranked["CCTV1"]], [fast.url, slow.url])

    def test_dead_source(self):
        from itvpipe.probe import HttpClient

        result = probe_one(f"{self.base}/dead.ts", "CCTV1", self.cfg, HttpClient(self.cfg))
        self.assertFalse(result.ok)
        self.assertIn("404", result.error)

    def test_probe_many_deduplicates(self):
        runner = ProbeRunner(self.cfg)

        async def run():
            try:
                return await runner.probe_many(
                    [f"{self.base}/master.m3u8", f"{self.base}/master.m3u8"]
                )
            finally:
                runner.close()

        results = asyncio.run(run())
        self.assertEqual(len(results), 1)
        self.assertTrue(next(iter(results.values())).ok)


if __name__ == "__main__":
    unittest.main()

"""HTTP probing: manifest resolution, measured throughput and health checks.

Only the standard library is used.  Blocking ``urllib`` calls run in a bounded
thread pool, which keeps GitHub-hosted runners dependency-free while still
allowing dozens of concurrent probes.

Besides raw throughput the probe records a *time series* of the read windows
``(timestamp, bytes)``.  From it we derive sustained (median) throughput,
throughput jitter and stall count, which feed :mod:`itvpipe.smooth` — the
smoothness ranking that replaced "fastest burst wins".
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .config import ProbeConfig
from .models import ProbeResult, quality_rank
from .smooth import CAP_KBPS, ScoreOptions, analyze_samples, bucket_score

LOG = logging.getLogger(__name__)

_IPL_RE = re.compile(r"^#EXT-X-STREAM-INF:(.*)$", re.IGNORECASE)
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
_BANDWIDTH_RE = re.compile(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", re.IGNORECASE)
_EXTINF_RE = re.compile(r"^#EXTINF:\s*([0-9.]+)", re.IGNORECASE)
_BITRATE_RE = re.compile(r"^#EXT-X-BITRATE:\s*(\d+)", re.IGNORECASE)
_CONTENT_RANGE_TOTAL_RE = re.compile(r"/\s*(\d+)\s*$")

# 每次 socket 读取的字节数。窗口越细，抖动/停顿统计越准。
READ_CHUNK = 16 * 1024


@dataclass
class FetchResult:
    url: str = ""
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    data: bytes = b""
    start: float = 0.0
    first_at: Optional[float] = None
    last_at: float = 0.0
    truncated: bool = False
    error: str = ""
    # (读取完成时刻, 本次读到字节数) —— 用于计算中位速率/抖动/停顿
    samples: List[Tuple[float, int]] = field(default_factory=list)
    # 完整对象大小（有 Range 时取 Content-Range 的总长）
    content_length: int = 0

    @property
    def elapsed(self) -> float:
        return max((self.last_at or self.start) - self.start, 0.0)


def _response_size(headers, status: int) -> int:
    """Return the full object size, honouring ``Content-Range`` on 206."""

    if headers is None:
        return 0
    try:
        if status == 206:
            match = _CONTENT_RANGE_TOTAL_RE.search(headers.get("Content-Range", "") or "")
            if match:
                return int(match.group(1))
        return max(int(headers.get("Content-Length") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _is_transient(error: str) -> bool:
    text = (error or "").lower()
    return any(
        token in text
        for token in (
            "connectionreset",
            "remotedisconnected",
            "connection aborted",
            "connectionabort",
            "incompleteread",
            "eof occurred",
        )
    )


class HttpClient:
    """Small thread-safe HTTP client with range sampling and TLS options."""

    def __init__(self, cfg: ProbeConfig):
        self.cfg = cfg
        self._local = threading.local()
        self._context = ssl.create_default_context()
        if not cfg.tls_verify:
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

    def _opener(self) -> urllib.request.OpenerDirector:
        opener = getattr(self._local, "opener", None)
        if opener is None:
            handlers = [
                urllib.request.ProxyHandler(),
                urllib.request.HTTPSHandler(context=self._context),
                urllib.request.HTTPRedirectHandler(),
            ]
            opener = urllib.request.build_opener(*handlers)
            self._local.opener = opener
        return opener

    def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout: Optional[float] = None,
        range_bytes: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> FetchResult:
        """Fetch with a few cheap retries for transient keep-alive resets."""

        result = FetchResult()
        for attempt in range(3):
            result = self._fetch_once(
                url,
                max_bytes=max_bytes,
                timeout=timeout,
                range_bytes=range_bytes,
                headers=headers,
            )
            if result.data or not _is_transient(result.error):
                return result
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
        return result

    def _fetch_once(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout: Optional[float] = None,
        range_bytes: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> FetchResult:
        result = FetchResult(url=url, final_url=url)
        started = time.perf_counter()
        result.start = started
        result.last_at = started
        timeout = timeout or self.cfg.timeout
        request_headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        request_headers.update(self.cfg.headers)
        if headers:
            request_headers.update(headers)
        if range_bytes and max_bytes > 0:
            request_headers["Range"] = f"bytes=0-{max_bytes - 1}"

        try:
            request = urllib.request.Request(url, headers=request_headers)
            response = self._opener().open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            result.status = int(exc.code)
            result.content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            result.final_url = exc.geturl() or url
            result.content_length = _response_size(exc.headers, result.status)
            result.error = f"HTTP {exc.code}"
            try:
                body = exc.read(min(max_bytes, 64 * 1024))
                result.data = body or b""
                if result.data:
                    result.first_at = time.perf_counter()
                    result.last_at = result.first_at
            except Exception:
                pass
            return result
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError, ValueError) as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.last_at = time.perf_counter()
            return result

        result.status = int(getattr(response, "status", 0) or 0)
        result.content_type = response.headers.get("Content-Type", "") if response.headers else ""
        result.final_url = response.geturl() or url
        result.content_length = _response_size(response.headers, result.status)
        chunks: List[bytes] = []
        total = 0
        # 时间预算：既要采够窗口，又不让单个源拖垮整轮。
        time_budget = max(float(getattr(self.cfg, "sample_seconds", 0.0) or 0.0), 0.0)
        try:
            while total < max_bytes:
                chunk = response.read(min(READ_CHUNK, max_bytes - total))
                if not chunk:
                    break
                now = time.perf_counter()
                if result.first_at is None:
                    result.first_at = now
                chunks.append(chunk)
                result.samples.append((now, len(chunk)))
                total += len(chunk)
                result.last_at = now
                if result.last_at - started > timeout + 1.0:
                    result.truncated = True
                    break
                if (
                    time_budget
                    and result.first_at is not None
                    and result.last_at - result.first_at >= time_budget
                ):
                    result.truncated = True
                    break
            if total >= max_bytes:
                result.truncated = True
        except (socket.timeout, TimeoutError, ssl.SSLError, OSError, ValueError, http.client.HTTPException) as exc:
            if not chunks:
                result.error = f"{type(exc).__name__}: {exc}"
            else:
                result.error = ""
        finally:
            try:
                response.close()
            except Exception:
                pass
        result.data = b"".join(chunks)
        # last_at 用"最后一个字节到达时刻"，比循环退出时刻更代表真实传输窗口。
        if result.samples:
            result.last_at = result.samples[-1][0]
        else:
            result.last_at = result.first_at or result.start
        if result.status not in (200, 206):
            result.error = result.error or f"HTTP {result.status}"
        return result


def parse_resolution(value: str) -> str:
    match = _RESOLUTION_RE.search(value or "")
    if not match:
        return ""
    height = int(match.group(2))
    if height >= 2160:
        return "2160p"
    if height >= 1440:
        return "1440p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 576:
        return "576p"
    if height >= 480:
        return "480p"
    return f"{height}p"


def _choose_variant(
    variants: List[Tuple[int, str, str]], max_variant_kbps: int
) -> Optional[Tuple[int, str, str]]:
    if not variants:
        return None
    cap = max(1, max_variant_kbps) * 1000
    under = [v for v in variants if v[0] <= cap and v[0] > 0]
    if under:
        chosen = max(under, key=lambda item: item[0])
    else:
        chosen = min(variants, key=lambda item: item[0] if item[0] > 0 else 10**12)
    return chosen


@dataclass(frozen=True)
class PlaylistInfo:
    """Parsed HLS playlist: where to measure next and how much bitrate is needed."""

    kind: str  # "variant" | "segment"
    uri: str
    quality: str = ""
    # 变体声明带宽（kbps）：播放器按这个码率消耗，实测低于它就会卡。
    bandwidth_kbps: Optional[float] = None
    # 媒体列表声明的实际码率（kbps，EXT-X-BITRATE）
    bitrate_kbps: Optional[float] = None
    # 首个分片时长（秒）：用来判断能不能在播放消耗完之前下完一个分片。
    segment_seconds: float = 0.0


def parse_playlist_details(
    content: str, max_variant_kbps: int = 12000
) -> Optional[PlaylistInfo]:
    """Parse an HLS playlist into :class:`PlaylistInfo`.

    For a master playlist the highest rendition below the configured bandwidth
    cap is chosen; otherwise the first media segment (or init map) is returned.
    """

    lines = [line.strip().lstrip("\ufeff") for line in content.splitlines() if line.strip()]
    if not lines:
        return None

    variants: List[Tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = line.split(":", 1)[1] if ":" in line else ""
        bandwidth_match = _BANDWIDTH_RE.search(attrs)
        bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
        quality = parse_resolution(attrs)
        for following in lines[index + 1:]:
            if following.startswith("#"):
                continue
            variants.append((bandwidth, following, quality))
            break

    chosen = _choose_variant(variants, max_variant_kbps)
    if chosen:
        bandwidth, uri, quality = chosen
        return PlaylistInfo(
            kind="variant",
            uri=uri,
            quality=quality,
            bandwidth_kbps=round(bandwidth / 1000.0, 1) if bandwidth > 0 else None,
        )

    bitrate_kbps: Optional[float] = None
    segment_seconds = 0.0
    for line in lines:
        match = _BITRATE_RE.match(line)
        if match:
            try:
                bitrate_kbps = max(bitrate_kbps or 0.0, float(match.group(1)))
            except ValueError:
                pass
    for line in lines:
        match = _EXTINF_RE.match(line)
        if match:
            try:
                segment_seconds = float(match.group(1))
            except ValueError:
                segment_seconds = 0.0
            break

    for line in lines:
        upper = line.upper()
        if upper.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line, re.IGNORECASE)
            if match:
                return PlaylistInfo(
                    "segment",
                    match.group(1),
                    "",
                    None,
                    bitrate_kbps,
                    segment_seconds,
                )
        if line.startswith("#"):
            continue
        return PlaylistInfo("segment", line, "", None, bitrate_kbps, segment_seconds)
    return None


def parse_playlist_info(content: str, max_variant_kbps: int = 12000) -> Optional[Tuple[str, str, str]]:
    """Backwards-compatible helper returning ``(kind, uri, quality)``."""

    info = parse_playlist_details(content, max_variant_kbps)
    if not info:
        return None
    return info.kind, info.uri, info.quality


def parse_playlist(content: str) -> Optional[Tuple[str, str]]:
    """Backwards-compatible helper returning only ``(kind, uri)``."""

    info = parse_playlist_info(content)
    if not info:
        return None
    return info[0], info[1]


def _is_playlist(fetch: FetchResult) -> bool:
    content_type = (fetch.content_type or "").lower()
    if any(token in content_type for token in ("mpegurl", "m3u8", "x-mpegurl")):
        return True
    head = fetch.data.lstrip(b"\xef\xbb\xbf \t\r\n")[:16].upper()
    return head.startswith(b"#EXTM3U")


def _looks_like_html(fetch: FetchResult) -> bool:
    content_type = (fetch.content_type or "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    head = fetch.data.lstrip()[:64].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _build_media_result(
    name: str,
    original_url: str,
    fetch: FetchResult,
    via: str,
    *,
    manifest_ttfb_ms: float = 0.0,
    quality: str = "",
    min_sample_bytes: int,
    stall_seconds: float = 0.35,
    variant_kbps: Optional[float] = None,
    segment_seconds: float = 0.0,
) -> ProbeResult:
    if fetch.error and not fetch.data:
        return ProbeResult(name=name, url=original_url, error=fetch.error, status=fetch.status)
    if fetch.status not in (200, 206):
        return ProbeResult(
            name=name,
            url=original_url,
            error=fetch.error or f"HTTP {fetch.status}",
            status=fetch.status,
        )
    if _looks_like_html(fetch):
        return ProbeResult(name=name, url=original_url, error="返回 HTML 页面", status=fetch.status)
    size = len(fetch.data)
    if size == 0:
        return ProbeResult(name=name, url=original_url, error="空响应", status=fetch.status)

    first_at = fetch.first_at if fetch.first_at is not None else fetch.last_at
    latency_ms = max((first_at - fetch.start) * 1000.0, 0.0) + manifest_ttfb_ms
    duration = max(fetch.last_at - first_at, 0.001)
    stats = analyze_samples(
        fetch.samples,
        first_at=first_at,
        last_at=fetch.last_at,
        stall_seconds=stall_seconds,
    )
    kbps: Optional[float] = None
    if size >= min_sample_bytes:
        kbps = min(size * 8 / 1000.0 / duration, CAP_KBPS)
    return ProbeResult(
        name=name,
        url=original_url,
        ok=True,
        kbps=round(kbps, 1) if kbps is not None else None,
        latency_ms=round(latency_ms, 1),
        sample_bytes=size,
        seconds=round(duration, 3),
        via=via,
        final_url=fetch.final_url,
        quality=quality,
        status=fetch.status,
        checked_at=time.time(),
        # 采样不足时不提供中位值，排序会退回 kbps（或判定为"仅可达"）。
        kbps_p50=stats.kbps_p50 if kbps is not None else None,
        kbps_p25=stats.kbps_p25 if kbps is not None else None,
        jitter=stats.jitter,
        stalls=stats.stalls,
        samples=stats.samples,
        content_length=fetch.content_length,
        segment_seconds=round(segment_seconds, 3),
        variant_kbps=variant_kbps,
    )


def _probe_level(
    client: HttpClient,
    cfg: ProbeConfig,
    name: str,
    url: str,
    *,
    depth: int = 0,
    quality: str = "",
    deadline: Optional[float] = None,
    headers: Optional[Dict[str, str]] = None,
    required_kbps: Optional[float] = None,
) -> ProbeResult:
    if depth > 3:
        return ProbeResult(name=name, url=url, error="播放列表层级过深")
    now = time.monotonic()
    if deadline is not None and now >= deadline:
        return ProbeResult(name=name, url=url, error="测速超时")

    timeout = cfg.timeout
    if deadline is not None:
        timeout = max(0.5, min(timeout, deadline - now))
    fetch = client.fetch(url, max_bytes=cfg.sample_bytes, timeout=timeout, headers=headers)
    if fetch.error and not fetch.data:
        return ProbeResult(name=name, url=url, error=fetch.error, status=fetch.status)

    if _is_playlist(fetch):
        text = fetch.data.decode("utf-8", "replace")
        info = parse_playlist_details(text, cfg.max_variant_kbps)
        if not info:
            return ProbeResult(name=name, url=url, error="无法解析播放列表", status=fetch.status)
        target = urljoin(fetch.final_url or url, info.uri)
        manifest_ttfb = (
            (fetch.first_at - fetch.start) * 1000.0 if fetch.first_at is not None else 0.0
        )
        if info.kind == "variant":
            sub = _probe_level(
                client,
                cfg,
                name,
                target,
                depth=depth + 1,
                quality=info.quality or quality,
                deadline=deadline,
                headers=headers,
                required_kbps=info.bandwidth_kbps or required_kbps,
            )
            if not sub.ok:
                return ProbeResult(name=name, url=url, error=sub.error or "子播放列表不可用", status=sub.status)
            return ProbeResult(
                name=name,
                url=url,
                ok=True,
                kbps=sub.kbps,
                latency_ms=round(sub.latency_ms + manifest_ttfb, 1),
                sample_bytes=sub.sample_bytes,
                seconds=sub.seconds,
                via="hls",
                final_url=sub.final_url,
                quality=sub.quality or info.quality or quality,
                status=sub.status,
                checked_at=time.time(),
                kbps_p50=sub.kbps_p50,
                kbps_p25=sub.kbps_p25,
                jitter=sub.jitter,
                stalls=sub.stalls,
                samples=sub.samples,
                content_length=sub.content_length,
                segment_seconds=sub.segment_seconds,
                # 声明带宽 / 媒体列表码率：播放器实际要消耗的码率。
                variant_kbps=sub.variant_kbps or info.bandwidth_kbps or required_kbps,
            )

        segment = client.fetch(target, max_bytes=cfg.sample_bytes, timeout=timeout, headers=headers)
        return _build_media_result(
            name,
            url,
            segment,
            "hls",
            manifest_ttfb_ms=manifest_ttfb,
            quality=info.quality or quality,
            min_sample_bytes=cfg.min_sample_bytes,
            stall_seconds=cfg.stall_seconds,
            variant_kbps=required_kbps or info.bitrate_kbps,
            segment_seconds=info.segment_seconds,
        )

    return _build_media_result(
        name,
        url,
        fetch,
        "direct",
        quality=quality,
        min_sample_bytes=cfg.min_sample_bytes,
        stall_seconds=cfg.stall_seconds,
        variant_kbps=required_kbps,
    )


def probe_one(
    url: str,
    name: str,
    cfg: ProbeConfig,
    client: Optional[HttpClient] = None,
    headers: Optional[Dict[str, str]] = None,
) -> ProbeResult:
    """Blocking probe of one URL; safe to run inside a worker thread."""

    client = client or HttpClient(cfg)
    started = time.monotonic()
    deadline = started + cfg.max_seconds
    result = ProbeResult(name=name, url=url)
    for attempt in range(2):
        try:
            result = _probe_level(client, cfg, name, url, deadline=deadline, headers=headers)
        except Exception as exc:  # defensive: a parser bug must not kill the run
            result = ProbeResult(name=name, url=url, error=f"{type(exc).__name__}: {exc}")
        result.retries = attempt
        if result.ok or not _is_transient(result.error) or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if result.checked_at <= 0:
        result.checked_at = time.time()
    return result


class ProbeRunner:
    """Async facade around the blocking probe implementation."""

    def __init__(self, cfg: ProbeConfig, *, client: Optional[HttpClient] = None):
        self.cfg = cfg
        self.client = client or HttpClient(cfg)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, cfg.concurrency), thread_name_prefix="probe"
        )
        self._host_limits: Dict[str, asyncio.Semaphore] = {}

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _one(
        self,
        url: str,
        name: str,
        limiter: asyncio.Semaphore,
        headers: Optional[Dict[str, str]] = None,
    ) -> ProbeResult:
        host = (urlsplit(url).hostname or "").lower()
        host_limit = self._host_limits.get(host)
        if host_limit is None:
            host_limit = asyncio.Semaphore(max(1, self.cfg.per_host))
            self._host_limits[host] = host_limit
        async with limiter:
            async with host_limit:
                result = ProbeResult(name=name, url=url)
                for attempt in range(self.cfg.retries + 1):
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        self._executor, probe_one, url, name, self.cfg, self.client, headers
                    )
                    result.retries = attempt + int(result.retries or 0)
                    if result.ok:
                        return result
                    if result.error.startswith("HTTP ") and not result.error.startswith(("HTTP 5", "HTTP 429")):
                        return result
                    if attempt < self.cfg.retries:
                        await asyncio.sleep(0.25)
                return result

    async def probe_many(
        self,
        urls: Iterable[str],
        *,
        progress: Optional[Callable[[int, int], None]] = None,
        headers_by_url: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> Dict[str, ProbeResult]:
        unique: List[str] = []
        seen = set()
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                unique.append(url)
        if not unique:
            return {}
        limiter = asyncio.Semaphore(max(1, self.cfg.concurrency))
        completed = 0
        total = len(unique)

        async def run(url: str) -> Tuple[str, ProbeResult]:
            nonlocal completed
            headers = (headers_by_url or {}).get(url)
            result = await self._one(url, "", limiter, headers)
            completed += 1
            if progress and (completed % 25 == 0 or completed == total):
                progress(completed, total)
            return url, result

        pairs = await asyncio.gather(*(run(url) for url in unique))
        return dict(pairs)


def rank_key(item: ProbeResult, options: Optional[ScoreOptions] = None) -> tuple:
    """Best first, using the smoothness score instead of raw burst speed."""

    score = item.smooth_score(options)
    bucket = options.bucket if options is not None else 1.0
    return (
        item.tier,
        -bucket_score(score, bucket),
        -round(item.effective_kbps or 0.0),
        round(item.latency_ms / 100.0),
        -quality_rank(item.quality),
    )


def dedupe_sources(pairs: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    seen = set()
    result = []
    for name, url in pairs:
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        result.append((name, url))
    return result


def group_names_by_url(pairs: Iterable[Tuple[str, str]]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for name, url in pairs:
        names = grouped.setdefault(url, [])
        if name not in names:
            names.append(name)
    return grouped


def rank_channel_sources(
    results: Iterable[ProbeResult],
    keep: int = 20,
    options: Optional[ScoreOptions] = None,
) -> Dict[str, List[ProbeResult]]:
    grouped: Dict[str, List[ProbeResult]] = {}
    for item in results:
        grouped.setdefault(item.name, []).append(item)
    ranked: Dict[str, List[ProbeResult]] = {}
    for name, items in grouped.items():
        seen = set()
        picked = []
        for item in sorted(items, key=lambda entry: (rank_key(entry, options), entry.url)):
            if not item.ok or item.url in seen:
                continue
            seen.add(item.url)
            picked.append(item)
            if len(picked) >= keep:
                break
        if picked:
            ranked[name] = picked
    return ranked


def probe_source(session, name: str, url: str, semaphore=None) -> "asyncio.Future":
    """Compatibility wrapper used by old tests/callers.

    ``session`` is accepted but ignored; the new implementation uses the
    standard library.  The argument is kept so existing code keeps working.
    """

    async def _run() -> ProbeResult:
        cfg = ProbeConfig()
        client = HttpClient(cfg)
        if semaphore is not None:
            async with semaphore:
                return await asyncio.to_thread(probe_one, url, name, cfg, client)
        return await asyncio.to_thread(probe_one, url, name, cfg, client)

    return _run()


__all__ = [
    "FetchResult",
    "HttpClient",
    "PlaylistInfo",
    "ProbeRunner",
    "dedupe_sources",
    "group_names_by_url",
    "parse_playlist",
    "parse_playlist_details",
    "parse_playlist_info",
    "parse_resolution",
    "probe_one",
    "probe_source",
    "rank_channel_sources",
    "rank_key",
]

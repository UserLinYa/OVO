"""Optional zubo/hotel JSON-API scanner.

The scanner is deliberately opt-in in ``config.toml``.  It reuses the probe
HTTP client and performs exactly one request per candidate URL (the original
script performed a health request and then re-downloaded each JSON), which
substantially reduces scan traffic.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List
from urllib.parse import urlsplit

from .config import Config
from .models import ParseBundle
from .parsers import decode_bytes, parse_json_text, parse_txt_text
from .probe import HttpClient
from .state import StateManager

LOG = logging.getLogger(__name__)


def generate_scan_urls(
    base_urls: Iterable[str],
    json_paths: Iterable[str],
    *,
    octet_start: int = 1,
    octet_end: int = 254,
    max_urls: int = 12000,
) -> List[str]:
    """Expand IP bases such as ``http://1.2.3.4:8080`` into candidate APIs."""

    generated: List[str] = []
    seen = set()
    for base in base_urls:
        base = base.strip()
        if not base or base.startswith("#"):
            continue
        try:
            parts = urlsplit(base)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        try:
            port_number = parts.port
        except ValueError:
            continue
        host = parts.hostname
        octets = host.split(".")
        if len(octets) != 4 or not all(part.isdigit() and 0 <= int(part) <= 255 for part in octets):
            continue
        prefix = ".".join(octets[:3])
        port = f":{port_number}" if port_number else ""
        for index in range(max(1, octet_start), min(255, octet_end) + 1):
            for path in json_paths:
                if not path.startswith("/"):
                    path = "/" + path
                url = f"{parts.scheme}://{prefix}.{index}{port}{path}"
                if url in seen:
                    continue
                seen.add(url)
                generated.append(url)
                if len(generated) >= max_urls:
                    return generated
    return generated


class ZuboScanner:
    def __init__(self, cfg: Config, state: StateManager, client: HttpClient):
        self.cfg = cfg
        self.state = state
        self.client = client

    async def scan(self) -> ParseBundle:
        bundle = ParseBundle()
        z = self.cfg.sources.zubo
        if not z.enabled:
            return bundle
        if not self.state.scan_is_due():
            LOG.info("酒店源扫描未到期（间隔 %.1f 小时），本轮跳过", z.interval_hours)
            return bundle

        base_fetch = self.client.fetch(
            z.ip_urls_url,
            max_bytes=2_000_000,
            timeout=self.cfg.sources.fetch_timeout,
            range_bytes=False,
        )
        if base_fetch.error or base_fetch.status not in (200, 206):
            bundle.warnings.append(f"下载酒店 IP 列表失败: {base_fetch.error or base_fetch.status}")
            LOG.warning("下载酒店 IP 列表失败: %s", base_fetch.error or base_fetch.status)
            return bundle

        base_urls = [
            line.strip()
            for line in decode_bytes(base_fetch.data).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        urls = generate_scan_urls(
            base_urls,
            z.json_paths,
            octet_start=z.octet_start,
            octet_end=z.octet_end,
            max_urls=z.max_scan_urls,
        )
        if not urls:
            bundle.warnings.append("酒店 IP 列表为空或格式无法识别")
            return bundle

        LOG.info("开始扫描酒店源 API：%d 个候选（每个只请求一次）", len(urls))
        executor = ThreadPoolExecutor(
            max_workers=max(1, z.scan_concurrency), thread_name_prefix="scan"
        )
        semaphore = asyncio.Semaphore(max(1, z.scan_concurrency))
        completed = 0
        total = len(urls)

        async def one(url: str) -> List:
            nonlocal completed
            try:
                async with semaphore:
                    loop = asyncio.get_running_loop()
                    fetched = await loop.run_in_executor(
                        executor,
                        lambda: self.client.fetch(
                            url,
                            max_bytes=1_500_000,
                            timeout=z.scan_timeout,
                            range_bytes=False,
                        ),
                    )
                completed += 1
                if completed % 100 == 0:
                    LOG.info("酒店源扫描进度 %d/%d", completed, total)
                if fetched.status not in (200, 206) or not fetched.data:
                    return []
                text = decode_bytes(fetched.data).lstrip("\ufeff \t\r\n")
                if text.startswith(("{", "[")):
                    parsed = parse_json_text(text, origin=url)
                else:
                    parsed = parse_txt_text(text, origin=url)
                for source in parsed.sources:
                    source.source = "zubo-hotel"
                    source.priority = max(source.priority, 30)
                return parsed.sources
            except Exception as exc:  # one bad API must not abort the scan
                LOG.debug("扫描 %s 失败: %s", url, exc)
                return []

        try:
            results = await asyncio.gather(*(one(url) for url in urls))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        seen = set()
        for sources in results:
            for source in sources:
                key = (source.name, source.url)
                if key in seen:
                    continue
                seen.add(key)
                bundle.sources.append(source)
        bundle.files.append("zubo-hotel")
        self.state.mark_scan()
        LOG.info("酒店源扫描完成：抓到 %d 条唯一源", len(bundle.sources))
        return bundle


async def scan_zubo(cfg: Config, state: StateManager, client: HttpClient) -> ParseBundle:
    return await ZuboScanner(cfg, state, client).scan()


__all__ = ["ZuboScanner", "generate_scan_urls", "scan_zubo"]

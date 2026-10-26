"""Shared data structures for the IPTV pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .smooth import (
    DEFAULT_OPTIONS,
    RANK_MODE_SMOOTHNESS,
    RANK_MODE_SPEED,
    RANK_MODES,
    ScoreOptions,
    bucket_score,
    smoothness_score,
)


DEFAULT_GROUP_ORDER = [
    "央视频道",
    "卫视频道",
    "地方频道",
    "港澳台",
    "体育",
    "影视",
    "新闻",
    "少儿",
    "纪录片",
    "音乐",
    "其他",
]


@dataclass
class ChannelSpec:
    """A channel from Demo.txt, optionally with aliases and metadata."""

    name: str
    aliases: List[str] = field(default_factory=list)
    group: str = ""
    logo: str = ""
    epg_id: str = ""
    order: int = 1_000_000
    enabled: bool = True
    origin: str = ""

    def merge(self, other: "ChannelSpec") -> None:
        for alias in other.aliases:
            if alias and alias not in self.aliases:
                self.aliases.append(alias)
        if not self.group and other.group:
            self.group = other.group
        if not self.logo and other.logo:
            self.logo = other.logo
        if not self.epg_id and other.epg_id:
            self.epg_id = other.epg_id
        if other.order < self.order:
            self.order = other.order


@dataclass
class RawSource:
    """A source before channel normalisation/deduplication."""

    name: str
    url: str
    group: str = ""
    logo: str = ""
    source: str = ""
    priority: int = 0
    original_name: str = ""
    epg_id: str = ""
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class ProbeResult:
    """Result of probing one URL.

    The first fields mirror the old ``ITVlist.ProbeResult`` so existing
    callers/tests keep working.
    """

    name: str = ""
    url: str = ""
    ok: bool = False
    # 整体平均码率（旧字段，报告里仍展示）
    kbps: Optional[float] = None
    latency_ms: float = 999999.0
    sample_bytes: int = 0
    seconds: float = 0.0
    via: str = ""
    error: str = ""
    final_url: str = ""
    quality: str = ""
    status: int = 0
    checked_at: float = 0.0
    # 流畅度相关的时间序列统计（见 itvpipe.smooth）
    kbps_p50: Optional[float] = None
    kbps_p25: Optional[float] = None
    jitter: float = 0.0
    stalls: int = 0
    samples: int = 0
    content_length: int = 0
    segment_seconds: float = 0.0
    variant_kbps: Optional[float] = None
    # 这次成功前重试了几次（连接被重置/读取不完整是抖动信号）
    retries: int = 0

    @property
    def tier(self) -> int:
        """0 = measured speed, 1 = reachable only, 2 = unavailable."""
        if not self.ok:
            return 2
        return 0 if self.kbps is not None else 1

    @property
    def speed_mbps(self) -> Optional[float]:
        return None if self.kbps is None else self.kbps / 1000.0

    @property
    def effective_kbps(self) -> Optional[float]:
        """排序用的有效码率：中位窗口速率优先，没有时间序列时退回平均值。"""

        return self.kbps_p50 if self.kbps_p50 is not None else self.kbps

    def smooth_score(self, options: Optional[ScoreOptions] = None) -> float:
        """单次探测的流畅度评分（不含跨轮次可靠性，用默认值兜底）。"""

        if not self.ok:
            return 0.0
        return smoothness_score(
            kbps=self.effective_kbps,
            quality=self.quality,
            jitter=self.jitter,
            stalls=self.stalls,
            ttfb_ms=self.latency_ms,
            reliability=None,
            variant_kbps=self.variant_kbps,
            segment_seconds=self.segment_seconds,
            content_length=self.content_length,
            options=options,
        )


@dataclass
class SourceRecord:
    """Persisted health/quality record for a (channel, URL) pair."""

    channel: str
    url: str
    canonical_url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    source: str = ""
    priority: int = 0
    status: str = "new"  # new | ok | failed
    kbps: Optional[float] = None
    latency_ms: float = 999999.0
    sample_bytes: int = 0
    via: str = ""
    quality: str = ""
    checked_at: float = 0.0
    last_ok_at: float = 0.0
    fail_count: int = 0
    last_error: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    quarantined_until: float = 0.0
    headers: Dict[str, str] = field(default_factory=dict)
    # 流畅度原始指标：持久化这些数值，评分本身随时可重算（改权重无需重测）。
    kbps_p50: Optional[float] = None
    kbps_p25: Optional[float] = None
    jitter: float = 0.0
    stalls: int = 0
    samples: int = 0
    content_length: int = 0
    segment_seconds: float = 0.0
    variant_kbps: Optional[float] = None
    # 跨轮次成功率的指数滑动平均，0 表示"还没跑过"，评分时用中性值。
    success_ewma: float = 0.0

    @property
    def measured(self) -> bool:
        return self.status == "ok" and self.kbps is not None

    @property
    def effective_kbps(self) -> Optional[float]:
        """有效码率：中位窗口速率优先，比一次突发峰值更能代表持续能力。"""

        return self.kbps_p50 if self.kbps_p50 is not None else self.kbps

    def smooth_score(self, options: Optional[ScoreOptions] = None) -> float:
        """0..100 的流畅度评分（速度不是越快越好，够用即满分）。"""

        if self.status != "ok":
            return 0.0
        return smoothness_score(
            kbps=self.effective_kbps,
            quality=self.quality,
            jitter=self.jitter,
            stalls=self.stalls,
            ttfb_ms=self.latency_ms,
            reliability=self.success_ewma or None,
            variant_kbps=self.variant_kbps,
            segment_seconds=self.segment_seconds,
            content_length=self.content_length,
            options=options,
        )

    def rank_key(self, options: Optional[ScoreOptions] = None) -> tuple:
        """Best first. 默认按流畅度；``options.mode == "speed"`` 保留旧的测速排序。

        评分做分桶（默认 1 分一档），让评分相近的源不会每轮互相换位，
        从而保持播放列表输出稳定。
        """

        opts = options or DEFAULT_OPTIONS
        if self.status != "ok":
            return (2, 0, 0.0, -10, -self.priority)
        if opts.mode == RANK_MODE_SPEED:
            return (
                0 if self.kbps is not None else 1,
                -(self.kbps or 0.0),
                self.latency_ms,
                -quality_rank(self.quality),
                -self.priority,
            )
        score = self.smooth_score(opts)
        tier = 0 if self.kbps is not None else 1
        return (
            tier,
            -bucket_score(score, opts.bucket),
            # 同一评分档内优先留有余量的源，其次才是延迟/清晰度/优先级。
            -round(self.effective_kbps or 0.0),
            round(self.latency_ms / 100.0),
            -quality_rank(self.quality),
            -self.priority,
        )


def quality_rank(quality: str) -> int:
    """Rough quality preference used only as a tie-breaker."""
    text = (quality or "").lower()
    for rank, needle in enumerate(
        ("2160", "1440", "1080", "720", "576", "480", "360"), start=4
    ):
        if needle in text:
            return 10 - rank
    return 0


@dataclass
class ParseBundle:
    channels: Dict[str, ChannelSpec] = field(default_factory=dict)
    sources: List[RawSource] = field(default_factory=list)
    epg_urls: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)

    def add_channel(self, spec: ChannelSpec) -> None:
        existing = self.channels.get(spec.name)
        if existing is None:
            self.channels[spec.name] = spec
        else:
            existing.merge(spec)

    def extend(self, other: "ParseBundle") -> None:
        for spec in other.channels.values():
            self.add_channel(spec)
        self.sources.extend(other.sources)
        for url in other.epg_urls:
            if url not in self.epg_urls:
                self.epg_urls.append(url)
        self.warnings.extend(other.warnings)
        for name in other.files:
            if name not in self.files:
                self.files.append(name)


@dataclass
class Stats:
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    files: int = 0
    raw_sources: int = 0
    valid_sources: int = 0
    filtered_sources: int = 0
    duplicate_sources: int = 0
    channels_known: int = 0
    channels_selected: int = 0
    sources_selected: int = 0
    probes_planned: int = 0
    probes_run: int = 0
    probe_ok: int = 0
    probe_failed: int = 0
    cache_hits: int = 0
    cache_skips: int = 0
    dropped_dead: int = 0
    groups: Dict[str, int] = field(default_factory=dict)
    changed_files: List[str] = field(default_factory=list)
    unchanged_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return round(max(0.0, end - self.started_at), 3)

    def finish(self) -> None:
        self.finished_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "elapsed_seconds": self.elapsed,
            "files": self.files,
            "raw_sources": self.raw_sources,
            "valid_sources": self.valid_sources,
            "filtered_sources": self.filtered_sources,
            "duplicate_sources": self.duplicate_sources,
            "channels_known": self.channels_known,
            "channels_selected": self.channels_selected,
            "sources_selected": self.sources_selected,
            "probes_planned": self.probes_planned,
            "probes_run": self.probes_run,
            "probe_ok": self.probe_ok,
            "probe_failed": self.probe_failed,
            "cache_hits": self.cache_hits,
            "cache_skips": self.cache_skips,
            "dropped_dead": self.dropped_dead,
            "groups": dict(sorted(self.groups.items())),
            "changed_files": list(self.changed_files),
            "unchanged_files": list(self.unchanged_files),
            "warnings": list(self.warnings),
        }


@dataclass
class RunResult:
    selected: Dict[str, List[SourceRecord]]
    stats: Stats
    updated_at: float
    manifest: Dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


__all__ = [
    "ChannelSpec",
    "DEFAULT_GROUP_ORDER",
    "ParseBundle",
    "ProbeResult",
    "RANK_MODES",
    "RANK_MODE_SMOOTHNESS",
    "RANK_MODE_SPEED",
    "RawSource",
    "RunResult",
    "SourceRecord",
    "Stats",
    "quality_rank",
]

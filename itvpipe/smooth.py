"""播放源流畅度评分（smoothness ranking）。

旧逻辑用「一次 128 KB 短突发的峰值速度」给同一频道的源排序，于是
**瞬时突发快、但抖动大/首帧慢/历史上反复失败**的源会排到第一位，
播放器默认选它，实际播放就卡顿、反复重缓冲。

这里改成多维流畅度评分（每个分量归一化到 ``0..1``）：

``speed``
    码率充足度。用**持续（中位）速率**而不是峰值，除以该源真正需要的码率：
    HLS 变体声明码率 / 清晰度对应码率 / 分片实时下载能力，三者取最严。
    超过 ``safety`` 倍需求即满分——再快也不会更流畅。

``stability``
    抖动。把读取时间轴重采样成固定桶后，桶速率的变异系数越小越好。

``stall``
    采样期内「长时间没有字节」的停顿次数，直接对应重缓冲。

``latency``
    首帧延迟（TTFB），决定换台时的等待。

``reliability``
    跨轮次的成功 EWMA。偶发失败会显著压低评分，避免抖动源靠一次侥幸测速上位。

最终：``score = 100 * Σ weight_i * component_i``，再乘「码率可行性」系数——
持续码率低于 ``min_adequacy`` 倍需求时按缺口扣分，代码率不够的源即使很平稳
也会因为必然重缓冲而排到后面。``bucket_score`` 把评分分档，避免测量噪声让
播放列表顺序每轮抖动。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# 单窗口速率上限，防止瞬时突发把统计量拉爆。
CAP_KBPS = 500_000.0

# 小于该值的间隔视为「同一批数据」：合并到下一个窗口，避免除零和天文数字。
MIN_INTERVAL_SECONDS = 0.002

# 统计抖动时把时间轴重采样成固定桶：太细会把客户端调度噪声当成抖动，
# 太粗又会漏掉停顿。桶宽按常态间隔自适应，并夹在下面范围内。
MIN_BUCKET_SECONDS = 0.1
MAX_BUCKET_SECONDS = 0.5
BUCKET_INTERVAL_FACTOR = 3.0

# 默认「该清晰度需要多少码率才不卡」。只用于没有声明码率时的兜底。
DEFAULT_REQUIRED_KBPS: Dict[str, float] = {
    "2160p": 12000.0,
    "1440p": 7000.0,
    "1080p": 4000.0,
    "720p": 2500.0,
    "576p": 1800.0,
    "480p": 1200.0,
    "360p": 800.0,
    "": 2500.0,
}

# 未测出码率（仅握手成功）的源，按这个分数上限处理，保证排在实测源之后。
REACHABLE_ONLY_SCORE = 30.0

DEFAULT_RELIABILITY = 0.7


def clamp01(value: float) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return float(value)


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = clamp01(q) * (len(sorted_values) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return float(sorted_values[low] * (1.0 - fraction) + sorted_values[high] * fraction)


def median(values: Sequence[float]) -> float:
    return _quantile(sorted(values), 0.5)


@dataclass(frozen=True)
class RankWeights:
    """流畅度分量权重；使用前会归一化，所以不必凑成 1。"""

    speed: float = 0.34
    stability: float = 0.30
    stall: float = 0.16
    latency: float = 0.08
    reliability: float = 0.12

    def normalized(self) -> Tuple[float, float, float, float, float]:
        values = [
            max(0.0, float(self.speed)),
            max(0.0, float(self.stability)),
            max(0.0, float(self.stall)),
            max(0.0, float(self.latency)),
            max(0.0, float(self.reliability)),
        ]
        total = sum(values)
        if total <= 0:
            default = RankWeights().normalized()
            return default
        return tuple(value / total for value in values)  # type: ignore[return-value]


DEFAULT_WEIGHTS = RankWeights()

RANK_MODE_SMOOTHNESS = "smoothness"
RANK_MODE_SPEED = "speed"
RANK_MODES = (RANK_MODE_SMOOTHNESS, RANK_MODE_SPEED)


@dataclass(frozen=True)
class ScoreOptions:
    """一次评分/排序的全部可调参数（来自 ``[ranking]`` 配置段）。"""

    weights: RankWeights = DEFAULT_WEIGHTS
    # 清晰度 → 需求码率；缺省时用内置表
    required: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_REQUIRED_KBPS))
    # 码率达到"需求 * safety"才算满速
    safety: float = 1.2
    # 稳定性 = 1/(1 + jitter_scale * CV)
    jitter_scale: float = 2.0
    # 码率不足"需求 * min_adequacy"开始按缺口扣分
    min_adequacy: float = 0.8
    # 完全跟不上时最多保留的分数比例
    shortfall_floor: float = 0.25
    # 排序模式：smoothness | speed
    mode: str = RANK_MODE_SMOOTHNESS
    # 评分分桶粒度，越大播放列表越稳定
    bucket: float = 1.0


DEFAULT_OPTIONS = ScoreOptions()


@dataclass
class SampleStats:
    """一次探测内部的时间序列统计。"""

    samples: int = 0
    seconds: float = 0.0
    # 时间加权中位速率（抗突发），kbps
    kbps_p50: Optional[float] = None
    # 25 分位速率（差窗口），kbps
    kbps_p25: Optional[float] = None
    # 桶速率的变异系数（标准差/均值），0 表示非常平稳
    jitter: float = 0.0
    # 采样期内的长停顿次数
    stalls: int = 0
    # 读取间隔的中位数，用于自适应桶宽
    median_interval: float = 0.0


def _bucket_rates(
    samples: Sequence[Tuple[float, int]],
    first_at: float,
    last_at: float,
    bucket: float,
) -> List[float]:
    """把读取时间轴重采样成固定时长的桶，返回每个桶的速率（kbps）。

    用累计字节曲线的线性插值取桶边界，速率按「桶内字节 / 桶实际时长」计算。
    这样既不会因为读取批次刚好跨桶而误判抖动，也能让长期低速、突发后停摆
    如实反映到分布里。
    """

    duration = last_at - first_at
    if duration <= 0 or bucket <= 0:
        return []

    times: List[float] = [first_at]
    cumulative: List[float] = [0.0]
    skipped_start = False
    for timestamp, size in samples:
        if not size or size <= 0:
            continue
        # 只跳过「首个读取」：它属于启动突发，计入第一个桶会让它虚高。
        # 注意不能按时间窗口跳过——在千兆/回环链路上每次读取只相隔几十微秒，
        # 按窗口跳过会把整段采样丢空。
        if not skipped_start and timestamp <= first_at + MIN_INTERVAL_SECONDS:
            skipped_start = True
            continue
        if timestamp < times[-1]:
            timestamp = times[-1]
        times.append(timestamp)
        cumulative.append(cumulative[-1] + int(size))

    if cumulative[-1] <= 0:
        return []

    def total_at(moment: float) -> float:
        if moment <= times[0]:
            return 0.0
        if moment >= times[-1]:
            return cumulative[-1]
        index = bisect.bisect_right(times, moment) - 1
        low_time, high_time = times[index], times[index + 1]
        low_total, high_total = cumulative[index], cumulative[index + 1]
        if high_time <= low_time:
            return high_total
        ratio = (moment - low_time) / (high_time - low_time)
        return low_total + (high_total - low_total) * ratio

    count = max(1, int(math.ceil(duration / bucket)))
    rates: List[float] = []
    for position in range(count):
        start = first_at + position * bucket
        end = min(start + bucket, last_at)
        span = end - start
        if span <= 0:
            continue
        # 丢掉末尾不足半桶的碎片，否则一个小桶会被放大成尖峰。
        if position == count - 1 and count > 1 and span < bucket * 0.5:
            break
        size = total_at(end) - total_at(start)
        rates.append(min(size * 8.0 / 1000.0 / span, CAP_KBPS))
    return rates


def analyze_samples(
    samples: Sequence[Tuple[float, int]],
    *,
    first_at: float,
    last_at: float,
    stall_seconds: float = 0.35,
    bucket_seconds: Optional[float] = None,
) -> SampleStats:
    """把「(收到时间, 本次字节数)」序列换算成持续速率/抖动/停顿统计。

    ``samples`` 来自实际 socket 读取，每个元素是 *(读取完成时刻, 本次字节数)*。
    调用方保证时间单调递增。
    """

    stats = SampleStats(seconds=max(last_at - first_at, 0.0))
    if not samples or last_at <= first_at:
        return stats

    # 1) 原始间隔 → 停顿次数。用 25 分位间隔作为"常态节奏"，慢速源不会被误判。
    intervals: List[float] = []
    previous = first_at
    for timestamp, size in samples:
        if not size or size <= 0:
            continue
        delta = timestamp - previous
        previous = timestamp
        if delta < MIN_INTERVAL_SECONDS:
            continue
        intervals.append(delta)
    if intervals:
        ordered_intervals = sorted(intervals)
        baseline = max(_quantile(ordered_intervals, 0.25), MIN_INTERVAL_SECONDS)
        threshold = max(float(stall_seconds), BUCKET_INTERVAL_FACTOR * baseline)
        stats.stalls = sum(1 for delta in intervals if delta >= threshold)

    # 2) 重采样成固定桶 → 持续速率分布与抖动。
    bucket = bucket_seconds
    if bucket is None:
        base = median(intervals) if intervals else MIN_BUCKET_SECONDS
        bucket = min(max(base * BUCKET_INTERVAL_FACTOR, MIN_BUCKET_SECONDS), MAX_BUCKET_SECONDS)
    rates = _bucket_rates(samples, first_at, last_at, bucket)
    if not rates:
        return stats

    ordered = sorted(rates)
    stats.samples = len(rates)
    stats.kbps_p50 = round(_quantile(ordered, 0.5), 1)
    stats.kbps_p25 = round(_quantile(ordered, 0.25), 1)
    stats.median_interval = median(intervals) if intervals else 0.0

    if len(rates) >= 3:
        mean = sum(rates) / len(rates)
        if mean > 0:
            variance = sum((value - mean) ** 2 for value in rates) / (len(rates) - 1)
            stats.jitter = round(math.sqrt(max(variance, 0.0)) / mean, 4)
    return stats


def required_kbps(quality: str = "", overrides: Optional[Dict[str, float]] = None) -> float:
    """清晰度 → 不卡顿所需码率（kbps）。未知清晰度用兜底值。"""

    table = dict(DEFAULT_REQUIRED_KBPS)
    for key, value in (overrides or {}).items():
        try:
            table[str(key).lower()] = float(value)
        except (TypeError, ValueError):
            continue
    text = (quality or "").lower()
    for needle in ("2160", "1440", "1080", "720", "576", "480", "360"):
        if needle in text:
            return table.get(needle + "p", table[""])
    return table.get("", 2500.0)


def adequacy_ratio(
    kbps: Optional[float],
    *,
    quality: str = "",
    variant_kbps: Optional[float] = None,
    segment_seconds: float = 0.0,
    content_length: int = 0,
    required: Optional[Dict[str, float]] = None,
) -> float:
    """实测持续码率 / 该源需要的码率。``<1`` 表示跟不上播放消耗。"""

    if not kbps or kbps <= 0 or not math.isfinite(kbps):
        return 0.0
    need = max(float(variant_kbps or 0.0), 0.0) or required_kbps(quality, required)
    ratio = (kbps / need) if need > 0 else 1.0

    # 分片能否在播放消耗完之前下完：播放器只要跟不上就会卡。
    if segment_seconds > 0 and content_length > 0:
        fetch_seconds = content_length * 8.0 / 1000.0 / kbps
        if fetch_seconds > 0:
            ratio = min(ratio, segment_seconds / fetch_seconds)
    return max(ratio, 0.0)


def speed_component(
    kbps: Optional[float],
    *,
    quality: str = "",
    variant_kbps: Optional[float] = None,
    segment_seconds: float = 0.0,
    content_length: int = 0,
    required: Optional[Dict[str, float]] = None,
    safety: float = 1.2,
) -> float:
    """码率充足度：够用即满分，越快不再加分。"""

    ratio = adequacy_ratio(
        kbps,
        quality=quality,
        variant_kbps=variant_kbps,
        segment_seconds=segment_seconds,
        content_length=content_length,
        required=required,
    )
    return clamp01(ratio / max(float(safety), 1.0))


def stability_component(jitter: float, scale: float = 2.0) -> float:
    """1/(1+scale*CV)：越平稳越接近 1。"""

    if not jitter or jitter <= 0:
        return 1.0
    return 1.0 / (1.0 + max(float(scale), 0.0) * float(jitter))


def stall_component(stalls: int) -> float:
    if not stalls or stalls <= 0:
        return 1.0
    return 1.0 / (1.0 + float(stalls))


def latency_component(ttfb_ms: float) -> float:
    if not ttfb_ms or ttfb_ms <= 0:
        return 1.0
    return 1.0 / (1.0 + float(ttfb_ms) / 1000.0)


def reliability_component(reliability: Optional[float]) -> float:
    if reliability is None or reliability <= 0:
        return DEFAULT_RELIABILITY
    return clamp01(reliability)


def smoothness_score(
    *,
    kbps: Optional[float],
    quality: str = "",
    jitter: float = 0.0,
    stalls: int = 0,
    ttfb_ms: float = 0.0,
    reliability: Optional[float] = None,
    variant_kbps: Optional[float] = None,
    segment_seconds: float = 0.0,
    content_length: int = 0,
    options: Optional[ScoreOptions] = None,
    measured: Optional[bool] = None,
) -> float:
    """计算 0..100 的流畅度评分；``kbps`` 为空表示只握手成功。

    码率不足时整段评分会被"可行性"系数压低：不够码率的源再平稳也一定会
    重缓冲，所以不能只靠加权和把它的分数留在中上游。
    """

    opts = options or DEFAULT_OPTIONS
    w_speed, w_stability, w_stall, w_latency, w_reliability = opts.weights.normalized()
    measured = (kbps is not None) if measured is None else bool(measured)

    ratio = (
        adequacy_ratio(
            kbps,
            quality=quality,
            variant_kbps=variant_kbps,
            segment_seconds=segment_seconds,
            content_length=content_length,
            required=opts.required,
        )
        if measured
        else 0.0
    )
    speed = clamp01(ratio / max(float(opts.safety), 1.0))
    total = (
        w_speed * speed
        + w_stability * stability_component(jitter, opts.jitter_scale)
        + w_stall * stall_component(stalls)
        + w_latency * latency_component(ttfb_ms)
        + w_reliability * reliability_component(reliability)
    )
    score = 100.0 * clamp01(total)

    # 码率不足按缺口线性扣分：到 min_adequacy 倍需求才开始罚，
    # 完全跟不上时最多保留 shortfall_floor。
    floor = clamp01(opts.shortfall_floor)
    feasible = clamp01(ratio / max(float(opts.min_adequacy), 1e-6))
    score *= floor + (1.0 - floor) * feasible

    if not measured:
        # 没有实测码率的源无法证明流畅，分数封顶，永远排在实测源之后。
        score = min(score, REACHABLE_ONLY_SCORE)
    return round(clamp01(score / 100.0) * 100.0, 2)


def bucket_score(score: float, bucket: float = 1.0) -> int:
    """把评分分桶，作为排序主键；粒度越大，播放列表越稳定。"""

    size = float(bucket or 1.0)
    if size <= 0:
        size = 1.0
    return int(round(float(score) / size))


__all__ = [
    "CAP_KBPS",
    "DEFAULT_OPTIONS",
    "DEFAULT_RELIABILITY",
    "DEFAULT_REQUIRED_KBPS",
    "DEFAULT_WEIGHTS",
    "MIN_INTERVAL_SECONDS",
    "RANK_MODE_SMOOTHNESS",
    "RANK_MODE_SPEED",
    "RANK_MODES",
    "REACHABLE_ONLY_SCORE",
    "RankWeights",
    "SampleStats",
    "ScoreOptions",
    "adequacy_ratio",
    "analyze_samples",
    "bucket_score",
    "clamp01",
    "latency_component",
    "median",
    "reliability_component",
    "required_kbps",
    "smoothness_score",
    "speed_component",
    "stability_component",
    "stall_component",
]

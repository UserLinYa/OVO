"""Configuration loading and validation.

The file format is TOML because Python 3.11+ ships ``tomllib``; no YAML
dependency is required in GitHub Actions.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .models import DEFAULT_GROUP_ORDER, RANK_MODES
from .smooth import DEFAULT_REQUIRED_KBPS, RankWeights, ScoreOptions

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class AppConfig:
    timezone: str = "Asia/Shanghai"
    demo_file: str = "Demo.txt"
    state_dir: str = "state"
    output_dir: str = "dist"
    report_file: str = "speed_report.txt"
    legacy_txt_file: str = "itvlist.txt"
    log_level: str = "INFO"


@dataclass
class FiltersConfig:
    require_demo: bool = True
    allow_unknown: bool = False
    include_groups: List[str] = field(default_factory=list)
    exclude_name_keywords: List[str] = field(default_factory=list)
    exclude_url_keywords: List[str] = field(
        default_factory=lambda: ["/paiptv/", "/00/SNM/", "/00/CHANNEL"]
    )
    allowed_schemes: List[str] = field(default_factory=lambda: ["http", "https"])
    max_url_length: int = 4096


@dataclass
class LogoConfig:
    enabled: bool = True
    template: str = "https://www.xn--rgv465a.top/tvlogo/{id}.png"
    overrides: Dict[str, str] = field(default_factory=dict)


@dataclass
class OutputConfig:
    formats: List[str] = field(default_factory=lambda: ["m3u", "txt"])
    per_group: bool = True
    per_group_formats: List[str] = field(default_factory=lambda: ["m3u"])
    group_dir: str = "groups"
    max_sources_per_channel: int = 3
    min_sources_per_channel: int = 1
    playlist_name: str = "playlist.m3u"
    txt_name: str = "iptv.txt"
    legacy_txt: bool = True
    epg_url: str = ""
    tvg_id_style: str = "dash"  # dash | name
    extgrp: bool = False
    group_order: List[str] = field(default_factory=lambda: list(DEFAULT_GROUP_ORDER))
    include_empty_groups: bool = False
    header_comment: str = ""
    dedupe_urls_across_channels: bool = False
    include_vlc_options: bool = True
    append_update_entry: bool = True
    update_entry_url: str = "http://123.MP4"
    write_manifest: bool = True
    clean_stale_group_files: bool = True


@dataclass
class ProbeConfig:
    enabled: bool = True
    concurrency: int = 32
    per_host: int = 6
    # 采样窗口：读满 sample_bytes 或采够 sample_seconds，先到先停。
    # 384 KB 拆成 16 KB 窗口 ≈ 24 个样本，足够算出中位速率、抖动和停顿。
    sample_bytes: int = 384 * 1024
    sample_seconds: float = 3.0
    min_sample_bytes: int = 16 * 1024
    timeout: float = 5.0
    connect_timeout: float = 3.0
    max_seconds: float = 14.0
    retries: int = 1
    tls_verify: bool = False
    success_ttl_hours: float = 18.0
    failure_ttl_minutes: float = 30.0
    stale_after_hours: float = 96.0
    retain_hours: float = 168.0
    drop_after_failures: int = 2
    quarantine_hours: float = 24.0
    max_candidates_per_channel: int = 10
    max_new_per_channel: int = 3
    max_probes_per_run: int = 500
    max_variant_kbps: int = 12000
    # 超过该时长没有字节到达，且明显长于常态间隔，计为一次停顿（重缓冲信号）。
    stall_seconds: float = 0.35
    user_agent: str = DEFAULT_USER_AGENT
    headers: Dict[str, str] = field(default_factory=dict)
    force: bool = False
    assume_ok_without_probe: bool = False


@dataclass
class RankingConfig:
    """播放源排序策略：默认按"流畅度"而不是"测速最快"。"""

    # smoothness = 流畅度评分；speed = 旧行为（实测速度降序）
    mode: str = "smoothness"
    w_speed: float = 0.34
    w_stability: float = 0.30
    w_stall: float = 0.16
    w_latency: float = 0.08
    w_reliability: float = 0.12
    # 码率达到"需求 * speed_safety"即满分；再快也不会更流畅。
    speed_safety: float = 1.2
    # 稳定性 = 1/(1 + jitter_scale * CV)
    jitter_scale: float = 2.0
    # 码率不足"需求 * min_adequacy"开始按缺口扣分，完全跟不上时保留
    # shortfall_floor 比例（不够码率的源再稳也一定会重缓冲）。
    min_adequacy: float = 0.8
    shortfall_floor: float = 0.25
    # 排序时的分数分桶粒度：越大越不容易每轮换位（输出更稳定）。
    score_bucket: float = 1.0
    # 流畅度低于该分数的源不再输出；0 表示不淘汰。
    min_smooth_score: float = 0.0
    # 清晰度 → 不卡顿所需码率(kbps)，会与内置表合并
    required_kbps: Dict[str, float] = field(default_factory=dict)

    def weights(self) -> RankWeights:
        return RankWeights(
            speed=self.w_speed,
            stability=self.w_stability,
            stall=self.w_stall,
            latency=self.w_latency,
            reliability=self.w_reliability,
        )

    def requirements(self) -> Dict[str, float]:
        table = dict(DEFAULT_REQUIRED_KBPS)
        table.update({str(key).lower(): float(value) for key, value in self.required_kbps.items()})
        return table

    def options(self) -> ScoreOptions:
        """打包成评分函数需要的参数对象。"""

        return ScoreOptions(
            weights=self.weights(),
            required=self.requirements(),
            safety=self.speed_safety,
            jitter_scale=self.jitter_scale,
            min_adequacy=self.min_adequacy,
            shortfall_floor=self.shortfall_floor,
            mode=self.mode,
            bucket=self.score_bucket,
        )


@dataclass
class ZuboConfig:
    enabled: bool = False
    ip_urls_url: str = "https://raw.githubusercontent.com/kakaxi-1/zubo/main/ip_urls.txt"
    json_paths: List[str] = field(
        default_factory=lambda: [
            "/iptv/live/1000.json?key=txiptv",
            "/iptv/live/1001.json?key=txiptv",
            "/ZHGXTV/Public/json/live_interface.txt",
        ]
    )
    octet_start: int = 1
    octet_end: int = 254
    scan_concurrency: int = 120
    scan_timeout: float = 1.5
    max_scan_urls: int = 12000
    interval_hours: float = 6.0


@dataclass
class SourcesConfig:
    files: List[str] = field(default_factory=lambda: ["sources"])
    playlists: List[str] = field(default_factory=list)
    fetch_timeout: float = 15.0
    max_bytes: int = 5_000_000
    zubo: ZuboConfig = field(default_factory=ZuboConfig)


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    filters: FiltersConfig = field(default_factory=FiltersConfig)
    logo: LogoConfig = field(default_factory=LogoConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    base_dir: Path = field(default_factory=Path.cwd)
    config_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)
    offline: bool = False
    dry_run: bool = False

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def demo_path(self) -> Path:
        return self.path(self.app.demo_file)

    @property
    def state_dir(self) -> Path:
        return self.path(self.app.state_dir)

    @property
    def output_dir(self) -> Path:
        return self.path(self.app.output_dir)

    def validate(self) -> None:
        if not self.app.demo_file.strip():
            raise ValueError("[app].demo_file 不能为空")
        if not self.app.state_dir.strip() or not self.app.output_dir.strip():
            raise ValueError("[app].state_dir / output_dir 不能为空")
        if not self.sources.files and not self.sources.playlists and not self.sources.zubo.enabled:
            self.warnings.append("未配置文件/远程播放列表/酒店扫描，输出可能为空")
        if self.probe.concurrency < 1 or self.probe.concurrency > 256:
            raise ValueError("[probe].concurrency 必须在 1..256")
        if self.probe.per_host < 1 or self.probe.per_host > 64:
            raise ValueError("[probe].per_host 必须在 1..64")
        if self.probe.sample_bytes < 1024:
            raise ValueError("[probe].sample_bytes 不能小于 1024")
        if self.probe.sample_seconds < 0:
            raise ValueError("[probe].sample_seconds 不能为负数")
        if self.probe.stall_seconds <= 0:
            raise ValueError("[probe].stall_seconds 必须大于 0")
        if self.probe.min_sample_bytes < 0:
            raise ValueError("[probe].min_sample_bytes 不能为负数")
        if self.probe.timeout <= 0 or self.probe.max_seconds <= 0:
            raise ValueError("[probe].timeout / max_seconds 必须大于 0")
        if self.probe.drop_after_failures < 1:
            raise ValueError("[probe].drop_after_failures 必须 >= 1")
        if self.probe.max_candidates_per_channel < 1:
            raise ValueError("[probe].max_candidates_per_channel 必须 >= 1")
        if self.probe.max_new_per_channel < 0:
            raise ValueError("[probe].max_new_per_channel 不能为负数")
        if self.probe.max_probes_per_run < 1:
            raise ValueError("[probe].max_probes_per_run 必须 >= 1")
        if self.output.max_sources_per_channel < 1:
            raise ValueError("[output].max_sources_per_channel 必须 >= 1")
        if self.output.min_sources_per_channel < 0:
            raise ValueError("[output].min_sources_per_channel 不能为负数")
        if self.output.max_sources_per_channel < self.output.min_sources_per_channel:
            raise ValueError("[output] min_sources_per_channel 不能大于 max_sources_per_channel")
        for fmt in self.output.formats:
            if fmt not in ("m3u", "txt"):
                raise ValueError(f"不支持的输出格式: {fmt}")
        for fmt in self.output.per_group_formats:
            if fmt not in ("m3u", "txt"):
                raise ValueError(f"不支持的分组输出格式: {fmt}")
        if self.output.tvg_id_style not in ("dash", "name"):
            raise ValueError("[output].tvg_id_style 只能是 dash 或 name")
        if self.ranking.mode not in RANK_MODES:
            raise ValueError("[ranking].mode 只能是 smoothness 或 speed")
        weights = self.ranking.weights()
        if sum(weights.normalized()) <= 0:
            raise ValueError("[ranking] 权重之和必须大于 0")
        for name in ("w_speed", "w_stability", "w_stall", "w_latency", "w_reliability"):
            if getattr(self.ranking, name) < 0:
                raise ValueError(f"[ranking].{name} 不能为负数")
        if self.ranking.speed_safety <= 0:
            raise ValueError("[ranking].speed_safety 必须大于 0")
        if self.ranking.jitter_scale < 0:
            raise ValueError("[ranking].jitter_scale 不能为负数")
        if not 0 < self.ranking.min_adequacy:
            raise ValueError("[ranking].min_adequacy 必须大于 0")
        if not 0 <= self.ranking.shortfall_floor <= 1:
            raise ValueError("[ranking].shortfall_floor 必须在 0..1")
        if self.ranking.score_bucket <= 0:
            raise ValueError("[ranking].score_bucket 必须大于 0")
        if self.ranking.min_smooth_score < 0:
            raise ValueError("[ranking].min_smooth_score 不能为负数")
        for quality, kbps in self.ranking.required_kbps.items():
            if not isinstance(kbps, (int, float)) or kbps <= 0:
                raise ValueError(f"[ranking].required_kbps[{quality}] 必须是正数")
        for scheme in self.filters.allowed_schemes:
            if scheme not in ("http", "https"):
                raise ValueError("目前只支持 http/https 播放源")
        if not self.output.group_order:
            self.output.group_order = list(DEFAULT_GROUP_ORDER)
        # 分组顺序去重，缺省分组自动补到最后。
        seen = set()
        order = []
        for group in self.output.group_order:
            if group and group not in seen:
                seen.add(group)
                order.append(group)
        for group in DEFAULT_GROUP_ORDER:
            if group not in seen:
                seen.add(group)
                order.append(group)
        self.output.group_order = order

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "config": str(self.config_path or ""),
            "demo": str(self.demo_path),
            "output_dir": str(self.output_dir),
            "state_dir": str(self.state_dir),
            "probe": {
                "enabled": self.probe.enabled,
                "success_ttl_hours": self.probe.success_ttl_hours,
                "max_probes_per_run": self.probe.max_probes_per_run,
                "max_candidates_per_channel": self.probe.max_candidates_per_channel,
            },
            "output": {
                "formats": self.output.formats,
                "max_sources_per_channel": self.output.max_sources_per_channel,
                "per_group": self.output.per_group,
            },
            "ranking": {
                "mode": self.ranking.mode,
                "score_bucket": self.ranking.score_bucket,
                "min_smooth_score": self.ranking.min_smooth_score,
                "weights": {
                    "speed": self.ranking.w_speed,
                    "stability": self.ranking.w_stability,
                    "stall": self.ranking.w_stall,
                    "latency": self.ranking.w_latency,
                    "reliability": self.ranking.w_reliability,
                },
            },
            "sources": {
                "files": self.sources.files,
                "playlists": len(self.sources.playlists),
                "zubo": self.sources.zubo.enabled,
            },
        }


def _section(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"配置段 [{key}] 必须是表（table）")
    return value


def _build(cls, data: Dict[str, Any], section: str = "") -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    kwargs = {key: copy.deepcopy(value) for key, value in data.items() if key in known}
    obj = cls(**kwargs)
    if unknown:
        label = f"[{section}]." if section else ""
        for key in sorted(unknown):
            setattr(obj, "_unknown_" + key, True)
    return obj


def _coerce_config_types(cfg: Config) -> None:
    """TOML types are already sane, but strings are common for numbers."""

    def as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def as_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def as_list(value: Any, default: List[str]) -> List[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return list(default)

    p = cfg.probe
    p.concurrency = as_int(p.concurrency, 32)
    p.per_host = as_int(p.per_host, 6)
    p.sample_bytes = as_int(p.sample_bytes, 384 * 1024)
    p.sample_seconds = as_float(p.sample_seconds, 3.0)
    p.min_sample_bytes = as_int(p.min_sample_bytes, 16 * 1024)
    p.timeout = as_float(p.timeout, 5.0)
    p.connect_timeout = as_float(p.connect_timeout, 3.0)
    p.max_seconds = as_float(p.max_seconds, 14.0)
    p.retries = as_int(p.retries, 1)
    p.success_ttl_hours = as_float(p.success_ttl_hours, 18.0)
    p.failure_ttl_minutes = as_float(p.failure_ttl_minutes, 30.0)
    p.stale_after_hours = as_float(p.stale_after_hours, 96.0)
    p.retain_hours = as_float(p.retain_hours, 168.0)
    p.drop_after_failures = as_int(p.drop_after_failures, 2)
    p.quarantine_hours = as_float(p.quarantine_hours, 24.0)
    p.max_candidates_per_channel = as_int(p.max_candidates_per_channel, 10)
    p.max_new_per_channel = as_int(p.max_new_per_channel, 3)
    p.max_probes_per_run = as_int(p.max_probes_per_run, 500)
    p.max_variant_kbps = as_int(p.max_variant_kbps, 12000)
    p.stall_seconds = as_float(p.stall_seconds, 0.35)

    r = cfg.ranking
    r.mode = str(r.mode or "smoothness").strip().lower()
    r.w_speed = as_float(r.w_speed, 0.34)
    r.w_stability = as_float(r.w_stability, 0.30)
    r.w_stall = as_float(r.w_stall, 0.16)
    r.w_latency = as_float(r.w_latency, 0.08)
    r.w_reliability = as_float(r.w_reliability, 0.12)
    r.speed_safety = as_float(r.speed_safety, 1.2)
    r.jitter_scale = as_float(r.jitter_scale, 2.0)
    r.min_adequacy = as_float(r.min_adequacy, 0.8)
    r.shortfall_floor = as_float(r.shortfall_floor, 0.25)
    r.score_bucket = as_float(r.score_bucket, 1.0)
    r.min_smooth_score = as_float(r.min_smooth_score, 0.0)
    if not isinstance(r.required_kbps, dict):
        r.required_kbps = {}
    else:
        coerced: Dict[str, float] = {}
        for key, value in r.required_kbps.items():
            try:
                coerced[str(key).lower()] = float(value)
            except (TypeError, ValueError):
                continue
        r.required_kbps = coerced

    o = cfg.output
    o.max_sources_per_channel = as_int(o.max_sources_per_channel, 3)
    o.min_sources_per_channel = as_int(o.min_sources_per_channel, 1)
    o.formats = as_list(o.formats, ["m3u", "txt"])
    o.per_group_formats = as_list(o.per_group_formats, ["m3u"])
    o.group_order = as_list(o.group_order, list(DEFAULT_GROUP_ORDER))

    f = cfg.filters
    f.max_url_length = as_int(f.max_url_length, 4096)
    f.include_groups = as_list(f.include_groups, [])
    f.exclude_name_keywords = as_list(f.exclude_name_keywords, [])
    f.exclude_url_keywords = as_list(
        f.exclude_url_keywords, ["/paiptv/", "/00/SNM/", "/00/CHANNEL"]
    )
    f.allowed_schemes = as_list(f.allowed_schemes, ["http", "https"])

    s = cfg.sources
    s.files = as_list(s.files, ["sources"])
    s.playlists = as_list(s.playlists, [])
    s.fetch_timeout = as_float(s.fetch_timeout, 15.0)
    s.max_bytes = as_int(s.max_bytes, 5_000_000)
    z = s.zubo
    z.json_paths = as_list(z.json_paths, list(ZuboConfig().json_paths))
    z.octet_start = as_int(z.octet_start, 1)
    z.octet_end = as_int(z.octet_end, 254)
    z.scan_concurrency = as_int(z.scan_concurrency, 120)
    z.scan_timeout = as_float(z.scan_timeout, 1.5)
    z.max_scan_urls = as_int(z.max_scan_urls, 12000)
    z.interval_hours = as_float(z.interval_hours, 6.0)
    if not isinstance(p.headers, dict):
        p.headers = {}


def load_config(path: Optional[str] = None, *, base_dir: Optional[Path] = None) -> Config:
    """Load ``config.toml``; missing file is allowed and uses safe defaults."""

    if path is None:
        path = os.environ.get("IPTV_CONFIG", "config.toml")
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = (base_dir or Path.cwd()) / config_path
    config_path = config_path.resolve()

    data: Dict[str, Any] = {}
    warnings: List[str] = []
    if config_path.is_file():
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    else:
        warnings.append(f"未找到配置文件 {config_path}，使用默认配置")

    sources_data = _section(data, "sources")
    files_value = sources_data.get("files", ["sources"])
    if isinstance(files_value, str):
        files_value = [files_value]
    playlists_value = sources_data.get("playlists", [])
    if isinstance(playlists_value, str):
        playlists_value = [playlists_value]

    cfg = Config(
        app=_build(AppConfig, _section(data, "app"), "app"),
        filters=_build(FiltersConfig, _section(data, "filters"), "filters"),
        logo=_build(LogoConfig, _section(data, "logo"), "logo"),
        output=_build(OutputConfig, _section(data, "output"), "output"),
        probe=_build(ProbeConfig, _section(data, "probe"), "probe"),
        ranking=_build(RankingConfig, _section(data, "ranking"), "ranking"),
        sources=SourcesConfig(
            files=list(files_value),
            playlists=list(playlists_value),
            fetch_timeout=sources_data.get("fetch_timeout", 15.0),
            max_bytes=sources_data.get("max_bytes", 5_000_000),
            zubo=_build(
                ZuboConfig,
                sources_data.get("zubo", {}),
                "sources.zubo",
            ),
        ),
        base_dir=config_path.parent,
        config_path=config_path,
        warnings=warnings,
    )

    # Unknown-key warnings (ignored but visible).
    for section_name, obj in (
        ("app", cfg.app),
        ("filters", cfg.filters),
        ("logo", cfg.logo),
        ("output", cfg.output),
        ("probe", cfg.probe),
        ("ranking", cfg.ranking),
        ("sources", cfg.sources),
        ("sources.zubo", cfg.sources.zubo),
    ):
        for attr in vars(obj):
            if attr.startswith("_unknown_"):
                cfg.warnings.append(f"[{section_name}] 未识别配置项: {attr[9:]}")

    _coerce_config_types(cfg)
    cfg.validate()

    for text in (cfg.output.epg_url, *(cfg.sources.playlists or [])):
        if text and urlsplit(text).scheme not in ("http", "https"):
            cfg.warnings.append(f"不是 http/https 地址: {text}")

    return cfg


def apply_cli_overrides(
    cfg: Config,
    *,
    demo: Optional[str] = None,
    output_dir: Optional[str] = None,
    groups: Optional[str] = None,
    no_probe: bool = False,
    force_probe: bool = False,
    offline: bool = False,
    max_probes: Optional[int] = None,
) -> Config:
    if demo:
        cfg.app.demo_file = demo
    if output_dir:
        cfg.app.output_dir = output_dir
    if groups is not None:
        cfg.filters.include_groups = [
            item.strip() for item in groups.split(",") if item.strip()
        ]
        # ad-hoc 分组输出不应删除其他分组的旧文件。
        cfg.output.clean_stale_group_files = False
    if no_probe:
        cfg.probe.enabled = False
    if force_probe:
        cfg.probe.force = True
    if offline:
        cfg.offline = True
        cfg.sources.playlists = []
        cfg.sources.zubo.enabled = False
        cfg.probe.enabled = False
        cfg.probe.assume_ok_without_probe = True
    if max_probes is not None:
        cfg.probe.max_probes_per_run = max(1, int(max_probes))
    return cfg


__all__ = [
    "AppConfig",
    "Config",
    "FiltersConfig",
    "LogoConfig",
    "OutputConfig",
    "ProbeConfig",
    "RankingConfig",
    "SourcesConfig",
    "ZuboConfig",
    "apply_cli_overrides",
    "load_config",
]

#!/usr/bin/env python3
"""Custom incremental IPTV playlist builder.

Run locally::

    python ITVlist.py --config config.toml

The script is intentionally dependency-free.  See README.md for the Demo.txt
format, source formats and configuration options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from itvpipe import __version__
from itvpipe.config import Config, apply_cli_overrides, load_config
from itvpipe.models import ChannelSpec, ProbeResult, RunResult
from itvpipe.normalize import DEFAULT_CHANNEL_NAMES, EXTRA_ALIASES
from itvpipe.output import format_time
from itvpipe.pipeline import run_pipeline
from itvpipe.probe import (
    dedupe_sources,
    group_names_by_url,
    parse_playlist,
    parse_playlist_info,
    probe_source,
    rank_channel_sources,
)

LOG = logging.getLogger("itvlist")

# Compatibility constants for code/tests written against the old single file.
RESULTS_PER_CHANNEL = 20
SPEED_TEST_ENABLED = True
SPEED_CONCURRENCY = 32
SPEED_SAMPLE_BYTES = 128 * 1024
SPEED_MIN_SAMPLE_BYTES = 16 * 1024
SPEED_TIMEOUT = 5.0
SPEED_CONNECT_TIMEOUT = 3.0
SPEED_MAX_SECONDS = 14.0
SPEED_RETRIES = 1
SPEED_TLS_VERIFY = False
SPEED_REPORT_FILE = "speed_report.txt"

CHANNEL_CATEGORIES = {
    "全部频道": list(DEFAULT_CHANNEL_NAMES),
}
CHANNEL_MAPPING = {name: list(aliases) for name, aliases in EXTRA_ALIASES.items()}


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def _print_summary(result: RunResult, cfg: Config) -> None:
    stats = result.stats
    mode = "流畅度" if cfg.ranking.mode == "smoothness" else "测速最快(mode=speed)"
    print("")
    print("=" * 64)
    print(f"频道: {stats.channels_selected}/{stats.channels_known}   "
          f"播放源: {stats.sources_selected}   "
          f"测速: {stats.probes_run} (成功 {stats.probe_ok})   缓存命中: {stats.cache_hits}")
    print(f"排序: {mode}   最小流畅度: {cfg.ranking.min_smooth_score:g}"
          f"   分数分档: {cfg.ranking.score_bucket:g}")
    print(f"原始: {stats.raw_sources}  有效: {stats.valid_sources}  "
          f"过滤: {stats.filtered_sources}  重复: {stats.duplicate_sources}  "
          f"清理失效: {stats.dropped_dead}")
    groups = ", ".join(f"{name}={count}" for name, count in sorted(stats.groups.items()))
    print(f"分组: {groups or '无'}")
    print(f"更新标记: {format_time(result.updated_at, cfg.app.timezone)}")
    label = "预计变更" if cfg.dry_run else "变更文件"
    print(f"{label}: {len(stats.changed_files)}  未变: {len(stats.unchanged_files)}")
    if stats.warnings:
        print(f"警告: {len(stats.warnings)} 条（详见日志）")
    print("=" * 64)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 Demo.txt 白名单增量构建 IPTV 播放列表（无第三方依赖）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("IPTV_CONFIG", "config.toml"),
        help="TOML 配置文件",
    )
    parser.add_argument("--demo", help="覆盖 Demo.txt 路径")
    parser.add_argument("--output-dir", help="覆盖输出目录")
    parser.add_argument("--groups", help="只输出指定分组，逗号分隔，例如 央视频道,卫视频道")
    parser.add_argument("--no-probe", action="store_true", help="跳过测速，未验证源按可播放预览")
    parser.add_argument("--force-probe", action="store_true", help="忽略测速缓存 TTL，强制重新验证")
    parser.add_argument("--offline", action="store_true", help="不访问网络，只用本地文件/缓存")
    parser.add_argument("--max-probes", type=int, help="本轮最多测速 URL 数")
    parser.add_argument("--dry-run", action="store_true", help="只计算，不写入任何文件")
    parser.add_argument("--strict", action="store_true", help="没有任何频道时返回非零退出码")
    parser.add_argument("--json", action="store_true", help="打印机器可读的摘要")
    parser.add_argument("--validate", action="store_true", help="只校验配置并退出")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
        apply_cli_overrides(
            cfg,
            demo=args.demo,
            output_dir=args.output_dir,
            groups=args.groups,
            no_probe=args.no_probe,
            force_probe=args.force_probe,
            offline=args.offline,
            max_probes=args.max_probes,
        )
        cfg.dry_run = bool(args.dry_run)
        if args.no_probe and not args.offline:
            cfg.probe.assume_ok_without_probe = True
        if not args.verbose:
            logging.getLogger().setLevel(
                getattr(logging, str(cfg.app.log_level).upper(), logging.INFO)
            )
    except (OSError, ValueError) as exc:
        LOG.error("配置错误: %s", exc)
        return 2

    if args.validate:
        print(json.dumps(cfg.to_public_dict(), ensure_ascii=False, indent=2))
        return 0

    try:
        result = asyncio.run(run_pipeline(cfg))
    except KeyboardInterrupt:
        LOG.warning("已中断")
        return 130
    except Exception:
        LOG.exception("运行失败")
        return 1

    _print_summary(result, cfg)
    if args.json:
        payload = {
            **result.manifest,
            "stats": result.stats.to_dict(),
            "exit_code": result.exit_code,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.strict and result.stats.channels_selected == 0:
        LOG.error("没有任何频道输出（--strict）")
        return 1
    return result.exit_code


# ---------------------------------------------------------------------------
# Backwards-compatible helpers (the old ITVlist.py API)
# ---------------------------------------------------------------------------
def write_itvlist(
    channel_urls: Dict[str, List[str]],
    beijing_now: str,
    disclaimer_url: str = "http://123.mp4",
    path: str = "itvlist.txt",
) -> None:
    """Write the legacy TXT format used by the original project."""

    lines = [f"更新时间: {beijing_now}（北京时间）", "", "全部频道,#genre#"]
    for name, urls in channel_urls.items():
        for url in urls:
            lines.append(f"{name},{url}")
    if disclaimer_url:
        lines.extend(["更新时间,#genre#", f"{beijing_now},{disclaimer_url}"])
    lines.append("")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def write_speed_report(
    ranked: Dict[str, List[ProbeResult]],
    generated_at: str,
    path: str = SPEED_REPORT_FILE,
) -> None:
    lines = [
        "# 频道播放源流畅度报告（每个频道按流畅度评分从高到低，不再按瞬时测速峰值）",
        f"# 生成时间: {generated_at}（北京时间）",
        "# 频道\t排名\t流畅度\t有效码率(kbps)\t平均(kbps)\t抖动CV\t停顿\t首帧(ms)\t采样(bytes)\t方式\t地址",
    ]
    for name, items in ranked.items():
        for index, item in enumerate(items, 1):
            effective = item.effective_kbps
            speed = f"{effective:.1f}" if effective is not None else "-"
            average = f"{item.kbps:.1f}" if item.kbps is not None else "-"
            lines.append(
                f"{name}\t{index}\t{item.smooth_score():.1f}\t{speed}\t{average}\t"
                f"{item.jitter:.2f}\t{item.stalls}\t{item.latency_ms:.0f}\t"
                f"{item.sample_bytes}\t{item.via}\t{item.url}"
            )
    lines.append("")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

"""Playlist rendering: M3U, TXT, per-group files, reports and manifest."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from .config import Config
from .models import ChannelSpec, SourceRecord, Stats
from .normalize import epg_id_for
from .state import OutputTracker, write_if_changed

LOG = logging.getLogger(__name__)


def _tz(name: str) -> tzinfo:
    if ZoneInfo and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=8))


def format_time(timestamp: float, tz_name: str = "Asia/Shanghai") -> str:
    return datetime.fromtimestamp(timestamp, _tz(tz_name)).strftime("%y/%m/%d-%H:%M:%S")


def _iso_time(timestamp: float, tz_name: str = "Asia/Shanghai") -> str:
    return datetime.fromtimestamp(timestamp, _tz(tz_name)).isoformat(timespec="seconds")


def _attr(value: str) -> str:
    return html.escape(str(value or ""), quote=True).replace("\n", " ").replace("\r", " ")


def _safe_filename(name: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return text or "group"


def _tvg_id(name: str, spec: Optional[ChannelSpec], cfg: Config) -> str:
    if spec and spec.epg_id:
        return spec.epg_id
    return epg_id_for(name, cfg.output.tvg_id_style)


def _channel_order(
    names: List[str], specs: Dict[str, ChannelSpec]
) -> List[str]:
    return sorted(
        names,
        key=lambda name: (
            specs[name].order if name in specs else 1_000_000,
            name,
        ),
    )


def build_m3u(
    cfg: Config,
    grouped: List[Tuple[str, List[str]]],
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    *,
    epg_url: str = "",
    updated_at: float = 0.0,
    include_footer: bool = True,
) -> str:
    lines: List[str] = ["#EXTM3U"]
    if epg_url:
        lines[0] += f' x-tvg-url="{_attr(epg_url)}"'
    if cfg.output.header_comment:
        lines.append(f"# {cfg.output.header_comment}")

    for group, names in grouped:
        for name in _channel_order(names, specs):
            records = selected.get(name, [])
            if not records:
                continue
            spec = specs.get(name)
            tvg_id = _tvg_id(name, spec, cfg)
            logo = ""
            if records:
                logo = records[0].logo or (spec.logo if spec else "")
            for record in records:
                attrs = [
                    f'tvg-id="{_attr(tvg_id)}"',
                    f'tvg-name="{_attr(name)}"',
                ]
                if logo:
                    attrs.append(f'tvg-logo="{_attr(logo)}"')
                attrs.append(f'group-title="{_attr(group)}"')
                lines.append("#EXTINF:-1 " + " ".join(attrs) + f",{name}")
                if cfg.output.extgrp:
                    lines.append(f"#EXTGRP:{group}")
                if cfg.output.include_vlc_options and record.headers:
                    for header_name, header_value in record.headers.items():
                        lower = header_name.lower()
                        if lower == "user-agent":
                            lines.append(f"#EXTVLCOPT:http-user-agent={header_value}")
                        elif lower in ("referer", "referrer"):
                            lines.append(f"#EXTVLCOPT:http-referrer={header_value}")
                lines.append(record.url)

    if include_footer and cfg.output.append_update_entry and cfg.output.update_entry_url:
        stamp = format_time(updated_at, cfg.app.timezone)
        lines.append(
            "#EXTINF:-1 "
            f'tvg-id="更新时间" tvg-name="{_attr(stamp)}" '
            f'group-title="更新时间",{stamp}'
        )
        lines.append(cfg.output.update_entry_url)

    lines.append("")
    return "\n".join(lines)


def build_txt(
    cfg: Config,
    grouped: List[Tuple[str, List[str]]],
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    *,
    updated_at: float,
    include_footer: bool = True,
) -> str:
    lines = [f"更新时间: {format_time(updated_at, cfg.app.timezone)}（北京时间）", ""]
    for group, names in grouped:
        if not names:
            if cfg.output.include_empty_groups:
                lines.append(f"{group},#genre#")
            continue
        lines.append(f"{group},#genre#")
        for name in _channel_order(names, specs):
            for record in selected.get(name, []):
                lines.append(f"{name},{record.url}")
    if include_footer and cfg.output.append_update_entry and cfg.output.update_entry_url:
        lines.append("")
        lines.append("更新时间,#genre#")
        lines.append(
            f"{format_time(updated_at, cfg.app.timezone)},{cfg.output.update_entry_url}"
        )
    lines.append("")
    return "\n".join(lines)


def build_group_m3u(
    cfg: Config,
    group: str,
    names: List[str],
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    *,
    epg_url: str = "",
) -> str:
    return build_m3u(
        cfg,
        [(group, names)],
        selected,
        specs,
        epg_url=epg_url,
        include_footer=False,
    )


def build_group_txt(
    cfg: Config,
    group: str,
    names: List[str],
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    *,
    updated_at: float,
) -> str:
    return build_txt(
        cfg,
        [(group, names)],
        selected,
        specs,
        updated_at=updated_at,
        include_footer=False,
    )


def build_speed_report(
    cfg: Config,
    grouped: List[Tuple[str, List[str]]],
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    *,
    updated_at: float,
) -> str:
    ranking = cfg.ranking
    options = ranking.options()
    lines = [
        "# 频道播放源流畅度报告（按流畅度评分从高到低，已包含持续码率/抖动/停顿/首帧/历史可靠性）",
        f"# 生成时间: {format_time(updated_at, cfg.app.timezone)}（北京时间）",
        "# 频道\t分组\t排名\t流畅度\t有效码率(kbps)\t平均(kbps)\t抖动CV\t停顿\t首帧(ms)\t采样(bytes)\t方式\t清晰度\t地址",
    ]
    for group, names in grouped:
        for name in _channel_order(names, specs):
            for index, record in enumerate(selected.get(name, []), 1):
                effective = record.effective_kbps
                speed = f"{effective:.1f}" if effective is not None else "-"
                average = f"{record.kbps:.1f}" if record.kbps is not None else "-"
                score = record.smooth_score(options)
                lines.append(
                    "\t".join(
                        [
                            name,
                            group,
                            str(index),
                            f"{score:.1f}",
                            speed,
                            average,
                            f"{record.jitter:.2f}",
                            str(record.stalls),
                            f"{record.latency_ms:.0f}",
                            str(record.sample_bytes),
                            record.via or "-",
                            record.quality or "-",
                            record.url,
                        ]
                    )
                )
    lines.append("")
    return "\n".join(lines)


def _manifest(
    cfg: Config,
    grouped: List[Tuple[str, List[str]]],
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    *,
    updated_at: float,
    epg_url: str,
    files: List[str],
) -> Dict[str, Any]:
    groups = []
    for group, names in grouped:
        groups.append(
            {
                "name": group,
                "channels": len(names),
                "sources": sum(len(selected.get(name, [])) for name in names),
            }
        )
    return {
        "version": 1,
        # 使用播放列表最后更新时间而不是本进程启动时间，避免“只改时间”的提交。
        "generated_at": _iso_time(updated_at, cfg.app.timezone),
        "playlist_updated_at": _iso_time(updated_at, cfg.app.timezone),
        "epg_url": epg_url,
        "channels": len(selected),
        "sources": sum(len(items) for items in selected.values()),
        "groups": groups,
        "files": files,
    }


def _stable_stats(stats: Stats) -> Dict[str, Any]:
    data = stats.to_dict()
    # 这些字段每轮都会变，不应让 manifest 产生无意义 diff。
    for key in ("elapsed_seconds", "changed_files", "unchanged_files"):
        data.pop(key, None)
    return data


def _cleanup_group_files(
    cfg: Config,
    expected: set,
    tracker: OutputTracker,
) -> None:
    if not cfg.output.clean_stale_group_files:
        return
    group_dir = cfg.output_dir / cfg.output.group_dir
    try:
        prefix = group_dir.resolve().relative_to(cfg.base_dir.resolve()).as_posix()
    except ValueError:
        prefix = group_dir.as_posix()
    for relative in list(tracker.hashes):
        if not relative.startswith(prefix.rstrip("/") + "/"):
            continue
        if relative in expected:
            continue
        suffix = Path(relative).suffix.lower()
        if suffix not in {".m3u", ".m3u8", ".txt"}:
            continue
        path = cfg.base_dir / relative
        if path.is_file() and not tracker.dry_run:
            try:
                path.unlink()
                LOG.info("清理已不存在的分组文件: %s", relative)
            except OSError as exc:
                LOG.warning("删除分组文件失败 %s: %s", relative, exc)
        tracker.hashes.pop(relative, None)
        tracker.changed.append(relative)


def write_outputs(
    cfg: Config,
    selected: Dict[str, List[SourceRecord]],
    grouped: List[Tuple[str, List[str]]],
    specs: Dict[str, ChannelSpec],
    tracker: OutputTracker,
    *,
    updated_at: float,
    stats: Stats,
    epg_url: str = "",
) -> Dict[str, Any]:
    out_dir = cfg.output_dir
    if not tracker.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    changed_before = len(tracker.changed)
    files: List[str] = []
    m3u_text = ""
    txt_text = ""

    if "m3u" in cfg.output.formats:
        m3u_text = build_m3u(
            cfg, grouped, selected, specs, epg_url=epg_url, updated_at=updated_at
        )
        path = out_dir / cfg.output.playlist_name
        write_if_changed(path, m3u_text, tracker)
        files.append(tracker.relative(path))

    if "txt" in cfg.output.formats:
        txt_text = build_txt(cfg, grouped, selected, specs, updated_at=updated_at)
        path = out_dir / cfg.output.txt_name
        write_if_changed(path, txt_text, tracker)
        files.append(tracker.relative(path))

    if cfg.output.legacy_txt and txt_text and cfg.app.legacy_txt_file.strip():
        legacy = cfg.path(cfg.app.legacy_txt_file)
        write_if_changed(legacy, txt_text, tracker)
        files.append(tracker.relative(legacy))

    if cfg.output.per_group:
        group_dir = out_dir / cfg.output.group_dir
        expected = set()
        for group, names in grouped:
            if not names and not cfg.output.include_empty_groups:
                continue
            base = _safe_filename(group)
            if "m3u" in cfg.output.per_group_formats:
                text = build_group_m3u(cfg, group, names, selected, specs, epg_url=epg_url)
                path = group_dir / f"{base}.m3u"
                write_if_changed(path, text, tracker)
                expected.add(tracker.relative(path))
                files.append(tracker.relative(path))
            if "txt" in cfg.output.per_group_formats:
                text = build_group_txt(
                    cfg, group, names, selected, specs, updated_at=updated_at
                )
                path = group_dir / f"{base}.txt"
                write_if_changed(path, text, tracker)
                expected.add(tracker.relative(path))
                files.append(tracker.relative(path))
        _cleanup_group_files(cfg, expected, tracker)

    if cfg.app.report_file:
        report = build_speed_report(
            cfg, grouped, selected, specs, updated_at=updated_at
        )
        path = cfg.path(cfg.app.report_file)
        write_if_changed(path, report, tracker)
        files.append(tracker.relative(path))

    manifest = _manifest(
        cfg,
        grouped,
        selected,
        specs,
        updated_at=updated_at,
        epg_url=epg_url,
        files=files,
    )
    stats.changed_files = list(dict.fromkeys(tracker.changed))
    stats.unchanged_files = list(dict.fromkeys(tracker.unchanged))
    manifest["files"] = files
    manifest["stats"] = _stable_stats(stats)
    if cfg.output.write_manifest:
        path = out_dir / "manifest.json"
        text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        write_if_changed(path, text, tracker)
        files.append(tracker.relative(path))
        manifest["files"] = files
        stats.changed_files = list(dict.fromkeys(tracker.changed))
        stats.unchanged_files = list(dict.fromkeys(tracker.unchanged))

    # 仅返回给日志/摘要，不写入 manifest，避免每轮 changed 列表造成无意义 diff。
    manifest["changed_files"] = list(stats.changed_files)
    LOG.info(
        "输出完成：%d 个频道 / %d 条源；变更文件 %d 个，未变 %d 个",
        len(selected),
        sum(len(items) for items in selected.values()),
        len(stats.changed_files),
        len(stats.unchanged_files),
    )
    return manifest


__all__ = [
    "build_group_m3u",
    "build_group_txt",
    "build_m3u",
    "build_speed_report",
    "build_txt",
    "format_time",
    "write_outputs",
]

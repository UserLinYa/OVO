"""End-to-end pipeline: load -> normalise -> incremental probe -> select."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Dict, Iterable, List, Set, Tuple

from .config import Config
from .models import (
    ChannelSpec,
    ParseBundle,
    ProbeResult,
    RawSource,
    RunResult,
    SourceRecord,
    Stats,
)
from .normalize import (
    DEFAULT_CHANNEL_NAMES,
    build_alias_index,
    canonical_url,
    epg_id_for,
    infer_group,
    is_candidate_url,
    logo_for,
    natural_channel_key,
    normalize_group,
    normalize_playback_url,
    resolve_channel,
)
from .parsers import (
    decode_bytes,
    iter_source_files,
    parse_auto_text,
    parse_file,
)
from .probe import HttpClient, ProbeRunner
from .state import StateManager
from .zubo import scan_zubo

LOG = logging.getLogger(__name__)


def _default_specs() -> Dict[str, ChannelSpec]:
    specs: Dict[str, ChannelSpec] = {}
    for index, name in enumerate(DEFAULT_CHANNEL_NAMES):
        specs[name] = ChannelSpec(
            name=name,
            group=infer_group(name),
            order=index,
            origin="built-in",
        )
    return specs


def load_demo(cfg: Config) -> ParseBundle:
    path = cfg.demo_path
    if not path.is_file():
        LOG.warning("未找到 %s，使用内置频道白名单（%d 个）", path, len(DEFAULT_CHANNEL_NAMES))
        bundle = ParseBundle(warnings=[f"缺少 {path}"])
        for index, name in enumerate(DEFAULT_CHANNEL_NAMES):
            bundle.add_channel(
                ChannelSpec(
                    name=name,
                    group=infer_group(name),
                    order=index,
                    origin="built-in",
                )
            )
        return bundle
    bundle = parse_file(path, demo=True)
    if not bundle.channels:
        LOG.warning("Demo 文件未解析到频道，使用内置频道白名单")
        for index, name in enumerate(DEFAULT_CHANNEL_NAMES):
            bundle.add_channel(
                ChannelSpec(name=name, group=infer_group(name), order=index, origin="built-in")
            )
    return bundle


def load_local_sources(cfg: Config) -> ParseBundle:
    bundle = ParseBundle()
    entries = cfg.sources.files or []
    if isinstance(entries, str):
        entries = [entries]
    files = iter_source_files(entries, cfg.base_dir, demo_path=cfg.demo_path)
    for path in files:
        LOG.info("读取源文件: %s", path)
        bundle.extend(parse_file(path))
    return bundle


def load_remote_sources(cfg: Config, client: HttpClient) -> ParseBundle:
    bundle = ParseBundle()
    for url in cfg.sources.playlists or []:
        if not url.startswith(("http://", "https://")):
            bundle.warnings.append(f"远程源不是 http/https: {url}")
            continue
        LOG.info("下载远程播放列表: %s", url)
        fetched = client.fetch(
            url,
            max_bytes=cfg.sources.max_bytes,
            timeout=cfg.sources.fetch_timeout,
            range_bytes=False,
        )
        if fetched.error or fetched.status not in (200, 206):
            message = f"下载 {url} 失败: {fetched.error or fetched.status}"
            LOG.warning(message)
            bundle.warnings.append(message)
            continue
        text = decode_bytes(fetched.data)
        parsed = parse_auto_text(text, origin=url)
        for source in parsed.sources:
            source.priority = max(source.priority, 10)
        bundle.extend(parsed)
    return bundle


def _ensure_channel_specs(
    bundle: ParseBundle,
    cfg: Config,
    *,
    stats: Stats,
) -> Dict[str, ChannelSpec]:
    specs = dict(bundle.channels)
    if not specs:
        specs = _default_specs()
    # Fill groups / order and de-duplicate aliases.
    for index, spec in enumerate(specs.values()):
        if spec.group:
            spec.group = normalize_group(spec.group, spec.name)
        if not spec.group:
            spec.group = infer_group(spec.name)
        if spec.order >= 1_000_000:
            spec.order = index
        spec.epg_id = spec.epg_id or epg_id_for(spec.name, cfg.output.tvg_id_style)
    stats.channels_known = len(specs)
    return specs


def _name_excluded(name: str, cfg: Config) -> bool:
    lowered = (name or "").lower()
    return any(keyword and keyword.lower() in lowered for keyword in cfg.filters.exclude_name_keywords)


def canonicalize_sources(
    bundle: ParseBundle,
    cfg: Config,
    specs: Dict[str, ChannelSpec],
    *,
    stats: Stats,
    now: float,
) -> List[RawSource]:
    """Resolve names, validate URLs, dedupe and attach unified metadata."""

    alias_index = build_alias_index(specs)
    valid: List[RawSource] = []
    seen: Dict[Tuple[str, str], RawSource] = {}
    for raw in bundle.sources:
        stats.raw_sources += 1
        if _name_excluded(raw.original_name or raw.name, cfg):
            stats.filtered_sources += 1
            continue
        channel = resolve_channel(raw.name, alias_index)
        if not channel:
            stats.filtered_sources += 1
            continue
        if channel not in specs:
            if cfg.filters.require_demo and not cfg.filters.allow_unknown:
                stats.filtered_sources += 1
                continue
            spec = ChannelSpec(
                name=channel,
                group=normalize_group(raw.group, channel) or infer_group(channel),
                logo=raw.logo,
                epg_id=raw.epg_id or epg_id_for(channel, cfg.output.tvg_id_style),
                order=len(specs),
                origin=raw.source,
            )
            specs[channel] = spec
            alias_index = build_alias_index(specs)

        spec = specs[channel]
        url = normalize_playback_url(raw.url)
        ok, reason = is_candidate_url(
            url,
            allowed_schemes=cfg.filters.allowed_schemes,
            blocked_keywords=cfg.filters.exclude_url_keywords,
            max_length=cfg.filters.max_url_length,
        )
        if not ok:
            LOG.debug("丢弃源 %s: %s", raw.url, reason)
            stats.filtered_sources += 1
            continue
        canonical = canonical_url(url)
        if not canonical:
            stats.filtered_sources += 1
            continue
        group = normalize_group(spec.group or raw.group, channel)
        if not group:
            group = infer_group(channel)
        if cfg.filters.include_groups and group not in cfg.filters.include_groups:
            stats.filtered_sources += 1
            continue
        logo = logo_for(channel, spec, cfg.logo.template if cfg.logo.enabled else "", cfg.logo.overrides)
        if not logo:
            logo = raw.logo
        normalized = RawSource(
            name=channel,
            url=url,
            group=group,
            logo=logo,
            source=raw.source or "",
            priority=raw.priority,
            original_name=raw.original_name or raw.name,
            epg_id=spec.epg_id or epg_id_for(channel, cfg.output.tvg_id_style),
            headers=dict(raw.headers),
        )
        key = (channel, canonical)
        existing = seen.get(key)
        if existing is not None:
            stats.duplicate_sources += 1
            if normalized.priority > existing.priority:
                seen[key] = normalized
            continue
        seen[key] = normalized

    valid = list(seen.values())
    stats.valid_sources = len(valid)
    stats.channels_known = len(specs)
    return valid


def build_library(
    sources: Iterable[RawSource],
    state: StateManager,
    specs: Dict[str, ChannelSpec],
    cfg: Config,
    *,
    now: float,
) -> None:
    for source in sources:
        spec = specs.get(source.name)
        if spec is None:
            continue
        state.upsert_source(
            channel=source.name,
            url=source.url,
            canonical=canonical_url(source.url),
            group=source.group or spec.group,
            logo=source.logo,
            tvg_id=source.epg_id or spec.epg_id,
            source=source.source,
            priority=source.priority,
            now=now,
            headers=source.headers,
        )

    # Restore metadata on records retained from previous runs.
    for channel, records in list(state.library.items()):
        spec = specs.get(channel)
        if spec is None:
            continue
        for record in records.values():
            record.group = record.group or spec.group
            record.logo = record.logo or logo_for(
                channel, spec, cfg.logo.template if cfg.logo.enabled else "", cfg.logo.overrides
            )
            record.tvg_id = record.tvg_id or spec.epg_id or epg_id_for(channel, cfg.output.tvg_id_style)
            state.apply_cache(record, now)

    state.prune(channels=specs.keys(), now=now)


def _fresh_ok_count(state: StateManager, records: Iterable[SourceRecord], now: float) -> int:
    count = 0
    for record in records:
        entry = state.cache.get(record.canonical_url)
        if entry and entry.get("ok") and state.cache_is_fresh(entry, now):
            count += 1
        elif (
            record.status == "ok"
            and record.checked_at
            and (now - record.checked_at) <= state.cfg.probe.success_ttl_hours * 3600
        ):
            count += 1
    return count


def build_probe_plan(
    state: StateManager,
    specs: Dict[str, ChannelSpec],
    cfg: Config,
    *,
    now: float,
) -> Tuple[Dict[str, str], List[Tuple[str, List[str]]], int, int]:
    """Choose a small, fair number of URLs to probe.

    Returns ``(canonical_url -> playback_url, per_channel_plan,
    cache_hits, cache_skips)``.
    """

    channels: List[Tuple[int, int, str, List[Tuple[str, SourceRecord]]]] = []
    cache_hits = 0
    cache_skips = 0

    for spec in specs.values():
        records = state.records_for(spec.name)
        if not records:
            continue
        fresh_ok = 0
        candidates: List[Tuple[str, SourceRecord]] = []
        for record in records:
            entry = state.cache.get(record.canonical_url)
            fresh = state.cache_is_fresh(entry, now)
            if entry and fresh and entry.get("ok"):
                fresh_ok += 1
                cache_hits += 1
                continue
            if entry and fresh and not entry.get("ok"):
                # A recent failure is already known; do not waste a request.
                cache_skips += 1
                continue
            if record.quarantined_until and record.quarantined_until > now and not cfg.probe.force:
                cache_skips += 1
                continue
            if record.status == "failed" and record.fail_count >= cfg.probe.drop_after_failures:
                if not cfg.probe.force and record.quarantined_until > now:
                    cache_skips += 1
                    continue
            candidates.append((record.canonical_url, record))

        if not candidates:
            continue

        def candidate_key(item: Tuple[str, SourceRecord]) -> tuple:
            record = item[1]
            ranking = state.rank_key(record)
            if record.status == "ok":
                # Revalidate known-good sources before spending probes on new ones.
                return (0, -record.last_ok_at, -record.checked_at, ranking)
            if record.status == "failed":
                return (2, record.fail_count, -record.last_ok_at, ranking)
            digest = hashlib.sha1(item[0].encode("utf-8")).hexdigest()
            return (1, -record.priority, digest, ranking)

        candidates.sort(key=candidate_key)
        target = cfg.output.max_sources_per_channel
        slots = max(0, target - fresh_ok)
        quota = slots + max(0, cfg.probe.max_new_per_channel)
        if fresh_ok >= target:
            quota = max(1, cfg.probe.max_new_per_channel)
        quota = max(1, min(quota, cfg.probe.max_candidates_per_channel))
        candidates = candidates[:quota]
        channels.append((fresh_ok, spec.order, spec.name, candidates))

    # Channels with no healthy source get the first probes, then Demo order.
    channels.sort(key=lambda item: (item[0], item[1], natural_channel_key(item[2])))

    selected: Dict[str, str] = {}
    per_channel: List[Tuple[str, List[str]]] = []
    max_len = max((len(candidates) for _, _, _, candidates in channels), default=0)
    for round_index in range(max_len):
        for _, _, channel, candidates in channels:
            if round_index >= len(candidates):
                continue
            if len(selected) >= cfg.probe.max_probes_per_run:
                break
            canonical, record = candidates[round_index]
            if canonical in selected:
                continue
            if not record.url:
                continue
            selected[canonical] = record.url
            per_channel.append((channel, [canonical]))
        if len(selected) >= cfg.probe.max_probes_per_run:
            break
    return selected, per_channel, cache_hits, cache_skips


def _ewma(previous: float, sample: float, alpha: float) -> float:
    """指数滑动平均；``previous <= 0`` 表示还没有历史，用中性起点。"""

    base = previous if previous and previous > 0 else 0.7
    return base + alpha * (sample - base)


def _update_record_from_result(
    record: SourceRecord,
    result: ProbeResult,
    cfg: Config,
    now: float,
) -> None:
    record.checked_at = now
    record.status = "ok" if result.ok else "failed"
    record.last_error = (result.error or "")[:300]
    if result.ok:
        record.kbps = result.kbps
        record.latency_ms = result.latency_ms
        record.sample_bytes = result.sample_bytes
        record.via = result.via
        record.quality = result.quality
        # 流畅度原始指标：持续速率、抖动、停顿、声明码率、分片时长。
        record.kbps_p50 = result.kbps_p50
        record.kbps_p25 = result.kbps_p25
        record.jitter = result.jitter
        record.stalls = result.stalls
        record.samples = result.samples
        record.content_length = result.content_length
        record.segment_seconds = result.segment_seconds
        record.variant_kbps = result.variant_kbps
        record.last_ok_at = now
        record.fail_count = 0
        record.quarantined_until = 0.0
        # 成功一次缓慢加分：偶发失败的源永远追不上稳定源。
        # 需要重试才成功也按"半次成功"计分——连接被重置本身就是抖动信号。
        sample = 1.0 if not result.retries else 0.5
        record.success_ewma = round(_ewma(record.success_ewma, sample, 0.35), 4)
    else:
        record.fail_count += 1
        record.kbps = None
        record.kbps_p50 = None
        record.kbps_p25 = None
        # 失败一次扣分更狠（0.6），避免"抖动源"靠一次侥幸测速排到前面。
        record.success_ewma = round(_ewma(record.success_ewma, 0.0, 0.6), 4)
        if record.fail_count >= cfg.probe.drop_after_failures:
            record.quarantined_until = now + cfg.probe.quarantine_hours * 3600


def apply_probe_results(
    state: StateManager,
    results: Dict[str, ProbeResult],
    cfg: Config,
    *,
    now: float,
) -> None:
    if not results:
        return
    for canonical, result in results.items():
        state.put_probe(canonical, result.url, result, now)
    for record in state.all_library_records():
        result = results.get(record.canonical_url)
        if result is not None:
            _update_record_from_result(record, result, cfg, now)


def select_channels(
    state: StateManager,
    specs: Dict[str, ChannelSpec],
    cfg: Config,
    *,
    now: float,
) -> Tuple[Dict[str, List[SourceRecord]], float, int]:
    selected: Dict[str, List[SourceRecord]] = {}
    global_seen: Set[str] = set()
    newest = 0.0
    dropped = 0
    rank_options = state.rank_options()
    min_score = float(getattr(cfg.ranking, "min_smooth_score", 0.0) or 0.0)

    for spec in specs.values():
        if cfg.filters.include_groups and spec.group not in cfg.filters.include_groups:
            continue
        usable: List[SourceRecord] = []
        for record in state.records_for(spec.name):
            if record.status == "failed":
                if record.fail_count >= cfg.probe.drop_after_failures:
                    dropped += 1
                continue
            if record.quarantined_until and record.quarantined_until > now:
                dropped += 1
                continue
            last_proof = max(record.last_ok_at, record.checked_at)
            if record.status != "ok" or not last_proof:
                continue
            if now - last_proof > cfg.probe.stale_after_hours * 3600:
                dropped += 1
                continue
            if not record.url:
                continue
            # 可选的硬门槛：抖动/停顿太严重的源直接不进播放列表。
            if min_score > 0 and record.smooth_score(rank_options) < min_score:
                dropped += 1
                continue
            if cfg.output.dedupe_urls_across_channels and record.canonical_url in global_seen:
                continue
            usable.append(record)

        # 流畅度优先；同分时再按余量/延迟/清晰度/来源优先级，最后用 URL 保证 deterministic。
        usable.sort(key=lambda item: (item.rank_key(rank_options), item.canonical_url))
        # Defensive de-duplication in case an older state file contains duplicates.
        unique: List[SourceRecord] = []
        seen = set()
        for record in usable:
            if record.canonical_url in seen:
                continue
            seen.add(record.canonical_url)
            unique.append(record)
        picked = unique[: cfg.output.max_sources_per_channel]
        if not picked or len(picked) < cfg.output.min_sources_per_channel:
            continue
        if cfg.output.dedupe_urls_across_channels:
            global_seen.update(record.canonical_url for record in picked)
        selected[spec.name] = picked
        newest = max(
            newest,
            max(max(record.last_ok_at, record.checked_at) for record in picked),
        )
    return selected, newest or now, dropped


def _group_channels(
    selected: Dict[str, List[SourceRecord]],
    specs: Dict[str, ChannelSpec],
    cfg: Config,
) -> List[Tuple[str, List[str]]]:
    groups: Dict[str, List[str]] = {}
    for name in selected:
        spec = specs.get(name)
        group = spec.group if spec and spec.group else infer_group(name)
        groups.setdefault(group, []).append(name)

    order = []
    for group in cfg.output.group_order:
        if group in groups and group not in order:
            order.append(group)
    for group in groups:
        if group not in order:
            order.append(group)

    result: List[Tuple[str, List[str]]] = []
    for group in order:
        names = groups.get(group, [])
        names.sort(
            key=lambda name: (
                specs[name].order if name in specs else 1_000_000,
                natural_channel_key(name),
            )
        )
        if names or cfg.output.include_empty_groups:
            result.append((group, names))
    return result


async def run_pipeline(cfg: Config) -> RunResult:
    stats = Stats()
    state = StateManager(cfg, dry_run=getattr(cfg, "dry_run", False))
    client = HttpClient(cfg.probe)
    now = time.time()

    for warning in cfg.warnings:
        LOG.warning(warning)
        stats.warnings.append(warning)

    demo_bundle = load_demo(cfg)
    local_bundle = load_local_sources(cfg)
    remote_bundle = load_remote_sources(cfg, client) if not cfg.offline else ParseBundle()
    zubo_bundle = (
        await scan_zubo(cfg, state, client)
        if (cfg.sources.zubo.enabled and not cfg.offline)
        else ParseBundle()
    )

    bundle = ParseBundle()
    for part in (demo_bundle, local_bundle, remote_bundle, zubo_bundle):
        bundle.extend(part)
    stats.files = len(bundle.files)
    stats.warnings.extend(bundle.warnings)
    for warning in bundle.warnings:
        LOG.warning(warning)

    specs = _ensure_channel_specs(demo_bundle, cfg, stats=stats)
    sources = canonicalize_sources(bundle, cfg, specs, stats=stats, now=now)
    build_library(sources, state, specs, cfg, now=now)

    plan_urls, per_channel, cache_hits, cache_skips = build_probe_plan(
        state, specs, cfg, now=now
    )
    stats.probes_planned = len(plan_urls)
    stats.cache_hits = cache_hits
    stats.cache_skips = cache_skips
    LOG.info(
        "源文件 %d 个：原始 %d 条，有效 %d 条，去重 %d 条，过滤 %d 条；计划测速 %d 个（缓存命中 %d）",
        stats.files,
        stats.raw_sources,
        stats.valid_sources,
        stats.duplicate_sources,
        stats.filtered_sources,
        stats.probes_planned,
        stats.cache_hits,
    )

    results: Dict[str, ProbeResult] = {}
    if cfg.probe.enabled and plan_urls:
        runner = ProbeRunner(cfg.probe, client=client)

        def progress(done: int, total: int) -> None:
            LOG.info("测速进度 %d/%d", done, total)

        headers_by_url = {
            record.url: record.headers
            for record in state.all_library_records()
            if record.canonical_url in plan_urls and record.headers
        }
        try:
            results = await runner.probe_many(
                plan_urls.values(), progress=progress, headers_by_url=headers_by_url
            )
        finally:
            runner.close()
        # Map playback URLs back to canonical keys (the runner keys by URL).
        by_url = {canonical: url for canonical, url in plan_urls.items()}
        canonical_results = {
            canonical: results[url]
            for canonical, url in by_url.items()
            if url in results
        }
        stats.probes_run = len(canonical_results)
        stats.probe_ok = sum(1 for item in canonical_results.values() if item.ok)
        stats.probe_failed = stats.probes_run - stats.probe_ok
        apply_probe_results(state, canonical_results, cfg, now=time.time())
    elif cfg.probe.enabled:
        LOG.info("没有需要测速的源（全部命中缓存或没有候选源）")

    if not cfg.probe.enabled and cfg.probe.assume_ok_without_probe:
        for record in state.all_library_records():
            if record.status == "new":
                record.status = "ok"
                record.checked_at = now
                record.last_ok_at = now
                record.latency_ms = 0.0

    selected, updated_at, dropped = select_channels(state, specs, cfg, now=time.time())
    if not selected:
        previous_update = float(state.run_state.get("last_playlist_update") or 0.0)
        if previous_update:
            updated_at = previous_update
    stats.dropped_dead = dropped
    stats.channels_selected = len(selected)
    stats.sources_selected = sum(len(items) for items in selected.values())
    grouped = _group_channels(selected, specs, cfg)
    stats.groups = {group: len(names) for group, names in grouped}
    if not selected:
        LOG.warning("本轮没有可用频道；已保留状态库，等待下一轮恢复")
        stats.warnings.append("本轮没有可用频道")

    from .output import write_outputs

    state.tracker.dry_run = getattr(cfg, "dry_run", False)
    stats.finish()
    manifest = write_outputs(
        cfg,
        selected,
        grouped,
        specs,
        state.tracker,
        updated_at=updated_at,
        stats=stats,
        epg_url=(cfg.output.epg_url or (bundle.epg_urls[0] if bundle.epg_urls else "")),
    )

    state.run_state["last_playlist_update"] = round(updated_at, 3)
    state.run_state["last_channels"] = len(selected)
    state.run_state["last_sources"] = stats.sources_selected
    if not getattr(cfg, "dry_run", False):
        state.save(now=time.time())

    manifest["stats"] = stats.to_dict()
    manifest["elapsed_seconds"] = stats.elapsed
    return RunResult(
        selected=selected,
        stats=stats,
        updated_at=updated_at,
        manifest=manifest,
        exit_code=0,
    )


__all__ = [
    "apply_probe_results",
    "build_library",
    "build_probe_plan",
    "canonicalize_sources",
    "load_demo",
    "load_local_sources",
    "load_remote_sources",
    "run_pipeline",
    "select_channels",
]

"""JSON state, probe cache and idempotent atomic file output."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import Config
from .models import ProbeResult, SourceRecord


def _now() -> float:
    return time.time()


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        # Keep a corrupt backup for humans instead of silently destroying it.
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return default


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_if_changed(path: Path, text: str, tracker: "OutputTracker") -> bool:
    data = text.encode("utf-8")
    digest = sha256_text(data)
    relative = tracker.relative(path)
    if path.is_file() and tracker.hashes.get(relative) == digest:
        if not tracker.dry_run:
            tracker.unchanged.append(relative)
        return False
    if tracker.dry_run:
        tracker.changed.append(relative)
        tracker.hashes[relative] = digest
        return True
    atomic_write_bytes(path, data)
    tracker.changed.append(relative)
    tracker.hashes[relative] = digest
    return True


class OutputTracker:
    """Track content hashes so unchanged playlists are not rewritten/committed."""

    def __init__(self, path: Path, base_dir: Path, *, dry_run: bool = False):
        self.path = path
        self.base_dir = base_dir
        self.dry_run = dry_run
        data = load_json(path, {})
        self.hashes: Dict[str, str] = dict(data.get("files", {})) if isinstance(data, dict) else {}
        self.changed: List[str] = []
        self.unchanged: List[str] = []

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.base_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def save(self) -> None:
        if self.dry_run:
            return
        payload = dump_json({"version": 1, "files": self.hashes})
        if self.path.is_file():
            try:
                old = self.path.read_text(encoding="utf-8")
            except OSError:
                old = ""
            if old == payload:
                return
        atomic_write_text(self.path, payload)


class StateManager:
    """Persistent probe cache + per-channel source library."""

    VERSION = 2

    def __init__(self, cfg: Config, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.dir = cfg.state_dir
        self.cache_path = self.dir / "probe_cache.json"
        self.library_path = self.dir / "library.json"
        self.run_path = self.dir / "run_state.json"
        self.tracker = OutputTracker(self.dir / "output_hashes.json", cfg.base_dir, dry_run=dry_run)

        cache_data = load_json(self.cache_path, {})
        self.cache: Dict[str, Dict[str, Any]] = {}
        if isinstance(cache_data, dict):
            entries = cache_data.get("entries", cache_data)
            if isinstance(entries, dict):
                self.cache = {
                    str(key): value
                    for key, value in entries.items()
                    if isinstance(value, dict)
                }

        library_data = load_json(self.library_path, {})
        self.library: Dict[str, Dict[str, SourceRecord]] = {}
        if isinstance(library_data, dict):
            channels = library_data.get("channels", {})
            if isinstance(channels, dict):
                for channel, records in channels.items():
                    if not isinstance(records, dict):
                        continue
                    for key, payload in records.items():
                        if not isinstance(payload, dict):
                            continue
                        try:
                            record = self._record_from_payload(str(channel), str(key), payload)
                        except (TypeError, ValueError):
                            continue
                        self.library.setdefault(record.channel, {})[record.canonical_url] = record

        run_data = load_json(self.run_path, {})
        self.run_state: Dict[str, Any] = run_data if isinstance(run_data, dict) else {}
        self.run_state.setdefault("version", self.VERSION)

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _record_from_payload(channel: str, key: str, data: Dict[str, Any]) -> SourceRecord:
        canonical = str(data.get("canonical_url") or key or "")
        return SourceRecord(
            channel=str(data.get("channel") or channel),
            url=str(data.get("url") or ""),
            canonical_url=canonical,
            group=str(data.get("group") or ""),
            logo=str(data.get("logo") or ""),
            tvg_id=str(data.get("tvg_id") or ""),
            source=str(data.get("source") or ""),
            priority=int(data.get("priority") or 0),
            status=str(data.get("status") or "new"),
            kbps=float(data["kbps"]) if data.get("kbps") is not None else None,
            latency_ms=float(data["latency_ms"]) if data.get("latency_ms") is not None else 999999.0,
            sample_bytes=int(data.get("sample_bytes") or 0),
            via=str(data.get("via") or ""),
            quality=str(data.get("quality") or ""),
            checked_at=float(data.get("checked_at") or 0.0),
            last_ok_at=float(data.get("last_ok_at") or 0.0),
            fail_count=int(data.get("fail_count") or 0),
            last_error=str(data.get("last_error") or ""),
            first_seen=float(data.get("first_seen") or 0.0),
            last_seen=float(data.get("last_seen") or 0.0),
            quarantined_until=float(data.get("quarantined_until") or 0.0),
            # 旧版 state 没有这些字段，缺省即"未测量"，评分会自动退回 kbps。
            kbps_p50=_optional_float(data.get("kbps_p50")),
            kbps_p25=_optional_float(data.get("kbps_p25")),
            jitter=float(data.get("jitter") or 0.0),
            stalls=int(data.get("stalls") or 0),
            samples=int(data.get("samples") or 0),
            content_length=int(data.get("content_length") or 0),
            segment_seconds=float(data.get("segment_seconds") or 0.0),
            variant_kbps=_optional_float(data.get("variant_kbps")),
            success_ewma=float(data.get("success_ewma") or 0.0),
            headers={
                str(key): str(value)
                for key, value in (
                    data.get("headers") if isinstance(data.get("headers"), dict) else {}
                ).items()
            },
        )

    @staticmethod
    def _record_to_payload(record: SourceRecord) -> Dict[str, Any]:
        return {
            "url": record.url,
            "canonical_url": record.canonical_url,
            "group": record.group,
            "logo": record.logo,
            "tvg_id": record.tvg_id,
            "source": record.source,
            "priority": record.priority,
            "status": record.status,
            "kbps": round(record.kbps, 1) if record.kbps is not None else None,
            "latency_ms": round(record.latency_ms, 1),
            "sample_bytes": record.sample_bytes,
            "via": record.via,
            "quality": record.quality,
            "checked_at": round(record.checked_at, 3),
            "last_ok_at": round(record.last_ok_at, 3),
            "fail_count": record.fail_count,
            "last_error": record.last_error[:300],
            "first_seen": round(record.first_seen, 3),
            "last_seen": round(record.last_seen, 3),
            "quarantined_until": round(record.quarantined_until, 3),
            "kbps_p50": round(record.kbps_p50, 1) if record.kbps_p50 is not None else None,
            "kbps_p25": round(record.kbps_p25, 1) if record.kbps_p25 is not None else None,
            "jitter": round(record.jitter, 4),
            "stalls": record.stalls,
            "samples": record.samples,
            "content_length": record.content_length,
            "segment_seconds": round(record.segment_seconds, 3),
            "variant_kbps": (
                round(record.variant_kbps, 1) if record.variant_kbps is not None else None
            ),
            "success_ewma": round(record.success_ewma, 4),
            **({"headers": record.headers} if record.headers else {}),
        }

    # ------------------------------------------------------------------
    # Probe cache
    # ------------------------------------------------------------------
    def cache_entry(self, canonical: str) -> Optional[Dict[str, Any]]:
        return self.cache.get(canonical)

    def cache_is_fresh(self, entry: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
        if not entry:
            return False
        now = now or _now()
        checked = float(entry.get("checked_at") or 0.0)
        if checked <= 0:
            return False
        if self.cfg.probe.force:
            return False
        if entry.get("ok"):
            return now - checked <= self.cfg.probe.success_ttl_hours * 3600
        if int(entry.get("fail_count") or 0) >= self.cfg.probe.drop_after_failures:
            quarantine = float(entry.get("quarantined_until") or 0.0)
            if quarantine > now:
                return True
        return now - checked <= self.cfg.probe.failure_ttl_minutes * 60

    def cache_is_usable(self, entry: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
        if not entry or not entry.get("ok"):
            return False
        now = now or _now()
        last_ok = float(entry.get("last_ok_at") or entry.get("checked_at") or 0.0)
        return bool(last_ok) and now - last_ok <= self.cfg.probe.stale_after_hours * 3600

    def put_probe(self, canonical: str, url: str, result: ProbeResult, now: Optional[float] = None) -> None:
        now = now or _now()
        previous = self.cache.get(canonical) or {}
        entry = dict(previous)
        entry["url"] = url
        entry["status"] = result.status
        entry["checked_at"] = round(now, 3)
        entry["error"] = (result.error or "")[:300]
        entry["ok"] = bool(result.ok)
        if result.ok:
            entry["kbps"] = round(result.kbps, 1) if result.kbps is not None else None
            entry["latency_ms"] = round(result.latency_ms, 1)
            entry["sample_bytes"] = result.sample_bytes
            entry["via"] = result.via
            entry["quality"] = result.quality
            entry["kbps_p50"] = (
                round(result.kbps_p50, 1) if result.kbps_p50 is not None else None
            )
            entry["kbps_p25"] = (
                round(result.kbps_p25, 1) if result.kbps_p25 is not None else None
            )
            entry["jitter"] = round(result.jitter, 4)
            entry["stalls"] = int(result.stalls or 0)
            entry["samples"] = int(result.samples or 0)
            entry["content_length"] = int(result.content_length or 0)
            entry["segment_seconds"] = round(float(result.segment_seconds or 0.0), 3)
            entry["variant_kbps"] = (
                round(result.variant_kbps, 1) if result.variant_kbps is not None else None
            )
            entry["last_ok_at"] = round(now, 3)
            entry["fail_count"] = 0
            entry["quarantined_until"] = 0.0
        else:
            entry["fail_count"] = int(previous.get("fail_count") or 0) + 1
            entry["last_ok_at"] = float(previous.get("last_ok_at") or 0.0)
            if entry["fail_count"] >= self.cfg.probe.drop_after_failures:
                entry["quarantined_until"] = round(
                    now + self.cfg.probe.quarantine_hours * 3600, 3
                )
        self.cache[canonical] = entry

    def apply_cache(self, record: SourceRecord, now: Optional[float] = None) -> None:
        """Fill a record from cache if the cache entry is newer."""

        entry = self.cache.get(record.canonical_url)
        if not entry:
            return
        checked = float(entry.get("checked_at") or 0.0)
        if checked <= record.checked_at:
            return
        now = now or _now()
        record.checked_at = checked
        record.status = "ok" if entry.get("ok") else "failed"
        record.kbps = float(entry["kbps"]) if entry.get("kbps") is not None else None
        record.latency_ms = (
            float(entry["latency_ms"]) if entry.get("latency_ms") is not None else 999999.0
        )
        record.sample_bytes = int(entry.get("sample_bytes") or 0)
        record.via = str(entry.get("via") or "")
        record.quality = str(entry.get("quality") or "")
        record.kbps_p50 = _optional_float(entry.get("kbps_p50"))
        record.kbps_p25 = _optional_float(entry.get("kbps_p25"))
        record.jitter = float(entry.get("jitter") or 0.0)
        record.stalls = int(entry.get("stalls") or 0)
        record.samples = int(entry.get("samples") or 0)
        record.content_length = int(entry.get("content_length") or 0)
        record.segment_seconds = float(entry.get("segment_seconds") or 0.0)
        record.variant_kbps = _optional_float(entry.get("variant_kbps"))
        record.last_ok_at = float(entry.get("last_ok_at") or 0.0)
        record.fail_count = int(entry.get("fail_count") or 0)
        record.last_error = str(entry.get("error") or "")
        record.quarantined_until = float(entry.get("quarantined_until") or 0.0)

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------
    def records_for(self, channel: str) -> List[SourceRecord]:
        return list(self.library.get(channel, {}).values())

    def all_library_records(self) -> Iterable[SourceRecord]:
        for records in self.library.values():
            yield from records.values()

    def upsert_source(
        self,
        *,
        channel: str,
        url: str,
        canonical: str,
        group: str,
        logo: str,
        tvg_id: str,
        source: str,
        priority: int,
        now: float,
        headers: Optional[Dict[str, str]] = None,
    ) -> SourceRecord:
        bucket = self.library.setdefault(channel, {})
        record = bucket.get(canonical)
        if record is None:
            record = SourceRecord(
                channel=channel,
                url=url,
                canonical_url=canonical,
                group=group,
                logo=logo,
                tvg_id=tvg_id,
                source=source,
                priority=priority,
                first_seen=now,
                last_seen=now,
                headers=dict(headers or {}),
            )
            bucket[canonical] = record
        else:
            record.url = url
            # 不要每轮都改 last_seen，否则即使播放列表没变也会产生 state diff。
            if not record.last_seen or (now - record.last_seen) >= 12 * 3600:
                record.last_seen = now
            record.priority = max(record.priority, priority)
            record.group = group or record.group
            record.logo = logo or record.logo
            record.tvg_id = tvg_id or record.tvg_id
            if headers:
                record.headers.update(headers)
            if source and not record.source:
                record.source = source
        return record

    def rank_options(self):
        """RankingConfig -> 评分/排序参数对象（见 ``itvpipe.smooth.ScoreOptions``）。"""

        ranking = getattr(self.cfg, "ranking", None)
        if ranking is None:
            return None
        return ranking.options()

    def rank_key(self, record: SourceRecord) -> tuple:
        return record.rank_key(self.rank_options())

    def prune(self, channels: Optional[Iterable[str]] = None, now: Optional[float] = None) -> None:
        now = now or _now()
        allowed = set(channels) if channels is not None else None
        retain_seconds = max(self.cfg.probe.retain_hours, 24) * 3600
        per_channel = max(20, self.cfg.output.max_sources_per_channel * 6)

        for channel in list(self.library):
            if allowed is not None and channel not in allowed:
                del self.library[channel]
                continue
            bucket = self.library[channel]
            for key, record in list(bucket.items()):
                last_activity = max(record.last_seen, record.last_ok_at, record.checked_at)
                if last_activity and now - last_activity > retain_seconds * 2:
                    del bucket[key]
                    continue
                if (
                    record.status == "failed"
                    and record.fail_count >= self.cfg.probe.drop_after_failures
                    and record.quarantined_until
                    and record.quarantined_until < now
                    and now - max(record.last_seen, record.checked_at) > retain_seconds
                ):
                    del bucket[key]
                    continue
            items = sorted(bucket.values(), key=lambda r: (self.rank_key(r), r.canonical_url))
            for record in items[per_channel:]:
                del bucket[record.canonical_url]
            if not bucket:
                del self.library[channel]

        # Probe cache: keep URLs still present in the library or recent entries.
        library_keys = {record.canonical_url for record in self.all_library_records()}
        recent_cutoff = now - max(7 * 86400, self.cfg.probe.retain_hours * 3600)
        cache_items = [
            (key, value)
            for key, value in self.cache.items()
            if key in library_keys or float(value.get("checked_at") or 0) >= recent_cutoff
        ]
        cache_items.sort(key=lambda item: float(item[1].get("checked_at") or 0), reverse=True)
        self.cache = dict(cache_items[:5000])

    def save(self, now: Optional[float] = None) -> None:
        if self.dry_run:
            return
        now = now or _now()
        self.prune(now=now)

        cache_payload = dump_json(
            {"version": self.VERSION, "entries": self.cache}
        )
        self._write_state_file(self.cache_path, cache_payload)

        channels_payload: Dict[str, Dict[str, Any]] = {}
        for channel in sorted(self.library):
            channels_payload[channel] = {
                key: self._record_to_payload(record)
                for key, record in sorted(self.library[channel].items())
            }
        library_payload = dump_json(
            {"version": self.VERSION, "channels": channels_payload}
        )
        self._write_state_file(self.library_path, library_payload)

        run_payload = dict(self.run_state)
        run_payload["version"] = self.VERSION
        self._write_state_file(self.run_path, dump_json(run_payload))
        self.tracker.save()

    def _write_state_file(self, path: Path, text: str) -> None:
        if path.is_file():
            try:
                if path.read_text(encoding="utf-8") == text:
                    return
            except OSError:
                pass
        atomic_write_text(path, text)

    def mark_scan(self, now: Optional[float] = None) -> None:
        self.run_state["last_scan"] = round(now or _now(), 3)

    def scan_is_due(self, now: Optional[float] = None) -> bool:
        if not self.cfg.sources.zubo.enabled:
            return False
        now = now or _now()
        last = float(self.run_state.get("last_scan") or 0.0)
        return (now - last) >= self.cfg.sources.zubo.interval_hours * 3600


__all__ = [
    "OutputTracker",
    "StateManager",
    "atomic_write_bytes",
    "atomic_write_text",
    "dump_json",
    "load_json",
    "write_if_changed",
]

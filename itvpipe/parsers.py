"""Parsers for Demo.txt, M3U/M3U8, TXT and JSON source feeds."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .models import ChannelSpec, DEFAULT_GROUP_ORDER, ParseBundle, RawSource
from .normalize import (
    extract_url,
    infer_group,
    infer_name_from_url,
    normalize_channel_name,
    normalize_group,
    strip_url_options,
)

_ATTR_RE = re.compile(r"([A-Za-z0-9_-]+)\s*=\s*\"([^\"]*)\"")
_ATTR_SINGLE_RE = re.compile(r"([A-Za-z0-9_-]+)\s*=\s*'([^']*)'")
_ATTR_UNQUOTED_RE = re.compile(r"([A-Za-z0-9_-]+)\s*=\s*([^\s,]+)")
_GROUP_HEADER_RE = re.compile(r"^\s*(.+?)\s*,\s*#genre#\s*$", re.IGNORECASE)
_SOURCE_SUFFIXES = {".m3u", ".m3u8", ".txt", ".text", ".json", ".jsonl"}
_IGNORED_CHANNEL_NAMES = {"更新时间", "更新", "公告", "声明", "免责声明", "disclaimer"}


def parse_attrs(text: str) -> Dict[str, str]:
    attrs = {key.upper(): value for key, value in _ATTR_RE.findall(text)}
    for key, value in _ATTR_SINGLE_RE.findall(text):
        attrs.setdefault(key.upper(), value)
    for key, value in _ATTR_UNQUOTED_RE.findall(text):
        attrs.setdefault(key.upper(), value)
    return attrs


def _split_extinf(line: str) -> Tuple[str, str]:
    """Split ``#EXTINF:<duration attrs>,<title>`` at the first unquoted comma."""

    body = line[len("#EXTINF:"):]
    quoted = False
    for index, char in enumerate(body):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return body[:index], body[index + 1:].strip()
    return body, ""


def _parse_extinf(line: str) -> Tuple[Dict[str, str], str]:
    head, title = _split_extinf(line)
    return parse_attrs(head), title.strip()


def _clean_name(name: str) -> str:
    text = (name or "").strip().strip("\"'`“”")
    text = re.sub(r"^\[[^\]]*\]\s*", "", text)
    return text.strip()


def _is_ignored_name(name: str) -> bool:
    return normalize_channel_name(name).lower() in _IGNORED_CHANNEL_NAMES


def _looks_like_group(value: str) -> bool:
    candidate = normalize_group(value)
    return candidate in DEFAULT_GROUP_ORDER and len(value) <= 12


def _add_channel(
    bundle: ParseBundle,
    name: str,
    *,
    aliases: Optional[Iterable[str]] = None,
    group: str = "",
    logo: str = "",
    epg_id: str = "",
    order: Optional[int] = None,
    origin: str = "",
) -> str:
    canonical = normalize_channel_name(name)
    if not canonical:
        return ""
    spec = ChannelSpec(
        name=canonical,
        aliases=[a.strip() for a in (aliases or []) if a and a.strip()],
        group=group or "",
        logo=logo or "",
        epg_id=epg_id or "",
        order=(len(bundle.channels) if order is None else order),
        origin=origin,
    )
    bundle.add_channel(spec)
    return canonical


def parse_demo_text(text: str, origin: str = "Demo.txt") -> ParseBundle:
    """Parse Demo.txt.

    Supported forms::

        # 频道白名单，一行一个
        CCTV1
        CCTV1 | CCTV-1;中央一套 | 央视频道 | https://logo.example/cctv1.png
        CCTV1,http://example/live.m3u8
        CCTV1 | http://example/live.m3u8
        央视频道,#genre#
        CCTV1,http://example/live.m3u8

    A line without a URL is a whitelist entry. A line with a URL is both a
    whitelist entry and an inline source.
    """

    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith(("{", "[")):
        return parse_json_text(text, origin=origin)
    if re.search(r"(?im)^\s*#EXTM3U", stripped[:4000]) or "#EXTINF:" in stripped[:4000]:
        return parse_m3u_text(text, origin=origin)

    bundle = ParseBundle(files=[origin])
    current_group = ""
    order = 0
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith(("#", "//", ";")):
            continue

        group_match = _GROUP_HEADER_RE.match(line)
        if group_match:
            current_group = group_match.group(1).strip()
            continue

        url = extract_url(line)
        pipe_parts = [part.strip() for part in line.split("|")]
        url_is_logo = False
        if url:
            prefix = line[: line.find(url)].lower()
            if "logo=" in prefix or "tvg-logo=" in prefix:
                url_is_logo = True
            if "|" in line and url.lower().split("?", 1)[0].endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")
            ):
                url_is_logo = True
            # 第 4 列是台标列；第 2 列才是内联源。
            if "|" in line and len(pipe_parts) >= 4 and pipe_parts[3].startswith(("http://", "https://")):
                url_is_logo = True
        if url and not url_is_logo:
            url_start = line.find(url)
            if url_start < 0:
                url_start = line.find(url.replace("%20", " "))
            name_part = line[: max(0, url_start)].strip()
            name_part = re.sub(r"[,，|]\s*$", "", name_part).strip()
            name = _clean_name(name_part) or infer_name_from_url(url)
            if _is_ignored_name(name):
                continue
            canonical = _add_channel(bundle, name, group=current_group, origin=origin, order=order)
            bundle.sources.append(
                RawSource(
                    name=canonical or name,
                    url=url,
                    group=current_group,
                    source=origin,
                    priority=100,
                    original_name=name,
                )
            )
            order += 1
            continue

        parts = [part.strip() for part in line.split("|")]
        name = _clean_name(parts[0]) if parts else ""
        if not name:
            continue
        aliases: List[str] = []
        group = current_group
        logo = ""
        epg_id = ""
        for part in parts[1:]:
            if not part:
                continue
            if part.lower().startswith("group="):
                group = part.split("=", 1)[1].strip()
            elif part.lower().startswith(("logo=", "tvg-logo=")):
                logo = part.split("=", 1)[1].strip()
            elif part.lower().startswith(("epg=", "tvg-id=")):
                epg_id = part.split("=", 1)[1].strip()
            elif part.startswith(("http://", "https://")):
                logo = part
            elif _looks_like_group(part) and not group:
                group = part
            elif not aliases:
                aliases = [a.strip() for a in re.split(r"[;；,，]", part) if a.strip()]
            elif not group:
                group = part
            elif not logo:
                logo = part
        _add_channel(
            bundle,
            name,
            aliases=aliases,
            group=group,
            logo=logo,
            epg_id=epg_id,
            order=order,
            origin=origin,
        )
        order += 1
    return bundle


def _resolve_url(reference: str, base: str) -> str:
    reference = reference.strip()
    if not reference:
        return ""
    if reference.startswith("//"):
        scheme = urlsplit(base).scheme or "https"
        return f"{scheme}:{reference}"
    if reference.startswith(("http://", "https://")):
        return reference
    if base.startswith(("http://", "https://")):
        return urljoin(base, reference)
    return reference


def parse_m3u_text(text: str, origin: str = "playlist.m3u") -> ParseBundle:
    bundle = ParseBundle(files=[origin])
    current: Optional[Dict[str, Any]] = None
    extgrp = ""
    pending_headers: Dict[str, str] = {}
    base_url = origin if origin.startswith(("http://", "https://")) else ""

    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTM3U"):
            attrs = parse_attrs(line)
            for key in ("X-TV-G-URL", "URL-TVG", "X-TVG-URL", "TVG-URL"):
                if attrs.get(key):
                    for epg in re.split(r"[,;\s]+", attrs[key]):
                        if epg and epg not in bundle.epg_urls:
                            bundle.epg_urls.append(epg)
            continue
        if upper.startswith("#EXTINF:"):
            attrs, title = _parse_extinf(line)
            current = {"attrs": attrs, "title": title, "group": extgrp, "headers": dict(pending_headers)}
            pending_headers = {}
            continue
        if upper.startswith("#EXTGRP:"):
            extgrp = line.split(":", 1)[1].strip()
            if current is not None:
                current["group"] = extgrp
            continue
        if upper.startswith("#EXTVLCOPT:"):
            option = line.split(":", 1)[1].strip()
            if "=" in option:
                key, value = option.split("=", 1)
                key = key.strip().lower()
                if key in ("http-user-agent", "http-referrer", "http-referer"):
                    header_name = {
                        "http-user-agent": "User-Agent",
                        "http-referrer": "Referer",
                        "http-referer": "Referer",
                    }[key]
                    pending_headers[header_name] = value.strip()
                    if current is not None:
                        current.setdefault("headers", {})[header_name] = value.strip()
            continue
        if upper.startswith("#KODIPROP:") or line.startswith("#"):
            continue

        url = _resolve_url(strip_url_options(line), base_url)
        if not url:
            continue
        attrs = current.get("attrs", {}) if current else {}
        title = _clean_name(current.get("title", "")) if current else ""
        name = title or attrs.get("TVG-NAME") or attrs.get("TVG-ID") or infer_name_from_url(url)
        if _is_ignored_name(name):
            current = None
            continue
        group = current.get("group", "") if current else ""
        group = group or attrs.get("GROUP-TITLE", "")
        logo = attrs.get("TVG-LOGO", "")
        epg_id = attrs.get("TVG-ID", "")
        canonical = _add_channel(
            bundle,
            name,
            group=group,
            logo=logo,
            epg_id=epg_id,
            origin=origin,
            order=len(bundle.channels),
        )
        headers = {}
        if current:
            headers = dict(current.get("headers", {}))
        if attrs.get("TVG-SHIFT"):
            headers.setdefault("x-tvg-shift", attrs["TVG-SHIFT"])
        bundle.sources.append(
            RawSource(
                name=canonical or name,
                url=url,
                group=group,
                logo=logo,
                epg_id=epg_id,
                source=origin,
                priority=50,
                original_name=name,
                headers=headers,
            )
        )
        current = None
    return bundle


def _split_name_url(line: str) -> Tuple[str, str]:
    url = extract_url(line)
    if not url:
        return line.strip(), ""
    url_start = line.find(url)
    if url_start < 0:
        url_start = line.find(url.replace("%20", " "))
    before = line[: max(0, url_start)].strip()
    before = re.sub(r"[,，|]\s*$", "", before).strip()
    return before, url


def parse_txt_text(text: str, origin: str = "sources.txt") -> ParseBundle:
    bundle = ParseBundle(files=[origin])
    current_group = ""
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTINF:") or line.upper().startswith("#EXTM3U"):
            # Mixed file: hand the rest to the M3U parser.
            parsed = parse_m3u_text(text, origin=origin)
            bundle.extend(parsed)
            return bundle
        if line.startswith(("#", "//", ";")):
            continue
        group_match = _GROUP_HEADER_RE.match(line)
        if group_match:
            current_group = group_match.group(1).strip()
            continue
        name, url = _split_name_url(line)
        if url:
            if not url.startswith(("http://", "https://")) and origin.startswith(("http://", "https://")):
                url = urljoin(origin, url)
            name = _clean_name(name) or infer_name_from_url(url)
            if _is_ignored_name(name):
                continue
            canonical = _add_channel(
                bundle,
                name,
                group=current_group,
                origin=origin,
                order=len(bundle.channels),
            )
            bundle.sources.append(
                RawSource(
                    name=canonical or name,
                    url=url,
                    group=current_group,
                    source=origin,
                    priority=10,
                    original_name=name,
                )
            )
            continue
        # A bare channel name in a TXT source file is a valid whitelist entry.
        if len(line) <= 40 and not re.search(r"[\s,]", line):
            _add_channel(bundle, line, group=current_group, origin=origin, order=len(bundle.channels))
    return bundle


_JSON_NAME_KEYS = (
    "name",
    "title",
    "channel",
    "channel_name",
    "tvg-name",
    "tvg_name",
    "tvg-id",
    "tvg_id",
    "display_name",
)
_JSON_URL_KEYS = (
    "url",
    "playback_url",
    "play_url",
    "stream",
    "stream_url",
    "src",
    "address",
    "link",
    "m3u8",
)
_JSON_GROUP_KEYS = ("group", "group-title", "group_title", "category", "type")
_JSON_LOGO_KEYS = ("logo", "tvg-logo", "tvg_logo", "icon", "image")


def _first_key(mapping: Dict[str, Any], keys: Iterable[str]) -> Any:
    lower = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return None


def _walk_json(obj: Any, bundle: ParseBundle, origin: str, depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_json(item, bundle, origin, depth + 1)
        return
    if not isinstance(obj, dict):
        return

    url_value = _first_key(obj, _JSON_URL_KEYS)
    name_value = _first_key(obj, _JSON_NAME_KEYS)
    if isinstance(url_value, str) and url_value.strip():
        raw_url = url_value.strip()
        if origin.startswith(("http://", "https://")) and not raw_url.startswith(("http://", "https://", "//")):
            url = strip_url_options(urljoin(origin, raw_url))
        else:
            url = strip_url_options(raw_url)
        if url:
            name = str(name_value or "").strip() or infer_name_from_url(url)
            if not _is_ignored_name(name):
                group = str(_first_key(obj, _JSON_GROUP_KEYS) or "")
                logo = str(_first_key(obj, _JSON_LOGO_KEYS) or "")
                canonical = _add_channel(
                    bundle,
                    name,
                    group=group,
                    logo=logo,
                    origin=origin,
                    order=len(bundle.channels),
                )
                bundle.sources.append(
                    RawSource(
                        name=canonical or name,
                        url=url,
                        group=group,
                        logo=logo,
                        source=origin,
                        priority=20,
                        original_name=name,
                    )
                )
        # Still walk nested children (some APIs wrap channels).
    # Dict of channel name -> URL is a common lightweight format.
    if len(obj) <= 500 and obj and all(
        isinstance(value, str) and extract_url(value)
        for value in obj.values()
    ):
        for key, value in obj.items():
            url = strip_url_options(value)
            if not url or _is_ignored_name(str(key)):
                continue
            canonical = _add_channel(
                bundle,
                str(key),
                origin=origin,
                order=len(bundle.channels),
            )
            bundle.sources.append(
                RawSource(
                    name=canonical or str(key),
                    url=url,
                    source=origin,
                    priority=5,
                    original_name=str(key),
                )
            )
        return
    for value in obj.values():
        if isinstance(value, (dict, list)):
            _walk_json(value, bundle, origin, depth + 1)


def parse_json_text(text: str, origin: str = "sources.json") -> ParseBundle:
    bundle = ParseBundle(files=[origin])
    cleaned = text.lstrip("\ufeff")
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        # JSONL / concatenated JSON objects.
        parsed_any = False
        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                _walk_json(json.loads(line), bundle, origin)
                parsed_any = True
            except (json.JSONDecodeError, ValueError):
                continue
        if not parsed_any:
            bundle.warnings.append(f"{origin}: JSON 解析失败 ({exc})")
        return bundle
    _walk_json(data, bundle, origin)
    return bundle


def parse_auto_text(text: str, origin: str = "", format_hint: str = "auto") -> ParseBundle:
    """Detect M3U / JSON / TXT automatically."""

    origin = origin or "input"
    hint = format_hint.lower().lstrip(".")
    stripped = text.lstrip("\ufeff \t\r\n")
    if hint in ("m3u", "m3u8") or stripped.startswith("#EXTM3U") or "#EXTINF:" in stripped[:2000]:
        return parse_m3u_text(text, origin=origin)
    if hint in ("json", "jsonl") or stripped.startswith(("{", "[")):
        return parse_json_text(text, origin=origin)
    if hint in ("demo", "whitelist"):
        return parse_demo_text(text, origin=origin)
    return parse_txt_text(text, origin=origin)


def decode_bytes(data: bytes) -> str:
    """Decode common Chinese/UTF-8 playlist encodings without external deps."""

    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def read_text_file(path: Path) -> str:
    return decode_bytes(path.read_bytes())


def parse_file(path: Path, *, demo: bool = False) -> ParseBundle:
    try:
        text = read_text_file(path)
    except OSError as exc:
        bundle = ParseBundle(files=[str(path)])
        bundle.warnings.append(f"读取 {path} 失败: {exc}")
        return bundle
    origin = str(path)
    if demo:
        return parse_demo_text(text, origin=origin)
    return parse_auto_text(text, origin=origin, format_hint=path.suffix)


def iter_source_files(entries: Iterable[str], base_dir: Path, *, demo_path: Path) -> List[Path]:
    """Expand configured files/directories into supported source files."""

    files: List[Path] = []
    demo_resolved = demo_path.resolve()
    for entry in entries:
        path = Path(entry)
        if not path.is_absolute():
            path = base_dir / path
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in _SOURCE_SUFFIXES:
                    if child.resolve() == demo_resolved:
                        continue
                    files.append(child)
        elif path.is_file():
            if path.resolve() != demo_resolved:
                files.append(path)
    # Stable unique order.
    seen = set()
    result = []
    for path in files:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


__all__ = [
    "decode_bytes",
    "iter_source_files",
    "parse_attrs",
    "parse_auto_text",
    "parse_demo_text",
    "parse_file",
    "parse_json_text",
    "parse_m3u_text",
    "parse_txt_text",
    "read_text_file",
]

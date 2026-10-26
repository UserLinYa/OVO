#!/usr/bin/env python3
"""Print a short GitHub Actions summary from dist/manifest.json."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    path = Path("dist/manifest.json")
    if not path.is_file():
        print("dist/manifest.json 未生成")
        return 0
    manifest = json.loads(path.read_text(encoding="utf-8"))
    stats = manifest.get("stats", {})
    print("## IPTV 构建结果")
    print("")
    print(f"- 频道：**{manifest.get('channels', 0)}**，播放源：**{manifest.get('sources', 0)}**")
    print(
        f"- 测速：{stats.get('probes_run', 0)}，成功：{stats.get('probe_ok', 0)}，"
        f"缓存命中：{stats.get('cache_hits', 0)}"
    )
    print(f"- 更新标记：`{manifest.get('playlist_updated_at', '')}`")
    groups = manifest.get("groups", [])
    if groups:
        rendered = "，".join(f"{item['name']}={item['channels']}" for item in groups)
        print(f"- 分组：{rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

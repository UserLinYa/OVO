# 本地源文件目录

把 M3U / TXT / JSON 源文件放在这里，脚本会自动读取（可在 `config.toml` 的 `[sources].files` 中修改）。

支持：

- `*.m3u` / `*.m3u8`
- `*.txt` / `*.text`
- `*.json`

文件内容最终都会按 `Demo.txt` 的频道/别名白名单筛选。示例：

```m3u
#EXTM3U
#EXTINF:-1,CCTV1
http://example.com/live/cctv1.m3u8
```

或：

```text
央视频道,#genre#
CCTV1,http://example.com/live/cctv1.m3u8
```

不需要源文件时，本目录可以保持为空。酒店源扫描和远程播放列表分别在 `config.toml` 的 `[sources.zubo]`、`[sources].playlists` 配置。

# 定制版 IPTV 增量抓取与播放列表构建

按 `Demo.txt` 白名单筛选直播源，在 GitHub Actions 上定时运行；通过持久化测速缓存、候选配额、失效清理和内容哈希，只做必要的测速与输出。运行时 **不需要任何第三方 Python 包**（Python 3.11+ 标准库）。

## 功能对照

| 需求 | 实现 |
| --- | --- |
| 筛选 Demo.txt 里的频道 | `Demo.txt` 是唯一白名单；支持标准名、别名、分组、台标、内联源 |
| 支持多元格式 | 输入自动识别 M3U/M3U8、TXT（`频道,地址` / `#genre#`）、JSON；输出 M3U + TXT + 分组文件 |
| 增量更新 | `state/library.json` 保存频道源库；上一轮可用源参与下一轮选择，抓取失败不会清空列表 |
| 大幅减少测速请求 | 成功缓存 18h、失败缓存 30min；同一 URL 只测一次；每频道候选上限 8；单轮全局预算 450 |
| 自动清理失效源 | 连续失败 2 次即从输出移除并隔离 24h，之后才允许重试；长期无活动的记录自动修剪 |
| 频道别名归一化 | `CCTV-1`/`中央一套`/`CCTV1HD`/`CCTV-01` 等统一为 `CCTV1`；卫视高清/4K 后缀归一 |
| 消除重复频道 | 标准名唯一 + `(频道, URL)` 去重 + URL 规范化去重 |
| 统一分组和台标 | 央视/卫视/地方/港澳台/体育/影视/新闻/少儿/纪录片/音乐/其他；统一台标模板 |
| 提升播放列表质量 | 流畅度评分排序（持续码率/抖动/停顿/首帧/历史可靠性）、HLS 清晰度与声明码率识别、每频道最多 3 条 |
| 可配置化 | 全部行为集中在 `config.toml`，无需改代码 |
| 大幅减少列表体积 | 默认每频道 3 条；无更新时间冗余条目、无失效源、无重复 URL |
| 提升播放体验 | 每个频道第一条即当前网络下**最不容易卡**的源，而不是"瞬间测速最快"的源 |
| 节省测速资源 | 成功缓存、失败退避、按需复测、并发/单域限制、全局预算 |
| 按需分组输出 | `output.per_group` + `output.per_group_formats`，或 `--groups 央视频道` |
| 频道排序优化 | 分组顺序 → `Demo.txt` 顺序 → CCTV 数字/卫视自然顺序 |
| 增量输出 | 文件内容哈希比较，内容不变不重写；GitHub 只在有差异时提交 |
| 末尾更新时间 | 播放列表末尾追加 `更新时间` 分组，附带时间戳和 `http://123.MP4` |

## 工作流

```text
Demo.txt (白名单/别名/内联源)
        +
sources/*.m3u|txt|json + 远程播放列表 + 可选酒店源扫描
        ↓
统一频道名 → 过滤白名单 → 分组/台标 → URL 规范化 → 去重
        ↓
合并上一轮 library.json（增量）
        ↓
只挑少量候选 URL 测速：缓存命中则跳过，失败退避，全局预算
        ↓
按流畅度评分排序 → 清理失效 → 每频道保留 Top N
        ↓
dist/playlist.m3u + dist/iptv.txt + dist/groups/*.m3u
        ↓
内容哈希比较，只有变化的文件才落盘/提交
```

## 快速开始

要求 Python 3.11+（使用 `tomllib`）。

```bash
git clone <你的仓库>
cd <你的仓库>

# 1) 按需修改 Demo.txt（白名单）
# 2) 按需修改 config.toml
# 3) 本地运行
python ITVlist.py --config config.toml
```

首次运行会：

1. 解析 `Demo.txt`；
2. 读取 `sources/`、远程播放列表，以及（默认开启的）酒店源扫描；
3. 对候选源做有限测速；
4. 生成 `dist/`、根目录 `itvlist.txt` 和 `speed_report.txt`；
5. 把测速缓存和源库写入 `state/`。

没有可用源时不会报错退出，方便定时任务等待下一轮恢复；需要 CI 在空列表时失败可加 `--strict`。

### 只预览、不测速

```bash
python ITVlist.py --config config.toml --no-probe
```

未验证的新源会按“可播放”加入预览，适合先检查抓取和筛选结果。

### 完全离线

```bash
python ITVlist.py --config config.toml --offline
```

不访问网络，只读取本地文件/已有缓存。

## GitHub Actions

仓库已包含 `.github/workflows/ITVlist.yml`：

- 每 3 小时运行一次（cron 为 UTC：`0 */3 * * *`）；
- 也可在 Actions 页面手动触发，支持：
  - `force_probe`：忽略缓存 TTL，强制复测；
  - `no_probe`：跳过测速预览；
  - `offline`：离线运行；
  - `groups`：只输出某些分组；
  - `max_probes`：临时覆盖单轮测速预算。
- 依次执行单元测试、构建、上传 artifact、提交增量结果；
- 只有 `dist/`、`state/`、`itvlist.txt`、`speed_report.txt` 有变化时才产生提交；
- 使用 `contents: write` 权限，不需要 PAT；如仓库开启了分支保护，需要允许 Actions 推送。

首次运行前建议在仓库 **Settings → Actions → General** 确认 Workflow permissions 为 “Read and write permissions”。

## Demo.txt 格式

`Demo.txt` 决定“保留哪些频道”。只要源的标准名或别名命中，就会保留；不在文件中的频道会被过滤。文件内容也兼容 M3U/JSON（会先按对应格式解析，再作为白名单）。

```text
# 每行一个频道；后三项可省略
标准频道名 | 别名1;别名2 | 分组 | 台标URL

# 例：
CCTV1 | CCTV-1;中央一套;中央1台 | 央视频道
湖南卫视 | 湖南卫视高清;湖南卫视4K | 卫视频道
大象新闻 | 河南大象新闻 | 地方频道
```

也支持直接写源（同时加入白名单）：

```text
CCTV1,http://example.com/live/cctv1.m3u8
CCTV1 | http://example.com/live/cctv1.m3u8
```

也支持用 `#genre#` 划分后续分组：

```text
央视频道,#genre#
CCTV1
CCTV2
卫视频道,#genre#
湖南卫视
```

注意：

- 别名用分号 `;` 或逗号分隔均可；
- 第 2 列是别名，第 3 列是分组，第 4 列是台标；不要把台标写到第 2 列，否则会被当成源；
- 标准名会被归一化（`CCTV-01` → `CCTV1`），重复标准名自动合并别名；
- 默认白名单包含 CCTV1–17、36 个卫视、大象新闻，共 54 个频道。

## 输入：多元格式

### 本地文件/目录

`config.toml`：

```toml
[sources]
files = ["sources", "extra/demo.m3u"]
```

支持 `.m3u`、`.m3u8`、`.txt`、`.text`、`.json`。`Demo.txt` 不会被当作普通源文件重复读取。

M3U：

```m3u
#EXTM3U
#EXTINF:-1 tvg-id="CCTV-1" tvg-name="CCTV1" tvg-logo="https://logo/1.png" group-title="央视频道",CCTV1
http://example.com/live/cctv1.m3u8
```

解析器会保留 `#EXTVLCOPT:http-user-agent=...` / `http-referrer=...`，测速时使用，并在输出 M3U 中回写（可用 `output.include_vlc_options = false` 关闭）。

TXT：

```text
央视频道,#genre#
CCTV1,http://example.com/live/cctv1.m3u8
湖南卫视|http://example.com/live/hunan.m3u8
```

JSON（支持 `data` 列表、频道对象列表、频道名→URL 映射）：

```json
{"data":[{"name":"CCTV1","url":"http://example.com/live/cctv1.m3u8"}]}
```

### 远程播放列表

```toml
[sources]
playlists = [
  "https://example.com/playlist.m3u",
  "https://example.com/channels.json",
]
```

自动识别格式；下载体积上限 `max_bytes`，超时 `fetch_timeout`。

### 酒店源扫描（可选）

保留原 zubo 的酒店 JSON API 扫描，但改成 **每个候选 API 只请求一次**（原逻辑是先探测再下载）。默认开启，并做了配额限制：

```toml
[sources.zubo]
enabled = true
interval_hours = 6.0          # 扫描结果复用 6 小时
scan_concurrency = 120
scan_timeout = 1.5
max_scan_urls = 12000
```

如果不需要酒店源，改为 `enabled = false`，只用本地/远程播放列表即可。

## 频道归一化、去重、分组、排序

### 归一化

- CCTV：`CCTV-1`、`CCTV 1`、`CCTV01`、`CCTV-01`、`cctv1HD`、`CCTV1 综合`、`中央一台`、`中央一套` → `CCTV1`；
- `CCTV-5+`、`CCTV5plus`、`CCTV5+体育赛事` → `CCTV5+`；
- `CCTV-4K` / `CCTV4K超高清` → `CCTV4K`，不会被误判为 `CCTV4`；
- 卫视：`湖南卫视高清`、`湖南卫视4K`、`湖南 卫视` → `湖南卫视`；
- 保留独立频道：`凤凰卫视中文台`、`翡翠台` 不会被错误合并；
- 未知频道保留清洗后的名字（去掉画质/直播/测试等后缀），不会直接丢弃。

### 去重

- 频道：归一化后的标准名唯一；
- 源：`(标准频道, canonical URL)` 唯一；
- URL 规范化会统一 host 大小写、默认端口、片段、`utm_*` 参数，但不重排业务 query，避免破坏签名地址。

### 分组

默认分组顺序：

```text
央视频道 → 卫视频道 → 地方频道 → 港澳台 → 体育 → 影视 → 新闻 → 少儿 → 纪录片 → 音乐 → 其他
```

`Demo.txt` 中的分组优先，未填写时自动推断。分组名可用 `config.toml` 的 `output.group_order` 调整。

### 排序

1. 先按分组顺序；
2. 组内按 `Demo.txt` 中的书写顺序；
3. 未在 Demo 中的频道按 CCTV 数字、卫视自然顺序兜底；
4. 同频道内按 **流畅度评分降序**（见下节），同分时再按余量 → 首帧延迟 → 清晰度 → 来源优先级 → URL。

旧版本按「一次 128 KB 短突发的峰值速度」排序，结果经常把"开局爆发很快、随后抖动或抖动式限速"的源排在第一，播放器默认选它就卡。现在默认改为流畅度排序。

### 流畅度排序（默认）

每次探测不再只记一个峰值速度，而是把 384 KB 采样按 16 KB 窗口读取，记录每个窗口的到达时间，据此计算 5 个分量：

| 分量 | 权重 | 含义 |
| --- | --- | --- |
| `speed` 码率充足度 | 0.34 | 时间加权**中位**速率 ÷ 该源真正需要的码率（HLS 变体声明码率 / 清晰度对应码率 / 分片实时下载能力取最严）。达到需求的 1.2 倍即满分——**再快也不会更流畅** |
| `stability` 抖动 | 0.30 | 把读取时间轴重采样成固定桶后，桶速率的变异系数 CV；`1/(1+2·CV)` |
| `stall` 停顿 | 0.16 | 采样期内「≥0.35s 没有收到字节」的次数；`1/(1+停顿次数)` |
| `latency` 首帧 | 0.08 | 首字节耗时（HLS 含主列表耗时）；`1/(1+TTFB秒)` |
| `reliability` 历史 | 0.12 | 跨轮次成功率的指数滑动平均（成功 +0.35、失败 −0.6，需重试才成功按半次计），偶发失败会被记住 |

`评分 = 100 × Σ 权重 × 分量`，再乘以「码率可行性」系数：持续码率低于需求 0.8 倍时按缺口扣分，完全跟不上时最多保留 25%——不够码率的源再平稳也一定会重缓冲。

排序时评分按 `ranking.score_bucket`（默认 1 分）分档，避免测量噪声让播放列表每轮换位。

想恢复旧行为，把配置改成：

```toml
[ranking]
mode = "speed"
```

只想过滤掉抖得厉害的源：

```toml
[ranking]
min_smooth_score = 55   # 低于 55 分的源不输出；0 = 不淘汰
```

## 输出

### `dist/playlist.m3u`

推荐播放器订阅地址：

```text
https://raw.githubusercontent.com/<user>/<repo>/<branch>/dist/playlist.m3u
```

示例：

```m3u
#EXTM3U x-tvg-url="https://epg.example.com/epg.xml.gz"
#EXTINF:-1 tvg-id="CCTV-1" tvg-name="CCTV1" tvg-logo="https://.../CCTV-1.png" group-title="央视频道",CCTV1
http://最快源/cctv1.m3u8
#EXTINF:-1 tvg-id="CCTV-1" tvg-name="CCTV1" tvg-logo="https://.../CCTV-1.png" group-title="央视频道",CCTV1
http://次快源/cctv1.m3u8
#EXTINF:-1 tvg-id="更新时间" tvg-name="26/10/25-09:11:18" group-title="更新时间",26/10/25-09:11:18
http://123.MP4
```

### `dist/iptv.txt` / `itvlist.txt`

兼容老订阅格式：

```text
更新时间: 26/10/24-20:00:00（北京时间）

央视频道,#genre#
CCTV1,http://最快源/cctv1.m3u8
CCTV1,http://次快源/cctv1.m3u8
卫视频道,#genre#
湖南卫视,http://...

更新时间,#genre#
26/10/25-09:11:18,http://123.MP4
```

根目录 `itvlist.txt` 与 `dist/iptv.txt` 内容一致，方便旧订阅地址继续使用；可在配置中关闭。

末尾的 `更新时间` 条目由以下配置控制；分组文件（`dist/groups/`）不会重复追加该条目：

```toml
[output]
append_update_entry = true
update_entry_url = "http://123.MP4"
```

### `dist/groups/*.m3u`

按分组拆分，适合只要央视/只要卫视的播放器：

```toml
[output]
per_group = true
per_group_formats = ["m3u"]
group_dir = "groups"
```

只输出部分分组：

```bash
python ITVlist.py --groups 央视频道,卫视频道
```

或配置：

```toml
[filters]
include_groups = ["央视频道", "卫视频道"]
```

### 其他文件

- `speed_report.txt`：每个输出源的流畅度评分、持续码率（中位）、平均码率、抖动、停顿次数、首帧、采样量、HLS 清晰度；不参与订阅。
- `dist/manifest.json`：构建统计、分组计数、文件列表；`generated_at` 使用播放列表最后更新时间，避免“只改时间”的 diff。
- `state/probe_cache.json`：测速缓存。
- `state/library.json`：频道源库/健康状态。
- `state/run_state.json`：上次运行、扫描时间。
- `state/output_hashes.json`：内容哈希，用于增量写入。

## 测速策略（资源节省的核心）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `probe.enabled` | `true` | 总开关 |
| `probe.concurrency` | `32` | 同时测速 URL 数 |
| `probe.per_host` | `4` | 单域名并发，避免打挂源站 |
| `probe.sample_bytes` | `393216` | 每源最多采样 384 KB（16 KB 窗口 ≈ 24 个样本） |
| `probe.sample_seconds` | `3.0` | 采样时间预算，先到先停 |
| `probe.stall_seconds` | `0.35` | 超过该时长的空档记为一次停顿 |
| `probe.min_sample_bytes` | `16384` | 达到 16 KB 才计“实测速度” |
| `probe.success_ttl_hours` | `18` | 成功源缓存 18 小时 |
| `probe.failure_ttl_minutes` | `30` | 失败源 30 分钟内不重复打 |
| `probe.stale_after_hours` | `96` | 超过 96h 未复测成功则不输出 |
| `probe.retain_hours` | `168` | 源消失后库中最多保留 7 天 |
| `probe.drop_after_failures` | `2` | 连续失败 2 次清理 |
| `probe.quarantine_hours` | `24` | 隔离 24h 后才允许重试 |
| `probe.max_candidates_per_channel` | `8` | 每频道候选上限 |
| `probe.max_new_per_channel` | `2` | 每轮每频道额外尝试的新源数 |
| `probe.max_probes_per_run` | `450` | 单轮全局测速预算 |

运行逻辑：

1. 同一 canonical URL 只测一次；多频道共享同一地址时复用结果；
2. 缓存未过期直接使用，不产生请求；
3. 每轮优先给“没有可用源”的频道测速，再轮询其他频道，保证公平；
4. 已缓存足够的频道只花少量名额发现新源；
5. 失败源进入退避/隔离，连续失败 2 次后从输出和库中清理；
6. 过期成功源在复测失败前仍可输出（最多 `stale_after_hours`），避免一轮网络抖动导致频道消失。

采样窗口比旧版大（384 KB / 最多 3s）：只有拿到足够多的时间窗口，才能区分"稳定 3 Mbps"和"瞬间 9 Mbps 然后限速"。单个源的采样上限仍受 `probe.timeout` / `probe.max_seconds` 约束，单轮总预算由 `max_probes_per_run` 控制。

### 为什么仍然需要测速？

GitHub runner 多在海外，测出来的是 runner 到源的链路质量，不等于你家里的网络。若希望排序完全贴合自己的网络：

- 用国内 self-hosted runner；
- 或本地运行 `python ITVlist.py`（把 `state/` 一起带过去，缓存可用）；
- 或把 `max_sources_per_channel` 调大，让播放器自行切换；
- 只想看测速峰值、不看流畅度时，用 `[ranking] mode = "speed"`。

## 可配置化

全部配置在 `config.toml`。常用项：

| 配置 | 作用 |
| --- | --- |
| `app.demo_file` | 白名单文件 |
| `app.state_dir` / `output_dir` | 状态/输出目录 |
| `filters.require_demo` | 是否只保留 Demo 频道 |
| `filters.include_groups` | 只处理/输出指定分组 |
| `output.max_sources_per_channel` | 每频道保留源数（默认 3，直接决定体积） |
| `output.formats` | `["m3u"]` 可只输出 M3U |
| `output.legacy_txt` | 是否写根目录 `itvlist.txt` |
| `output.per_group` | 是否拆分分组文件 |
| `output.include_vlc_options` | 是否回写 `#EXTVLCOPT` UA/Referer |
| `output.append_update_entry` | 是否在末尾追加 `更新时间` 分组 |
| `output.update_entry_url` | 末尾条目的地址，默认 `http://123.MP4` |
| `output.epg_url` | EPG 地址，写入 M3U 头 |
| `output.tvg_id_style` | `dash`（CCTV-1）或 `name`（CCTV1） |
| `logo.template` | 台标模板，`{id}`/`{name}` 占位 |
| `ranking.mode` | `smoothness`（默认，按流畅度）或 `speed`（旧的速度排序） |
| `ranking.w_*` | 五个分量权重（speed/stability/stall/latency/reliability） |
| `ranking.min_smooth_score` | 流畅度低于该分数的源不输出（默认关闭） |
| `ranking.required_kbps` | 覆盖"清晰度 → 需求码率"表 |
| `sources.zubo.enabled` | 是否启用酒店源扫描 |
| `probe.force`（CLI） | 忽略 TTL 强制复测 |

修改配置后重新运行即可，不需要改 `itvpipe/` 中的代码。

## 命令行

```bash
python ITVlist.py [选项]

--config PATH        配置文件，默认 config.toml
--demo PATH          覆盖 Demo.txt
--output-dir PATH    覆盖输出目录
--groups A,B         只输出指定分组
--no-probe           跳过测速预览
--force-probe        忽略缓存 TTL
--offline            不访问网络
--max-probes N       覆盖单轮测速预算
--dry-run            只计算，不写文件
--strict             没有频道时返回非零
--json               额外打印机器可读摘要
--validate           只校验配置
--verbose, -v        调试日志
```

## 测试

```bash
python -m unittest discover -s tests -v

# 可选：运行本机回环 HTTP 的 HLS/分片集成测试
IPTV_RUN_LOCAL_HTTP=1 python -m unittest discover -s tests -v
```

测试覆盖：

- 频道别名/画质后缀/4K/5+/港澳台名称归一化；
- Demo/M3U/TXT/JSON 解析与去重；
- URL canonical 化、屏蔽规则；
- 缓存命中、测速预算、失败清理、源排序；
- 流畅度评分：到达时间序列 → 持续速率/抖动/停顿、码率充足度、分片实时性、
  历史可靠性、`mode="speed"` 回滚、分档与确定性排序；
- M3U/TXT/分组输出与增量写入；
- 本地 HTTP 服务器的 HLS 主列表/媒体列表/分片测速、404、并发去重。

GitHub Actions 在构建前也会跑同一套测试。

## 目录结构

```text
ITVlist.py                  # CLI + 旧 API 兼容
config.toml                 # 配置
Demo.txt                   # 频道白名单
itvpipe/
  config.py                 # TOML 配置
  models.py                 # 数据模型 + 排序键
  smooth.py                 # 流畅度评分（持续速率/抖动/停顿/可靠性）
  normalize.py              # 别名/分组/URL 归一化
  parsers.py                # M3U/TXT/JSON/Demo 解析
  probe.py                  # 标准库 HTTP 测速
  state.py                  # 缓存/源库/增量写入
  pipeline.py               # 抓取→去重→测速→选择
  output.py                 # M3U/TXT/分组/报告
  zubo.py                   # 可选酒店源扫描
.github/workflows/ITVlist.yml
scripts/summarize.py
tests/
```

## 常见问题

**输出为空？**

1. 先跑 `python ITVlist.py --no-probe --dry-run --json` 看抓到了多少源；
2. 检查 `Demo.txt` 是否包含对应频道或别名；
3. 检查源是否在 `filters.exclude_url_keywords` 中被屏蔽；
4. 首次运行酒店源扫描可能较慢，可查看日志中的“酒店源扫描进度”。

**GitHub Actions 太慢 / 测速太多？**

- 降低 `max_probes_per_run`（例如 200）；
- 降低 `max_new_per_channel`（例如 1）；
- 降低 `probe.sample_seconds`（例如 1.5）比降低 `sample_bytes` 更安全：窗口太少就算不出抖动；
- 确实要降 `sample_bytes` 时别低于 `131072`（8 个 16 KB 窗口）；
- 关闭 `sources.zubo.enabled`，改用固定远程播放列表；
- 提高 `success_ttl_hours` 到 24/48。

**升级后排序看起来没变化？**

旧版 `state/library.json` 里没有抖动/停顿/中位速率，评分会自动退回"平均测速"。跑一次
`python ITVlist.py --force-probe` 让所有源重新采样后，流畅度排序才会完全生效。

**想每天只跑一次？**

把 workflow 的 cron 改为 `0 18 * * *`（UTC 18:00 = 北京时间 02:00），同时把 `success_ttl_hours` 调到 20 以上。

**台标不显示？**

- 确认 `logo.enabled = true`；
- 模板中的 `{id}` 对 CCTV 会生成 `CCTV-1`；也可以改用 `{name}`；
- 部分播放器不加载 HTTPS 台标，或需要允许明文 HTTP；
- 可在 `Demo.txt` 第 4 列或 `[logo.overrides]` 单独指定。

**字幕/EPG 对不上？**

- 设置 `output.epg_url`；
- `tvg-id_style = "dash"` 对应常见的 `CCTV-1` EPG ID；
- 卫视台标/EPG 使用中文台名，需与 EPG 源一致。

**Actions 没有提交？**

这是正常的：内容哈希未变化时不会重写文件，也就没有 diff。可查看 artifact 或运行日志中的“变更文件: 0”。

**旧订阅地址还能用吗？**

可以。默认继续写根目录 `itvlist.txt`，格式与旧项目一致；新订阅建议改用 `dist/playlist.m3u`。

## 说明

- 本项目只做播放列表整理/测速，不托管、不转播任何内容；请仅使用你有权访问的源，并遵守当地法律与源站条款。
- 酒店源扫描会访问第三方地址；如不需要请关闭。
- `state/` 建议随仓库提交，否则每次都会重新测速；它只包含 URL、速度和健康状态，不包含账号密码。

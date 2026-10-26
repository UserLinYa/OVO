"""Channel alias normalisation, grouping, logos and URL canonicalisation."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from .models import ChannelSpec, DEFAULT_GROUP_ORDER

# ---------------------------------------------------------------------------
# Channel metadata
# ---------------------------------------------------------------------------

CCTV_PROGRAMS = {
    1: "综合",
    2: "财经",
    3: "综艺",
    4: "中文国际",
    5: "体育",
    6: "电影",
    7: "国防军事",
    8: "电视剧",
    9: "纪录",
    10: "科教",
    11: "戏曲",
    12: "社会与法",
    13: "新闻",
    14: "少儿",
    15: "音乐",
    16: "奥林匹克",
    17: "农业农村",
}

SATELLITE_CHANNELS = [
    "安徽卫视",
    "北京卫视",
    "东方卫视",
    "东南卫视",
    "广东卫视",
    "广西卫视",
    "贵州卫视",
    "海南卫视",
    "河北卫视",
    "河南卫视",
    "湖北卫视",
    "湖南卫视",
    "吉林卫视",
    "江苏卫视",
    "江西卫视",
    "辽宁卫视",
    "宁夏卫视",
    "青海卫视",
    "山东卫视",
    "山西卫视",
    "陕西卫视",
    "深圳卫视",
    "四川卫视",
    "天津卫视",
    "云南卫视",
    "浙江卫视",
    "重庆卫视",
    "甘肃卫视",
    "西藏卫视",
    "香港卫视",
    "新疆卫视",
    "三沙卫视",
    "兵团卫视",
    "延边卫视",
    "黑龙江卫视",
    "内蒙古卫视",
]

LOCAL_CHANNELS = [
    "大象新闻",
]

DEFAULT_CHANNEL_NAMES = (
    [f"CCTV{i}" for i in range(1, 18)] + SATELLITE_CHANNELS + LOCAL_CHANNELS
)

# Names that cannot be derived mechanically from the canonical name.
EXTRA_ALIASES: Dict[str, List[str]] = {
    "CCTV1": ["CCTV-1", "CCTV 1", "CCTV01", "CCTV-01", "中央一台", "中央1台", "中央一套"],
    "CCTV2": ["CCTV-2", "CCTV 2", "CCTV02", "CCTV-02", "中央二台", "中央2台", "中央二套"],
    "CCTV3": ["CCTV-3", "CCTV 3", "CCTV03", "CCTV-03", "中央三台", "中央3台", "中央三套"],
    "CCTV4": ["CCTV-4", "CCTV 4", "CCTV04", "CCTV-04", "中央四台", "中央4台", "中央四套", "CCTV4中文国际", "CCTV-4中文国际"],
    "CCTV5": ["CCTV-5", "CCTV 5", "CCTV05", "CCTV-05", "中央五台", "中央5台", "中央五套"],
    "CCTV5+": ["CCTV-5+", "CCTV5+", "CCTV5plus", "CCTV5+体育赛事", "CCTV-5+体育赛事", "CCTV5+体育"],
    "CCTV6": ["CCTV-6", "CCTV 6", "CCTV06", "CCTV-06", "中央六台", "中央6台", "中央六套", "CCTV6电影"],
    "CCTV7": ["CCTV-7", "CCTV 7", "CCTV07", "CCTV-07", "中央七台", "中央7台", "中央七套", "CCTV7国防军事", "CCTV-7国防军事"],
    "CCTV8": ["CCTV-8", "CCTV 8", "CCTV08", "CCTV-08", "中央八台", "中央8台", "中央八套", "CCTV8电视剧"],
    "CCTV9": ["CCTV-9", "CCTV 9", "CCTV09", "CCTV-09", "中央九台", "中央9台", "中央九套", "CCTV9纪录", "CCTV9记录"],
    "CCTV10": ["CCTV-10", "CCTV 10", "中央十台", "中央10台", "中央十套", "CCTV10科教"],
    "CCTV11": ["CCTV-11", "CCTV 11", "中央十一台", "中央11台", "中央十一套", "CCTV11戏曲"],
    "CCTV12": ["CCTV-12", "CCTV 12", "中央十二台", "中央12台", "中央十二套", "CCTV12社会与法"],
    "CCTV13": ["CCTV-13", "CCTV 13", "中央十三台", "中央13台", "中央十三套", "CCTV13新闻"],
    "CCTV14": ["CCTV-14", "CCTV 14", "中央十四台", "中央14台", "中央十四套", "CCTV14少儿"],
    "CCTV15": ["CCTV-15", "CCTV 15", "中央十五台", "中央15台", "中央十五套", "CCTV15音乐"],
    "CCTV16": ["CCTV-16", "CCTV 16", "中央十六台", "中央16台", "中央十六套", "CCTV16奥林匹克", "CCTV-16奥林匹克"],
    "CCTV17": ["CCTV-17", "CCTV 17", "中央十七台", "中央17台", "中央十七套", "CCTV17农业农村", "CCTV-17农业农村"],
    "CCTV4K": ["CCTV-4K", "CCTV 4K", "CCTV4K超高清", "CCTV-4K超高清"],
    "CCTV8K": ["CCTV-8K", "CCTV 8K", "CCTV8K超高清"],
}

CHANNEL_GROUPS: Dict[str, str] = {}
for _i in range(1, 18):
    CHANNEL_GROUPS[f"CCTV{_i}"] = "央视频道"
CHANNEL_GROUPS.update(
    {
        "CCTV4K": "央视频道",
        "CCTV8K": "央视频道",
        "CCTV5+": "央视频道",
        "CGTN": "央视频道",
        "CGTN纪录": "纪录片",
    }
)
for _name in SATELLITE_CHANNELS:
    CHANNEL_GROUPS[_name] = "卫视频道"
for _name in LOCAL_CHANNELS:
    CHANNEL_GROUPS[_name] = "地方频道"

EPG_ID_OVERRIDES = {
    "CCTV1": "CCTV-1",
    "CCTV2": "CCTV-2",
    "CCTV3": "CCTV-3",
    "CCTV4": "CCTV-4",
    "CCTV5": "CCTV-5",
    "CCTV5+": "CCTV-5+",
    "CCTV6": "CCTV-6",
    "CCTV7": "CCTV-7",
    "CCTV8": "CCTV-8",
    "CCTV9": "CCTV-9",
    "CCTV10": "CCTV-10",
    "CCTV11": "CCTV-11",
    "CCTV12": "CCTV-12",
    "CCTV13": "CCTV-13",
    "CCTV14": "CCTV-14",
    "CCTV15": "CCTV-15",
    "CCTV16": "CCTV-16",
    "CCTV17": "CCTV-17",
    "CCTV4K": "CCTV-4K",
    "CCTV8K": "CCTV-8K",
}

GROUP_ALIASES = {
    "央视": "央视频道",
    "cctv": "央视频道",
    "中央": "央视频道",
    "央视频道": "央视频道",
    "卫视": "卫视频道",
    "卫视频道": "卫视频道",
    "地方": "地方频道",
    "地方频道": "地方频道",
    "本地": "地方频道",
    "港澳台": "港澳台",
    "港台": "港澳台",
    "台湾": "港澳台",
    "香港": "港澳台",
    "澳门": "港澳台",
    "体育": "体育",
    "体育频道": "体育",
    "影视": "影视",
    "电影": "影视",
    "影视剧": "影视",
    "新闻": "新闻",
    "新闻频道": "新闻",
    "少儿": "少儿",
    "少儿频道": "少儿",
    "卡通": "少儿",
    "动漫": "少儿",
    "纪录": "纪录片",
    "记录": "纪录片",
    "纪录片": "纪录片",
    "纪实": "纪录片",
    "音乐": "音乐",
    "音乐频道": "音乐",
    "其他": "其他",
    "其它": "其他",
    "全部频道": "央视频道",
}

HK_TW_MARKERS = (
    "凤凰",
    "tvb",
    "翡翠台",
    "明珠台",
    "无线",
    "亚视",
    "atv",
    "now",
    "viutv",
    "hoy",
    "澳视",
    "澳门",
    "莲花",
    "中天",
    "东森",
    "民视",
    "台视",
    "华视",
    "中视",
    "三立",
    "八大",
    "纬来",
    "龙华",
    "靖天",
    "无线新闻",
)

SPORT_MARKERS = ("体育", "足球", "篮球", "网球", "赛车", "赛事", "高尔夫", "搏击")
MOVIE_MARKERS = ("电影", "影视", "剧场", "影院", "电视剧", "美剧", "韩剧")
NEWS_MARKERS = ("新闻", "资讯", "财经", "法制", "社会与法")
KIDS_MARKERS = ("少儿", "卡通", "动漫", "动画", "儿童")
DOC_MARKERS = ("纪录", "记录", "纪实", "探索", "地理")
MUSIC_MARKERS = ("音乐", "歌曲", "演唱会", "mv")

LOCAL_MARKERS = (
    "北京",
    "上海",
    "天津",
    "重庆",
    "河北",
    "山西",
    "辽宁",
    "吉林",
    "黑龙江",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "四川",
    "贵州",
    "云南",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
    "西藏",
    "内蒙古",
    "深圳",
    "广州",
    "杭州",
    "南京",
    "武汉",
    "成都",
    "苏州",
    "厦门",
    "青岛",
    "大连",
    "宁波",
    "无锡",
    "福州",
    "济南",
    "郑州",
    "长沙",
    "合肥",
    "昆明",
    "贵阳",
    "南宁",
    "海口",
    "兰州",
    "西宁",
    "银川",
    "乌鲁木齐",
    "拉萨",
    "石家庄",
    "太原",
    "沈阳",
    "长春",
    "哈尔滨",
    "南昌",
    "呼和浩特",
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\u2b00-\u2bff"
    "\ufe0e\ufe0f"
    "\u200d"
    "]"
)
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_SPACE_RE = re.compile(r"\s+")
_QUALITY_TOKEN = (
    r"(?:hd|fhd|qhd|uhd|4k|8k|hdr(?:10)?|h\.?265|h\.?264|hevc|avc|"
    r"超高清|高清|超清|蓝光|标清|原画|流畅|极速|"
    r"高清版|直播|备用|测试|试看|线路\d*|"
    r"[456]0帧|[456]0fps|25fps|30fps|50fps|60fps)"
)
_QUALITY_SUFFIX_RE = re.compile(
    rf"(?:[\s\-_·:：/\\|]*(?:{_QUALITY_TOKEN}))+\s*$", re.IGNORECASE
)
_QUALITY_BRACKET_RE = re.compile(
    rf"[\(\[（【][^\)\]）】]{{0,40}}(?:{_QUALITY_TOKEN})[^\)\]）】]{{0,40}}[\)\]）】]",
    re.IGNORECASE,
)

_CCTV_RE = re.compile(
    r"(?i)(?:cctv|cctv[\s\-_]*)(?:\s*[-_ ]\s*)?0*(\d{1,2})\s*(?:\+|plus|p)?"
)
_CCTV_PLUS_RE = re.compile(r"(?i)(?:cctv)[\s\-_]*5\s*(?:\+|plus|p)")
_CCTV_4K_RE = re.compile(r"(?i)^cctv[\s\-_]*4k.*$")
_CCTV_8K_RE = re.compile(r"(?i)^cctv[\s\-_]*8k.*$")
_CENTRAL_NUM_RE = re.compile(
    r"中央(?:电视台|广播电视总台)?\s*第?\s*([一二三四五六七八九十百零〇两\d]+)\s*(?:套|台|频道)?"
)
_CENTRAL_PLUS_RE = re.compile(r"cctv[\s\-_]*5\s*(?:\+|plus|p)", re.IGNORECASE)
_SATELLITE_RE = re.compile(r"^([\u4e00-\u9fff]{2,12}卫视)")
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _chinese_number(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        tens, _, ones = value.partition("十")
        tens_value = _CN_DIGITS.get(tens, 1) if tens else 1
        ones_value = _CN_DIGITS.get(ones, 0) if ones else 0
        return tens_value * 10 + ones_value
    if len(value) == 1:
        return _CN_DIGITS.get(value)
    return None


def _strip_quality(text: str) -> str:
    previous = None
    current = text
    while current != previous:
        previous = current
        current = _QUALITY_BRACKET_RE.sub("", current)
        current = _QUALITY_SUFFIX_RE.sub("", current)
        current = re.sub(
            rf"[\s\-_·:：/\\|]*(?:{_QUALITY_TOKEN})\s*$",
            "",
            current,
            flags=re.IGNORECASE,
        )
        current = current.strip(" \t-_·:：/\\|")
    return current


def _clean_display(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = text.replace("＋", "+").replace("－", "-").replace("—", "-").replace("–", "-")
    text = text.replace("，", ",").replace("；", ";").replace("｜", "|")
    text = _SPACE_RE.sub(" ", text).strip()
    text = text.strip(" \t\r\n\"'`“”‘’")
    # 去掉频道名外层的装饰括号。
    text = re.sub(r"^[\[【(（]\s*([^\]】)）]+?)\s*[\]】)）]$", r"\1", text)
    return text


def normalize_channel_name(name: str) -> str:
    """Return a canonical channel name.

    Handles common CCTV spellings, Chinese numerals, HD/4K suffixes, satellite
    names and decorative prefixes/suffixes. Unknown names are preserved with
    quality tokens removed instead of being discarded.
    """

    text = _clean_display(name)
    if not text:
        return ""

    compact = re.sub(r"\s+", "", text)
    compact_no_quality = re.sub(
        rf"(?:[\-_·]*(?:{_QUALITY_TOKEN}))+$", "", compact, flags=re.IGNORECASE
    )

    # 4K/8K 本身就是频道名的一部分，必须在通用画质清洗之前判断。
    if _CCTV_4K_RE.match(compact) or _CCTV_4K_RE.match(compact_no_quality):
        return "CCTV4K"
    if _CCTV_8K_RE.match(compact) or _CCTV_8K_RE.match(compact_no_quality):
        return "CCTV8K"

    # CCTV5+ 要在普通 CCTV 数字之前判断，否则 "+" 会被忽略。
    if _CCTV_PLUS_RE.search(compact_no_quality):
        return "CCTV5+"

    match = _CCTV_RE.search(compact_no_quality)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 17:
            return f"CCTV{number}"

    central = _CENTRAL_NUM_RE.search(compact_no_quality)
    if central:
        number = _chinese_number(central.group(1))
        if number is not None and 1 <= number <= 17:
            return f"CCTV{number}"
        if number == 5 and "+" in compact_no_quality:
            return "CCTV5+"

    text = _strip_quality(text)
    text = _clean_display(text)
    if not text:
        return ""

    # "湖南 卫视" -> "湖南卫视"；但保留英文单词间的空格。
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s*[-_·]\s*", "-", text)
    text = text.strip(" -_·:：/\\|")

    sat = _SATELLITE_RE.match(text)
    if sat and sat.end() == len(text):
        # 仅 "湖南卫视" 这类完整名称归一；"凤凰卫视中文台" 必须保留为独立频道。
        return sat.group(1)

    if text.endswith("频道") and len(text) > 2:
        candidate = text[:-2].strip(" -_·")
        if candidate:
            text = candidate

    return text or _clean_display(name)


def alias_key(name: str) -> str:
    """Aggressive key used for alias lookups (spaces/dashes/case ignored)."""

    text = normalize_channel_name(name)
    text = unicodedata.normalize("NFKC", text).lower()
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[\s\-_·:：/\\|,，;；()（）\[\]【】]+", "", text)
    return text


def infer_group(name: str) -> str:
    text = name.lower()
    if name.startswith("CCTV") or name.startswith("CGTN") or "央视" in name:
        return "央视频道"
    # 凤凰/澳门等港澳台频道可能也带“卫视”字样，先判断。
    if "凤凰" in text or "澳门" in text:
        return "港澳台"
    if any(marker in text for marker in HK_TW_MARKERS) and not name.endswith("卫视"):
        return "港澳台"
    if name.endswith("卫视") or "卫视" in name:
        return "卫视频道"
    if any(marker in text for marker in HK_TW_MARKERS):
        return "港澳台"
    if any(marker in name for marker in SPORT_MARKERS):
        return "体育"
    if any(marker in name for marker in MOVIE_MARKERS):
        return "影视"
    if any(marker in name for marker in KIDS_MARKERS):
        return "少儿"
    if any(marker in name for marker in DOC_MARKERS):
        return "纪录片"
    if any(marker in name for marker in MUSIC_MARKERS):
        return "音乐"
    if any(marker in name for marker in NEWS_MARKERS):
        return "新闻"
    if any(marker in name for marker in LOCAL_MARKERS):
        return "地方频道"
    return "其他"


def normalize_group(group: str, channel: str = "") -> str:
    text = _clean_display(group)
    if not text:
        return CHANNEL_GROUPS.get(channel) or infer_group(channel)
    key = alias_key(text)
    if key in ("全部频道", "全部"):
        # 旧项目的“全部频道”不是有效分组，交给频道名推断。
        return ""
    for alias, canonical in GROUP_ALIASES.items():
        if key == alias_key(alias):
            return canonical
    # "📺央视频道" 等装饰性前缀/后缀。
    for canonical in DEFAULT_GROUP_ORDER:
        if canonical in text:
            return canonical
    return text


def epg_id_for(name: str, style: str = "dash") -> str:
    if style == "name":
        return name
    return EPG_ID_OVERRIDES.get(name, name)


def tvg_id_for(name: str, style: str = "dash") -> str:
    return epg_id_for(name, style)


def logo_for(name: str, spec: Optional[ChannelSpec], template: str, overrides: Dict[str, str]) -> str:
    if name in overrides:
        return overrides[name]
    if spec and spec.logo:
        return spec.logo
    if not template:
        return ""
    epg = epg_id_for(name, "dash")
    try:
        return template.format(id=quote(epg, safe="-._~+"), name=quote(name, safe="-._~+"))
    except (KeyError, ValueError):
        return ""


def build_alias_index(
    channels: Dict[str, ChannelSpec],
    extra_aliases: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, str]:
    """Build lookup key -> canonical name.

    Demo.txt aliases win over built-in aliases when they collide, because
    they are user-declared.
    """

    index: Dict[str, str] = {}

    def add(alias: str, canonical: str) -> None:
        key = alias_key(alias)
        if key and key not in index:
            index[key] = canonical

    for name in DEFAULT_CHANNEL_NAMES:
        add(name, name)
        add(name.replace("CCTV", "CCTV-"), name)
    for canonical, aliases in EXTRA_ALIASES.items():
        add(canonical, canonical)
        for alias in aliases:
            add(alias, canonical)
    for name in list(CHANNEL_GROUPS):
        add(name, name)
        add(name.replace("CCTV", "CCTV-"), name)
    if extra_aliases:
        for canonical, aliases in extra_aliases.items():
            add(canonical, canonical)
            for alias in aliases:
                add(alias, canonical)
    for spec in channels.values():
        add(spec.name, spec.name)
        for alias in spec.aliases:
            add(alias, spec.name)
    return index


def resolve_channel(raw_name: str, alias_index: Dict[str, str]) -> str:
    """Resolve a raw channel name to the canonical Demo/standard name.

    Unknown names are returned in normalised form; the caller decides whether
    to keep them based on ``filters.require_demo``.
    """

    normalised = normalize_channel_name(raw_name)
    if not normalised:
        return ""
    # Demo may define a non-standard canonical name (e.g. 大象新闻).
    if alias_index.get(alias_key(normalised)) == normalised:
        return normalised
    for candidate in (normalised, raw_name):
        key = alias_key(candidate)
        if key in alias_index:
            return alias_index[key]
    return normalised


def natural_channel_key(name: str) -> Tuple:
    """Natural sort key: CCTV1, CCTV2, ... CCTV17, then Chinese names."""

    match = re.match(r"^CCTV(\d+)(\+)?$", name, re.IGNORECASE)
    if match:
        return (0, int(match.group(1)), 1 if match.group(2) else 0, name)
    if name in SATELLITE_CHANNELS:
        return (1, SATELLITE_CHANNELS.index(name), 0, name)
    return (2, 0, 0, name)


# ---------------------------------------------------------------------------
# URL canonicalisation / filtering
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_PRIVATE_MULTICAST_PREFIXES = ("239.", "224.", "225.", "226.", "227.", "228.", "229.", "230.", "231.", "232.", "233.", "234.", "235.", "236.", "237.", "238.")
_REMOVE_QUERY_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm"}


def strip_url_options(url: str) -> str:
    """Remove VLC/Kodi pipe options from a URL (``url|User-Agent=...``)."""

    text = html.unescape((url or "").strip()).strip("\"'")
    # 播放地址中偶有未编码空格；先编码再提取，避免把 URL 截断。
    text = text.replace(" ", "%20")
    match = _URL_RE.search(text)
    if match:
        text = match.group(0)
        # VLC/Kodi 的 |User-Agent=... 选项不是 URL 的一部分。
        text = text.split("|", 1)[0]
    elif text.startswith("//"):
        text = "https:" + text
    return text.strip().rstrip("),;，；")


def canonical_url(url: str) -> str:
    """Stable key for duplicate detection; the display URL stays untouched."""

    text = strip_url_options(url)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    scheme = parts.scheme.lower()
    host = parts.hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return ""
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    query_items = []
    for key, value in re.findall(r"([^&=]+)=?([^&]*)", parts.query):
        if key.lower() in _REMOVE_QUERY_KEYS:
            continue
        query_items.append((key, value))
    query = "&".join(f"{key}={value}" if value else key for key, value in query_items)
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_playback_url(url: str) -> str:
    """URL to write/probe, with minimal escaping and no pipe options."""

    text = strip_url_options(url)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, query, ""))


def is_candidate_url(
    url: str,
    *,
    allowed_schemes: Optional[Iterable[str]] = None,
    blocked_keywords: Optional[Iterable[str]] = None,
    max_length: int = 4096,
) -> Tuple[bool, str]:
    text = normalize_playback_url(url)
    if not text:
        return False, "无效 URL"
    if len(text) > max_length:
        return False, "URL 过长"
    schemes = {s.lower() for s in (allowed_schemes or ("http", "https"))}
    parts = urlsplit(text)
    if parts.scheme.lower() not in schemes:
        return False, "不支持的协议"
    if not parts.hostname:
        return False, "缺少主机名"
    try:
        parts.port
    except ValueError:
        return False, "端口无效"
    host = parts.hostname.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return False, "本机地址"
    if any(host.startswith(prefix) for prefix in _PRIVATE_MULTICAST_PREFIXES):
        return False, "组播地址"
    suffix = parts.path.lower().rsplit(".", 1)[-1] if "." in parts.path.rsplit("/", 1)[-1] else ""
    if suffix in ("png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"):
        return False, "图片地址"
    keywords = blocked_keywords
    if keywords is None:
        keywords = ("/paiptv/", "/00/SNM/", "/00/CHANNEL")
    for keyword in keywords:
        if keyword and keyword.lower() in text.lower():
            return False, f"命中屏蔽词 {keyword}"
    return True, ""


def extract_url(text: str) -> str:
    match = _URL_RE.search((text or "").replace(" ", "%20"))
    return match.group(0) if match else ""


def infer_name_from_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "未知频道"
    segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
    segment = re.sub(r"\.(?:m3u8?|ts|flv|mp4|mkv|webm|mpd)$", "", segment, flags=re.IGNORECASE)
    if not segment:
        segment = parts.hostname or "未知频道"
    return segment.replace("_", " ").replace("-", " ").strip()


__all__ = [
    "CHANNEL_GROUPS",
    "DEFAULT_CHANNEL_NAMES",
    "EPG_ID_OVERRIDES",
    "EXTRA_ALIASES",
    "SATELLITE_CHANNELS",
    "alias_key",
    "build_alias_index",
    "canonical_url",
    "epg_id_for",
    "extract_url",
    "infer_group",
    "infer_name_from_url",
    "is_candidate_url",
    "logo_for",
    "natural_channel_key",
    "normalize_channel_name",
    "normalize_group",
    "normalize_playback_url",
    "resolve_channel",
    "strip_url_options",
    "tvg_id_for",
]

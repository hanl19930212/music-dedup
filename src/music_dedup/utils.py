"""工具函数：文件名规范化、日志、路径处理。"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from rich.logging import RichHandler

logger = logging.getLogger("music_dedup")


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """配置日志：终端 INFO + 文件 DEBUG。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)

    # 清除已有 handler，避免重复；先关闭文件句柄，防止长驻进程里出现句柄泄漏（WinError 32）
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.close()
    logger.handlers.clear()

    console_handler = RichHandler(rich_tracebacks=True, show_time=False, show_path=False)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    from datetime import datetime

    log_file = log_dir / f"dedup-{datetime.now():%Y%m%d}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)


SEPARATORS = [" - ", " _ ", " — ", " – ", " 一 ", " —— ", "·", "、"]

# 歌手名常见分隔前缀
ARTIST_SEPARATORS = ["-", "–", "—", "_", "·", "、", "－"]

# 需要去除的常见标记词
MARKER_WORDS = [
    "feat", "ft", "featuring",
    "remix", "remaster", "remastered",
    "live", "acoustic", "instrumental", "伴奏", "纯音乐",
    "hq", "hires", "hi-res",
]

# 下载器/刮削失败时常见的占位标签，不参与匹配
PLACEHOLDER_TAGS = {
    "unknown", "untitled", "track", "audio", "music", "song", "default",
    "title", "artist", "album",
    "kuwo", "kuwo音乐", "酷我", "酷我音乐", "qq music", "qq音乐", "qqmusic",
    "netease", "网易云音乐", "xiami", "虾米", "tidal", "soundcloud",
    "未知", "无标题", "默认", "音频", "音乐", "歌名", "歌手",
}

# 副本标记（文件名末尾的拷贝标识）
COPY_MARKERS = [
    "副本", "copy", "复件",
    r"\(\d+\)", r"（\d+）",  # (1), （1）
    r"-\s*\d+$",  # - 1, -2
]

# 片段分隔符：文件名里用来区分歌名/歌手的符号
SEGMENT_SEPARATORS = r"[\-_—–·、－]"

# 前导序号："10."、"03)"、"04、"，以及 1~3 位数字加空格（"06 歌名"）。
# 4 位数字留给年份，所以 "2002 年的第一场雪" 不会被削掉前缀。
LEADING_SEQUENCE_PATTERNS = [
    re.compile(r"^\d{1,4}\s*[.、)）]\s*"),
    re.compile(r"^\d{1,3}\s+"),
]


def _remove_copy_markers(name: str) -> str:
    """去除文件名末尾的副本标记。"""
    # 循环去除，处理 "十年 - 副本 (1)" 这类叠加情况
    changed = True
    while changed:
        changed = False
        for marker in COPY_MARKERS:
            new_name = re.sub(rf"\s*{marker}\s*$", "", name, flags=re.IGNORECASE)
            if new_name != name:
                name = new_name
                changed = True
    return name.strip()


def _strip_leading_sequence(token: str) -> str:
    """去掉片段开头的前导序号；纯数字片段（如 "0003"）直接丢弃。"""
    token = token.strip()
    if not token:
        return ""
    if token.isdigit():
        return ""
    for pattern in LEADING_SEQUENCE_PATTERNS:
        token = pattern.sub("", token, count=1)
    return token.strip()


def fullwidth_to_halfwidth(text: str) -> str:
    """全角字符转半角。"""
    result = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        result.append(chr(code))
    return "".join(result)


def filename_segments(name: str) -> list[str]:
    """把文件名切成有效片段。

    处理：去扩展名、全角转半角、去副本标记（"副本"、"(1)"）、
    丢弃前导序号（"0003-"、"01_"、"10."）与纯数字片段。

    括号里的版本标注（"(Live)"、"(Live Version)"）和 feat/remix 等标记词会被
    保留在片段里：它们代表不同的录音，抹掉会让现场版和录音室版被当成同一首。

    返回按出现顺序排列的片段列表，调用方可以自行决定如何组合。
    """
    # 去扩展名
    name = Path(name).stem

    # 全角转半角
    name = fullwidth_to_halfwidth(name)

    # 去除副本标记（"副本"、"copy"、"(1)" 等）
    name = _remove_copy_markers(name)

    segments: list[str] = []
    for raw in re.split(SEGMENT_SEPARATORS, name):
        token = _strip_leading_sequence(raw)
        if not token:
            continue
        # 去除标点，保留词间空格（避免 "Hey Jude" 与 "HeyJude" 被误判为同一首）
        token = re.sub(r"[\.,!?'\"`~@#\$%\^&\*\+=\|\\/<>:;()\[\]{}]", "", token)
        token = token.strip("-_—–·、－ ")
        token = re.sub(r"\s+", " ", token).strip()
        if token:
            segments.append(unicodedata.normalize("NFKC", token).lower())

    return segments


def normalize_filename(name: str) -> str:
    """将文件名规范化为可比较的形式。

    保留全部有效片段并按字典序排列，而不是只取最后一段：
    这样 "歌名-歌手" 与 "歌手-歌名" 能归到一起，而同一位歌手的不同歌曲
    不会因为歌手名相同被误判成重复。
    """
    segments = filename_segments(name)
    if not segments:
        return ""
    return "|".join(sorted(segments))


def is_placeholder_tag(value: str | None) -> bool:
    """判断标签是否为无意义的占位值。"""
    if not value:
        return True
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return not normalized or normalized in PLACEHOLDER_TAGS


def normalize_artist_artist_in_path(path: Path) -> str:
    """从路径中尝试提取歌手名（通常在父目录名中）。"""
    return path.parent.name.strip()


def human_size(size_bytes: int) -> str:
    """字节数转人类可读格式。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def human_duration(seconds: float) -> str:
    """秒数转 mm:ss 格式。"""
    if not seconds or seconds < 0:
        return "--:--"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


def get_file_ext(path: Path) -> str:
    """获取小写扩展名（不带点）。"""
    return path.suffix.lower().lstrip(".")

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

    # 清除已有 handler，避免重复
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


# 常见歌曲标注，规范化时去除
ANNOTATION_PATTERNS = [
    r"\([^)]*\)",  # (Live) (伴奏) (Remix)
    r"（[^）]*）",  # 全角括号
    r"\[[^\]]*\]",
    r"【[^】]*】",
    r"\{[^}]*\}",
]

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

# 副本标记（文件名末尾的拷贝标识）
COPY_MARKERS = [
    "副本", "copy", "复件",
    r"\(\d+\)", r"（\d+）",  # (1), （1）
    r"-\s*\d+$",  # - 1, -2
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


def normalize_filename(name: str) -> str:
    """将文件名规范化为可比较的形式。

    处理：
    - 去掉扩展名
    - 全角转半角
    - 去除括号内标注
    - 去除 feat/remix 等标记
    - 去除常见分隔符及前后的歌手名前缀（"陈奕迅 - 十年" -> "十年"）
    - 统一小写、去空格
    """
    # 去扩展名
    name = Path(name).stem

    # 全角转半角
    name = fullwidth_to_halfwidth(name)

    # 去除括号标注
    for pat in ANNOTATION_PATTERNS:
        name = re.sub(pat, " ", name)

    # 去除副本标记（"副本"、"copy"、"(1)" 等）
    name = _remove_copy_markers(name)

    # 按分隔符切分，取最后一段（通常是歌名）
    # 先尝试常见分隔符
    parts = [p.strip() for p in re.split(r"[\-_—–·、－]", name) if p.strip()]
    if len(parts) > 1:
        name = parts[-1]

    # 去除标记词
    for marker in MARKER_WORDS:
        name = re.sub(rf"\b{re.escape(marker)}\b", " ", name, flags=re.IGNORECASE)

    # 去除标点，保留词间空格（避免 "Hey Jude" 与 "HeyJude" 被误判为同一首）
    name = re.sub(r"[\.,!?'\"`~@#\$%\^&\*\+=\|\\/<>:;]+", "", name)

    # 去除首尾残留的分隔符
    name = name.strip("-_—–·、－ ")

    # 压缩连续空白为单个空格
    name = re.sub(r"\s+", " ", name).strip()

    # Unicode 规范化（处理组合字符）
    name = unicodedata.normalize("NFKC", name).lower()

    return name.strip()


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

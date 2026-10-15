"""封面图下载与嵌入模块。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import requests

from .utils import logger


def download_cover(url: str) -> Optional[bytes]:
    """下载封面图二进制数据。"""
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning(f"封面下载失败: {e}")
        return None


def detect_image_mime(data: bytes) -> str:
    """根据文件头判断 MIME 类型。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"  # 默认

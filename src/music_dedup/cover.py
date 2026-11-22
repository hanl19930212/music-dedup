"""封面图下载与嵌入模块。"""

from __future__ import annotations

from typing import Optional

import requests

from .utils import logger

# 封面是嵌进音频标签里的小图，限制上限避免异常 URL/响应吃满内存
MAX_COVER_BYTES = 8 * 1024 * 1024
DOWNLOAD_CHUNK = 64 * 1024
IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
)


def download_cover(url: str) -> Optional[bytes]:
    """流式下载封面图，限制大小并校验类型；失败或非图片返回 None。"""
    try:
        with requests.get(url, timeout=30, stream=True) as resp:
            resp.raise_for_status()
            content_type = (
                (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            )
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_COVER_BYTES:
                    logger.warning(f"封面超过大小上限 {MAX_COVER_BYTES} 字节，已放弃: {url}")
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
    except requests.RequestException as e:
        logger.warning(f"封面下载失败: {e}")
        return None
    except OSError as e:
        logger.warning(f"封面下载失败: {e}")
        return None

    if not data:
        return None
    if content_type and content_type not in IMAGE_CONTENT_TYPES:
        logger.warning(f"封面响应不是图片 ({content_type})，已放弃: {url}")
        return None
    if detect_image_mime(data) is None:
        logger.warning(f"封面数据无法识别为图片，已放弃: {url}")
        return None
    return data


def detect_image_mime(data: bytes) -> Optional[str]:
    """根据文件头判断 MIME 类型；无法识别返回 None（不再默认当成 JPEG）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None

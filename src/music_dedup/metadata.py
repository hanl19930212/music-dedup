"""音频元数据读取模块（基于 mutagen）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.wave import WAVE
from mutagen.dsf import DSF

from .utils import get_file_ext, logger


@dataclass
class AudioMetadata:
    """音频文件的元数据。"""

    path: Path
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[str] = None
    track: Optional[str] = None
    duration: Optional[float] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    bits_per_sample: Optional[int] = None
    has_cover: bool = False

    # 规范化后的比较字段
    norm_title: str = ""
    norm_artist: str = ""

    @property
    def ext(self) -> str:
        return get_file_ext(self.path)


def _get_first(tags, keys: list[str]) -> Optional[str]:
    """从标签中取第一个非空值。"""
    for key in keys:
        val = tags.get(key)
        if val:
            if isinstance(val, list):
                if val and val[0]:
                    return str(val[0])
            else:
                return str(val)
    return None


def read_metadata(path: Path) -> AudioMetadata:
    """读取音频文件的元数据。

    支持 mp3(ID3)、flac(Vorbis)、wav(ID3)、dsf(ID3)。
    读取失败时返回仅含 path 的对象。
    """
    meta = AudioMetadata(path=path)
    ext = get_file_ext(path)

    try:
        audio = MutagenFile(str(path), easy=False)
    except Exception as e:
        logger.warning(f"元数据读取失败 [{path.name}]: {e}")
        return meta

    if audio is None:
        return meta

    # 基础流信息
    info = getattr(audio, "info", None)
    if info:
        meta.duration = getattr(info, "length", None)
        meta.bitrate = getattr(info, "bitrate", None)
        meta.sample_rate = getattr(info, "sample_rate", None)
        meta.channels = getattr(info, "channels", None)
        meta.bits_per_sample = getattr(info, "bits_per_sample", None)

    # 按格式读取标签
    if ext == "mp3":
        tags = audio.tags if isinstance(audio, MP3) else getattr(audio, "tags", None)
        if tags:
            meta.title = _get_first(tags, ["TIT2", "TIT1"])
            meta.artist = _get_first(tags, ["TPE1", "TPE2"])
            meta.album = _get_first(tags, ["TALB"])
            meta.genre = _get_first(tags, ["TCON"])
            meta.year = _get_first(tags, ["TDRC", "TYER"])
            meta.track = _get_first(tags, ["TRCK"])
            # 检查封面
            for frame_key in tags:
                if frame_key.startswith("APIC"):
                    meta.has_cover = True
                    break
    elif ext == "flac":
        tags = audio.tags if isinstance(audio, FLAC) else getattr(audio, "tags", None)
        if tags:
            meta.title = _get_first(tags, ["title"])
            meta.artist = _get_first(tags, ["artist", "albumartist"])
            meta.album = _get_first(tags, ["album"])
            meta.genre = _get_first(tags, ["genre"])
            meta.year = _get_first(tags, ["date", "year", "originaldate"])
            meta.track = _get_first(tags, ["tracknumber"])
            if isinstance(audio, FLAC):
                meta.has_cover = len(audio.pictures) > 0
    elif ext in ("wav", "dsf"):
        tags = getattr(audio, "tags", None)
        if tags:
            meta.title = _get_first(tags, ["TIT2"])
            meta.artist = _get_first(tags, ["TPE1"])
            meta.album = _get_first(tags, ["TALB"])
            meta.genre = _get_first(tags, ["TCON"])
            meta.year = _get_first(tags, ["TDRC", "TYER"])
            meta.track = _get_first(tags, ["TRCK"])
            for frame_key in tags:
                if frame_key.startswith("APIC"):
                    meta.has_cover = True
                    break

    # 规范化比较字段
    from .utils import normalize_filename

    meta.norm_title = normalize_filename(meta.title) if meta.title else ""
    meta.norm_artist = normalize_filename(meta.artist) if meta.artist else ""

    return meta

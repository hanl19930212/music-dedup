"""重复检测分组算法。

分组优先级：
  1. MusicBrainz recording ID（刮削过的文件）
  2. fpcalc 指纹完全相同
  3. 指纹相似 + 元数据匹配
  4. 文件名规范化匹配（兜底）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import DedupConfig, FORMAT_PRIORITY
from .fingerprint import FingerprintResult
from .metadata import AudioMetadata
from .scraper import ScrapedMetadata
from .utils import normalize_filename


@dataclass
class TrackItem:
    """单个音频文件的完整信息。"""

    audio_file: object  # AudioFile
    fingerprint: Optional[str]
    duration: Optional[float]
    metadata: AudioMetadata
    scraped: Optional[ScrapedMetadata] = None

    @property
    def path(self) -> Path:
        return self.audio_file.path

    @property
    def size(self) -> int:
        return self.audio_file.size

    @property
    def ext(self) -> str:
        return self.audio_file.ext

    @property
    def title(self) -> Optional[str]:
        if self.scraped and self.scraped.title:
            return self.scraped.title
        return self.metadata.title

    @property
    def artist(self) -> Optional[str]:
        if self.scraped and self.scraped.artist:
            return self.scraped.artist
        return self.metadata.artist

    @property
    def album(self) -> Optional[str]:
        if self.scraped and self.scraped.album:
            return self.scraped.album
        return self.metadata.album

    @property
    def recording_id(self) -> Optional[str]:
        return self.scraped.recording_id if self.scraped else None

    @property
    def norm_name(self) -> str:
        """规范化文件名，用于兜底匹配。"""
        return normalize_filename(self.path.name)

    @property
    def norm_title(self) -> str:
        return normalize_filename(self.title) if self.title else ""

    @property
    def norm_artist(self) -> str:
        return normalize_filename(self.artist) if self.artist else ""


@dataclass
class DuplicateGroup:
    """一组重复文件。"""

    key: str
    match_type: str  # "recording_id" / "fingerprint" / "name"
    items: list[TrackItem] = field(default_factory=list)
    keep_index: int = 0  # 建议保留的文件在 items 中的索引

    @property
    def keep(self) -> TrackItem:
        return self.items[self.keep_index]

    @property
    def duplicates(self) -> list[TrackItem]:
        return [item for i, item in enumerate(self.items) if i != self.keep_index]

    @property
    def wasted_size(self) -> int:
        return sum(item.size for item in self.duplicates)

    @property
    def display_title(self) -> str:
        return self.keep.title or self.keep.path.stem

    @property
    def display_artist(self) -> str:
        return self.keep.artist or ""


def _pick_keep(items: list[TrackItem]) -> int:
    """选择要保留的文件索引。

    优先级：格式优先级 > 文件大小 > 修改时间。
    """
    def sort_key(item: TrackItem):
        fmt_pri = FORMAT_PRIORITY.get(item.ext, 99)
        return (fmt_pri, -item.size, -item.audio_file.mtime)

    indexed = list(enumerate(items))
    indexed.sort(key=lambda x: sort_key(x[1]))
    return indexed[0][0]


def group_duplicates(
    items: list[TrackItem],
    config: DedupConfig,
) -> list[DuplicateGroup]:
    """将文件分组为重复组。"""
    groups: list[DuplicateGroup] = []
    used: set[int] = set()

    # Level 1: recording ID（最精确）
    recording_groups: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        if item.recording_id:
            recording_groups.setdefault(item.recording_id, []).append(i)

    for rid, indices in recording_groups.items():
        if len(indices) < 2:
            continue
        group_items = [items[i] for i in indices]
        group = DuplicateGroup(
            key=f"recording:{rid}",
            match_type="recording_id",
            items=group_items,
        )
        group.keep_index = _pick_keep(group_items)
        groups.append(group)
        used.update(indices)

    # Level 2: 指纹完全相同
    fingerprint_groups: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        if i in used or not item.fingerprint:
            continue
        fingerprint_groups.setdefault(item.fingerprint, []).append(i)

    for fp, indices in fingerprint_groups.items():
        if len(indices) < 2:
            continue
        group_items = [items[i] for i in indices]
        group = DuplicateGroup(
            key=f"fingerprint:{fp[:16]}",
            match_type="fingerprint",
            items=group_items,
        )
        group.keep_index = _pick_keep(group_items)
        groups.append(group)
        used.update(indices)

    # Level 3: 文件名规范化匹配（兜底）
    if config.name_fallback:
        name_groups: dict[str, list[int]] = {}
        for i, item in enumerate(items):
            if i in used:
                continue
            norm = item.norm_name
            if not norm:
                continue
            # 同时检查规范化标题
            if item.norm_title and item.norm_title != norm:
                # 如果标题存在且与文件名不同，优先用标题分组
                name_groups.setdefault(item.norm_title, []).append(i)
            else:
                name_groups.setdefault(norm, []).append(i)

        for name, indices in name_groups.items():
            if len(indices) < 2:
                continue
            group_items = [items[i] for i in indices]
            group = DuplicateGroup(
                key=f"name:{name}",
                match_type="name",
                items=group_items,
            )
            group.keep_index = _pick_keep(group_items)
            groups.append(group)
            used.update(indices)

    # 按浪费空间排序，大的在前
    groups.sort(key=lambda g: g.wasted_size, reverse=True)
    return groups

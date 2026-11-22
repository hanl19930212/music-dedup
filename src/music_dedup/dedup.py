"""重复检测分组算法。

分组优先级：
  1. MusicBrainz recording ID（刮削过的文件）
  2. fpcalc 指纹完全相同
  3. 指纹相似（同一录音的不同编码，默认不参与删除）
  4. 文件名规范化匹配（兜底，默认不参与删除）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import FORMAT_PRIORITY, DedupConfig
from .fingerprint import fingerprint_similarity
from .metadata import AudioMetadata
from .scraper import ScrapedMetadata
from .utils import filename_segments, is_placeholder_tag


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
    def name_tokens(self) -> frozenset[str]:
        """同名匹配标识：文件名片段 + 标签里的歌名/歌手。

        用集合而不是单个字符串，"歌名-歌手" 与 "歌手-歌名" 才能对上，
        而同一位歌手的不同歌曲不会因为歌手名相同被误判成重复。

        有可用标签时以标签为准（文件名可能含序号等噪音），没有标签时才退回文件名。
        """
        tokens: set[str] = set()
        title, artist = self.title, self.artist
        if not is_placeholder_tag(title):
            tokens.update(filename_segments(title))
            if not is_placeholder_tag(artist):
                tokens.update(filename_segments(artist))
            return frozenset(tokens)

        tokens.update(filename_segments(self.path.name))
        if not is_placeholder_tag(artist):
            tokens.update(filename_segments(artist))
        return frozenset(tokens)


@dataclass
class DuplicateGroup:
    """一组重复文件。"""

    key: str
    match_type: str  # "recording_id" / "fingerprint" / "fingerprint_similar" / "name"
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

    # Level 2.5: 指纹相似（同一录音的不同编码：DSD / FLAC / WAV / MP3）
    if config.min_similarity < 1.0:
        for group in _group_similar_fingerprints(items, used, config):
            groups.append(group)

    # Level 3: 文件名规范化匹配（兜底）
    if config.name_fallback:
        name_groups: dict[frozenset[str], list[int]] = {}
        for i, item in enumerate(items):
            if i in used:
                continue
            tokens = item.name_tokens
            if not tokens:
                continue
            name_groups.setdefault(tokens, []).append(i)

        for tokens, indices in name_groups.items():
            if len(indices) < 2:
                continue
            group_items = [items[i] for i in indices]
            group = DuplicateGroup(
                key=f"name:{'|'.join(sorted(tokens))}",
                match_type="name",
                items=group_items,
            )
            group.keep_index = _pick_keep(group_items)
            groups.append(group)
            used.update(indices)

    # 按浪费空间排序，大的在前
    groups.sort(key=lambda g: g.wasted_size, reverse=True)
    return groups


def _group_similar_fingerprints(
    items: list[TrackItem], used: set[int], config: DedupConfig
) -> list[DuplicateGroup]:
    """把指纹相似但不完全相同的文件归为一组。

    典型场景是同一首曲子存在 DSD / FLAC / WAV / MP3 几种编码，指纹不会完全相同，
    但逐位相似度很高。只比较时长接近的文件，避免两两比较带来的开销。
    """
    candidates = [
        (index, item)
        for index, item in enumerate(items)
        if index not in used and item.fingerprint and item.duration
    ]
    if len(candidates) < 2:
        return []

    by_index = {index: item for index, item in candidates}
    duration_buckets: dict[int, list[int]] = defaultdict(list)
    for index, item in candidates:
        duration_buckets[int(round(item.duration))].append(index)

    parent = {index: index for index in by_index}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    checked: set[tuple[int, int]] = set()
    radius = int(config.fingerprint_duration_tolerance) + 1
    for duration, indexes in duration_buckets.items():
        nearby = [
            item
            for other in range(duration - radius, duration + radius + 1)
            for item in duration_buckets.get(other, [])
        ]
        for left in indexes:
            for right in nearby:
                if left == right:
                    continue
                pair = (min(left, right), max(left, right))
                if pair in checked:
                    continue
                checked.add(pair)
                left_item, right_item = by_index[left], by_index[right]
                if abs(left_item.duration - right_item.duration) > config.fingerprint_duration_tolerance:
                    continue
                if fingerprint_similarity(
                    left_item.fingerprint,
                    right_item.fingerprint,
                    threshold=config.min_similarity,
                ) >= config.min_similarity:
                    union(left, right)

    clustered: dict[int, list[int]] = defaultdict(list)
    for index in by_index:
        clustered[find(index)].append(index)

    groups: list[DuplicateGroup] = []
    for indices in clustered.values():
        if len(indices) < 2:
            continue
        group_items = [items[i] for i in indices]
        group = DuplicateGroup(
            key=f"similar:{items[indices[0]].fingerprint[:16]}",
            match_type="fingerprint_similar",
            items=group_items,
        )
        group.keep_index = _pick_keep(group_items)
        groups.append(group)
        used.update(indices)
    return groups

"""report.json 契约：序列化、严格校验与文件状态复核。

这是 ``apply`` 读取扫描结果的唯一入口。JSON 里的路径、keep_index、match_type
都会直接决定「哪个文件被移入回收站」，因此一律按不可信输入处理：先做结构与
取值校验，再做路径归属校验，最后用真实对象重建分组，避免伪造、过期或被篡改
的报告把扫描范围之外的文件删掉。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .dedup import DuplicateGroup, TrackItem
from .metadata import AudioMetadata
from .scanner import AudioFile

SCHEMA_VERSION = 1

VALID_MATCH_TYPES = frozenset(
    {"recording_id", "fingerprint", "fingerprint_similar", "name"}
)

# key 前缀必须与 match_type 对应，防止「只改 match_type」把低置信组伪装成精确匹配组。
# dedup.py 真实生成的前缀为 recording:/fingerprint:/similar:/name:；
# fingerprint_similar 额外接受 fingerprint_similar:，兼容测试辅助函数按 match_type 拼 key 的写法。
KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "recording_id": ("recording:",),
    "fingerprint": ("fingerprint:",),
    "fingerprint_similar": ("similar:", "fingerprint_similar:"),
    "name": ("name:",),
}

# 报告里记录的是扫描时刻的 mtime，重新 stat 时允许亚毫秒级误差
MTIME_TOLERANCE = 0.001


class ReportValidationError(ValueError):
    """报告结构或内容不合法，apply 必须拒绝执行。"""


def serialize_report(groups: list[DuplicateGroup], scan_roots: list[Path]) -> dict[str, Any]:
    """把内存中的分组序列化为带版本号的 JSON 契约。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_roots": [str(Path(root).resolve()) for root in scan_roots],
        "groups": [
            {
                "key": group.key,
                "match_type": group.match_type,
                "keep_index": group.keep_index,
                "keep_path": str(group.keep.path),
                "items": [
                    {
                        "path": str(item.path),
                        "size": int(item.size),
                        "ext": item.ext,
                        "mtime": float(item.audio_file.mtime),
                    }
                    for item in group.items
                ],
            }
            for group in groups
        ],
    }


def _normalize(path: str) -> str:
    """归一化绝对路径：统一大小写与分隔符，用于比较与归属判断。"""
    return os.path.normcase(os.path.abspath(path))


def _reject_parent_segments(raw_path: str, label: str) -> None:
    """显式拒绝含 '..' 段的路径，避免靠相对写法绕过归属校验。"""
    parts = raw_path.replace("\\", "/").split("/")
    if ".." in parts:
        raise ReportValidationError(f"{label}: 路径包含 '..' 段，已拒绝: {raw_path}")


def _path_under_roots(raw_path: str, roots: list[str], label: str) -> Path:
    """校验 path 落在任一 scan_root 内，返回可用的绝对 Path。

    commonpath 在跨盘符/跨 UNC 共享时会抛 ValueError；这里把它当作「与该
    root 不匹配」继续尝试下一个 root，只有所有 root 都不匹配才整体拒绝。
    这样既堵住 ``C:\\Windows\\...`` 这类越界路径，也不会误伤「本地盘 +
    UNC 共享」混合扫描生成的合法报告。
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ReportValidationError(f"{label}: path 必须是非空字符串")
    _reject_parent_segments(raw_path, label)

    absolute = os.path.abspath(raw_path)
    normalized = os.path.normcase(absolute)
    for root in roots:
        try:
            common = os.path.commonpath([normalized, root])
        except ValueError:
            continue
        # 归一化后的 UNC 共享根（\\host\share\）与盘符根（C:\）都保留末尾分隔符，
        # 直接拼 root + os.sep 会得到双分隔符，startswith 永不成立（扫共享根时会误拒）。
        # 统一去掉末尾分隔符后再比较，归属判断用「去掉尾分隔符 + 一个分隔符」做前缀。
        root_base = os.path.normcase(root).rstrip("\\/")
        common_base = os.path.normcase(common).rstrip("\\/")
        if common_base != root_base:
            continue
        if normalized == root_base or normalized.startswith(root_base + os.sep):
            return Path(absolute)
    raise ReportValidationError(
        f"{label}: 路径不在任何 scan_roots 范围内，已拒绝: {raw_path}"
    )


def _parse_item(raw_item: Any, roots: list[str], label: str) -> tuple[Path, int, str, float]:
    """校验单个 item，返回构造 AudioFile 所需的原始值。"""
    if not isinstance(raw_item, dict):
        raise ReportValidationError(f"{label}: item 必须是对象")

    raw_path = raw_item.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ReportValidationError(f"{label}: path 必须是非空字符串")
    path = _path_under_roots(raw_path, roots, label)

    size = raw_item.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReportValidationError(f"{label}: size 必须是非负整数，实际 {size!r}")

    ext = raw_item.get("ext")
    if not isinstance(ext, str):
        raise ReportValidationError(f"{label}: ext 必须是字符串，实际 {ext!r}")

    mtime = raw_item.get("mtime")
    if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
        raise ReportValidationError(f"{label}: mtime 必须是数值，实际 {mtime!r}")

    return path, size, ext, float(mtime)


def _parse_group(raw_group: Any, index: int, roots: list[str]) -> DuplicateGroup:
    """校验并重建单个分组；任何不合法之处都拒绝整个报告。"""
    label = f"第 {index + 1} 组"
    if not isinstance(raw_group, dict):
        raise ReportValidationError(f"{label}: 组必须是对象")

    key = raw_group.get("key")
    if not isinstance(key, str):
        raise ReportValidationError(f"{label}: key 必须是字符串，实际 {key!r}")

    match_type = raw_group.get("match_type")
    if not isinstance(match_type, str) or match_type not in VALID_MATCH_TYPES:
        raise ReportValidationError(
            f"{label}: match_type 非法 {match_type!r}，允许值: {sorted(VALID_MATCH_TYPES)}"
        )

    # 白名单挡不住「跨合法值篡改」：name 组只把 match_type 改成 fingerprint 就能绕过
    # 「低置信组默认不删」的保护。key 前缀是分组时按 match_type 生成的，可用来交叉验证。
    if not key.startswith(KEY_PREFIXES[match_type]):
        raise ReportValidationError(
            f"{label}: key 前缀与 match_type 不一致"
            f"（key={key!r}, match_type={match_type!r}），已拒绝"
        )

    items_data = raw_group.get("items")
    if not isinstance(items_data, list) or not items_data:
        raise ReportValidationError(f"{label}: items 必须是非空列表")

    keep_index = raw_group.get("keep_index")
    if isinstance(keep_index, bool) or not isinstance(keep_index, int):
        raise ReportValidationError(f"{label}: keep_index 必须是整数，实际 {keep_index!r}")
    if not 0 <= keep_index < len(items_data):
        raise ReportValidationError(
            f"{label}: keep_index 越界 ({keep_index}, items 共 {len(items_data)} 个)"
        )

    parsed_items = [
        _parse_item(raw_item, roots, f"{label}第 {position + 1} 项")
        for position, raw_item in enumerate(items_data)
    ]

    keep_path = raw_group.get("keep_path")
    if not isinstance(keep_path, str) or not keep_path.strip():
        raise ReportValidationError(f"{label}: keep_path 必须是非空字符串")
    _reject_parent_segments(keep_path, f"{label} keep_path")
    if _normalize(keep_path) != _normalize(str(parsed_items[keep_index][0])):
        raise ReportValidationError(
            f"{label}: keep_path 与 keep_index 指向的文件不一致，已拒绝"
        )

    track_items = [
        TrackItem(
            audio_file=AudioFile(path=path, size=size, mtime=mtime),
            fingerprint=None,
            duration=None,
            metadata=AudioMetadata(path=path),
        )
        for path, size, _ext, mtime in parsed_items
    ]
    return DuplicateGroup(
        key=key,
        match_type=match_type,
        items=track_items,
        keep_index=keep_index,
    )


def deserialize_report(data: Any) -> list[DuplicateGroup]:
    """严格校验并重建分组；任何问题都抛 ReportValidationError。"""
    if isinstance(data, list):
        raise ReportValidationError("报告格式过旧，请重新执行 scan 生成报告")
    if not isinstance(data, dict):
        raise ReportValidationError("报告顶层必须是 JSON 对象")

    version = data.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ReportValidationError(
            f"报告 schema_version 不支持: {version!r}（需要 {SCHEMA_VERSION}）"
        )

    raw_roots = data.get("scan_roots")
    if (
        not isinstance(raw_roots, list)
        or not raw_roots
        or not all(isinstance(root, str) and root.strip() for root in raw_roots)
    ):
        raise ReportValidationError("scan_roots 必须是非空字符串列表")
    roots = [_normalize(root) for root in raw_roots]

    groups_data = data.get("groups")
    if not isinstance(groups_data, list):
        raise ReportValidationError("groups 必须是列表")

    return [
        _parse_group(raw_group, index, roots)
        for index, raw_group in enumerate(groups_data)
    ]


def verify_file_state(group: DuplicateGroup) -> list[str]:
    """复核组内文件与报告是否一致；返回问题列表（空列表表示通过）。

    用于防「报告生成后文件被替换/移动」的 TOCTOU：dry-run 与真实执行都要过这一关。
    """
    problems: list[str] = []
    for item in group.items:
        try:
            stat = item.path.stat()
        except OSError as exc:
            problems.append(f"{item.path}: 文件不存在或无法访问（{exc}）")
            continue
        if stat.st_size != item.size:
            problems.append(
                f"{item.path}: 大小不一致（报告 {item.size} 字节，实际 {stat.st_size} 字节）"
            )
        recorded_mtime = float(item.audio_file.mtime)
        if abs(stat.st_mtime - recorded_mtime) > MTIME_TOLERANCE:
            problems.append(
                f"{item.path}: 修改时间不一致（报告 {recorded_mtime}，实际 {stat.st_mtime}）"
            )
    return problems

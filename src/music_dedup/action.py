"""文件操作模块：移到回收站。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

from .contract import VALID_MATCH_TYPES
from .dedup import DuplicateGroup
from .utils import human_size, logger


@dataclass
class ActionResult:
    """操作结果。"""

    path: Path
    success: bool
    message: str
    freed_size: int = 0
    quarantined: bool = False
    quarantined_size: int = 0


def _is_network_path(path: Path) -> bool:
    """判断路径是否位于网络共享（UNC 路径，或映射到网络位置的驱动器）。

    Windows 回收站不覆盖网络共享：send2trash 在 UNC 路径上往往**不报错**、
    直接把文件永久删除，异常兜底不会触发。这类路径必须跳过回收站。
    """
    drive = path.drive
    if drive.startswith("\\\\"):
        return True
    if os.name != "nt" or not drive:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def _move_to_quarantine(
    path: Path, size: int, quarantine_dir: Path, reason: str
) -> ActionResult:
    """把文件移入隔离目录，失败时如实报告，绝不静默删除。"""
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(quarantine_dir / path.name)
        shutil.move(str(path), str(destination))
        logger.info(f"{reason}，已移入隔离目录: {path} -> {destination}")
        return ActionResult(
            path=path,
            success=True,
            message=f"{reason}，已移入隔离目录: {destination}",
            # 同盘移动并未释放空间，只记为隔离占用
            freed_size=0,
            quarantined=True,
            quarantined_size=size,
        )
    except Exception as move_exc:
        logger.error(f"移入隔离目录失败 [{path}]: {move_exc}")
        return ActionResult(path=path, success=False, message=f"{reason}; 移入隔离目录失败: {move_exc}")


def move_to_trash(path: Path, quarantine_dir: Path | None = None) -> ActionResult:
    """将单个文件移到回收站。

    网络共享（UNC 或映射的网络驱动器）上回收站不可用，直接改走隔离目录；
    其他位置回收站失败时同样退回到隔离目录，保证文件不会被误删。
    """
    if not path.exists():
        return ActionResult(path=path, success=False, message="文件不存在")

    size = path.stat().st_size
    if _is_network_path(path):
        if quarantine_dir is None:
            logger.warning(f"网络共享不支持回收站，且未配置隔离目录 [{path}]: 已跳过")
            return ActionResult(
                path=path,
                success=False,
                message="网络共享不支持回收站，且未配置隔离目录，已跳过（未删除）",
            )
        return _move_to_quarantine(path, size, quarantine_dir, "网络共享不支持回收站")

    try:
        send2trash(str(path))
        logger.info(f"已移到回收站: {path} ({human_size(size)})")
        return ActionResult(path=path, success=True, message="已移到回收站", freed_size=size)
    except Exception as exc:
        logger.warning(f"移到回收站失败 [{path}]: {exc}")
        if quarantine_dir is None:
            return ActionResult(path=path, success=False, message=str(exc))
        return _move_to_quarantine(path, size, quarantine_dir, "回收站不可用")


def _unique_destination(candidate: Path) -> Path:
    """目标已存在时追加序号，避免覆盖。"""
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 100000):
        alternative = candidate.with_name(f"{stem}__{index}{suffix}")
        if not alternative.exists():
            return alternative
    raise RuntimeError(f"无法生成隔离路径: {candidate}")


def resolve_quarantine_dir(paths: list[Path], name: str) -> Path:
    """根据待处理文件推断隔离目录：所有文件的公共父目录 + name。"""
    if not paths:
        return Path.cwd() / name
    try:
        common = Path(os.path.commonpath([str(path) for path in paths]))
    except ValueError:
        common = paths[0].parent
    # 只有「确实是文件」才上移一层；D:\My.Music 这类带点的目录不能被误判
    if common.suffix and not common.is_dir():
        common = common.parent
    return common / name


def apply_dedup(
    groups: list[DuplicateGroup],
    dry_run: bool = True,
    include_name_matches: bool = False,
    include_similar_matches: bool = False,
    quarantine_dir: Path | None = None,
) -> list[ActionResult]:
    """对所有重复组执行去重操作。

    每组保留 keep，其余移到回收站。
    dry_run=True 时只打印计划，不实际操作。
    match_type 采用白名单：非法/未知类型一律跳过，避免伪造的 "fingerprint" 等
    值绕过「低置信组默认不删」的保护。
    """
    results: list[ActionResult] = []

    for group in groups:
        skip_reason: str | None = None
        if group.match_type not in VALID_MATCH_TYPES:
            skip_reason = f"未知匹配类型 {group.match_type!r}（拒绝处理）"
        elif group.match_type == "name" and not include_name_matches:
            skip_reason = "文件名匹配组（建议人工确认）"
        elif group.match_type == "fingerprint_similar" and not include_similar_matches:
            skip_reason = "指纹相似匹配组（建议人工确认）"
        if skip_reason is not None:
            logger.warning(
                f"跳过{skip_reason}: {group.display_title} "
                f"({len(group.duplicates)} 个文件未删除)"
            )
            continue

        for item in group.duplicates:
            if dry_run:
                results.append(ActionResult(
                    path=item.path,
                    success=True,
                    message=f"[DRY-RUN] 将移到回收站 ({human_size(item.size)})",
                    freed_size=item.size,
                ))
                logger.info(f"[DRY-RUN] 将删除: {item.path}")
            else:
                results.append(move_to_trash(item.path, quarantine_dir))

    return results

"""文件操作模块：移到回收站。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

from .dedup import DuplicateGroup
from .utils import human_size, logger


@dataclass
class ActionResult:
    """操作结果。"""

    path: Path
    success: bool
    message: str
    freed_size: int = 0


def move_to_trash(path: Path) -> ActionResult:
    """将单个文件移到回收站。"""
    if not path.exists():
        return ActionResult(path=path, success=False, message="文件不存在")

    size = path.stat().st_size
    try:
        send2trash(str(path))
        logger.info(f"已移到回收站: {path} ({human_size(size)})")
        return ActionResult(path=path, success=True, message="已移到回收站", freed_size=size)
    except Exception as e:
        logger.error(f"移到回收站失败 [{path}]: {e}")
        return ActionResult(path=path, success=False, message=str(e))


def apply_dedup(
    groups: list[DuplicateGroup],
    dry_run: bool = True,
    include_name_matches: bool = False,
) -> list[ActionResult]:
    """对所有重复组执行去重操作。

    每组保留 keep，其余移到回收站。
    dry_run=True 时只打印计划，不实际操作。
    include_name_matches=False 时跳过仅凭文件名匹配的组（这类匹配可能误判，默认不删除）。
    """
    results: list[ActionResult] = []

    for group in groups:
        if group.match_type == "name" and not include_name_matches:
            logger.warning(
                f"跳过文件名匹配组（建议人工确认）: {group.display_title} "
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
                results.append(move_to_trash(item.path))

    return results

r"""音频文件扫描模块。

支持本地目录、映射网络盘和 UNC 网络共享路径（如 \\server\share）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import ScanConfig
from .utils import get_file_ext, logger


@dataclass
class AudioFile:
    """扫描到的音频文件信息。"""

    path: Path
    size: int
    mtime: float

    @property
    def ext(self) -> str:
        return get_file_ext(self.path)

    @property
    def name(self) -> str:
        return self.path.name


def normalize_path(path: str | Path) -> Path:
    """规范化路径，支持 UNC 网络共享路径。

    - Windows 上将 / 转为 \\（UNC 路径必须用反斜杠）
    - 去除末尾多余的分隔符
    - 校验 UNC 路径结构（至少包含 server 和 share）
    """
    p = str(path).strip()

    # Windows 上的路径处理
    if sys.platform == "win32":
        # 将正斜杠转为反斜杠
        p = p.replace("/", "\\")
        # 规范化：将重复的反斜杠压缩（UNC 开头保留两个）
        if p.startswith("\\\\"):
            # UNC 路径：\\server\share\...
            rest = p[2:].replace("\\\\", "\\")
            p = "\\\\" + rest
        else:
            p = os.path.normpath(p)

    normalized = Path(p)

    # UNC 路径结构校验
    unc_parts = _parse_unc(normalized)
    if unc_parts is not None:
        server, share = unc_parts
        if not server:
            raise ValueError(f"UNC 路径缺少服务器名: {path}")
        if not share:
            raise ValueError(f"UNC 路径缺少共享名: {path}（应为 \\\\服务器\\共享名）")

    return normalized


def _parse_unc(path: Path) -> tuple[str, str] | None:
    """解析 UNC 路径，返回 (server, share)，非 UNC 返回 None。"""
    p = str(path)
    if sys.platform == "win32":
        if p.startswith("\\\\"):
            parts = p[2:].split("\\", 2)
            if len(parts) >= 2:
                return (parts[0], parts[1])
    else:
        # Linux/macOS 上模拟 UNC（smb:// 或 //）
        if p.startswith("//"):
            parts = p[2:].split("/", 2)
            if len(parts) >= 2:
                return (parts[0], parts[1])
    return None


def is_network_path(path: Path) -> bool:
    """判断是否为网络路径（UNC 或映射盘）。"""
    return _parse_unc(path) is not None


def scan_directory(root: Path, config: ScanConfig) -> list[AudioFile]:
    """递归扫描单个目录，返回所有支持的音频文件。

    跳过 exclude_dirs 中列出的目录。
    """
    normalized = normalize_path(root)

    if not normalized.exists():
        if is_network_path(normalized):
            raise _network_path_error(normalized)
        raise FileNotFoundError(f"目录不存在: {normalized}")

    ext_set = {e.lower().lstrip(".") for e in config.extensions}
    exclude_set = set(config.exclude_dirs)

    results: list[AudioFile] = []
    # 用 os.scandir 而不是 os.walk：目录项的大小/修改时间来自同一次目录枚举
    # （Windows 上 FindFirstFile 就带回来了），不必再对每个文件单独 stat()。
    # 网络共享上每个 syscall 都是几毫秒，几千个文件时会差出十几秒。
    stack = [str(normalized)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in exclude_set or entry.name.startswith("."):
                                continue
                            stack.append(entry.path)
                            continue
                        ext = Path(entry.name).suffix.lower().lstrip(".")
                        if ext not in ext_set:
                            continue
                        stat = entry.stat()
                        results.append(
                            AudioFile(
                                path=Path(entry.path),
                                size=stat.st_size,
                                mtime=stat.st_mtime,
                            )
                        )
                    except OSError:
                        # 无法访问的文件跳过（权限不足等）
                        logger.debug(f"跳过无法访问的文件: {entry.path}")
                        continue
        except PermissionError as e:
            if current == str(normalized):
                raise PermissionError(f"权限不足，无法访问目录: {normalized}") from e
            logger.warning(f"目录访问警告: {e}")
        except OSError as e:
            logger.warning(f"目录访问警告: {e}")

    return results


def scan_directories(roots: list[Path], config: ScanConfig) -> list[AudioFile]:
    """扫描多个目录（本地 + 网络共享），去重合并结果。"""
    all_files: dict[Path, AudioFile] = {}

    for root in roots:
        try:
            files = scan_directory(root, config)
            console_print(f"  {root}: {len(files)} 个文件")
            for f in files:
                # 以绝对路径去重，避免同一文件被多个入口重复扫描。
                # 用 abspath 而不是 Path.resolve()：后者在 Windows 上会逐级访问
                # 文件系统，UNC 路径上实测约 5ms/文件，几千个文件就是十几秒。
                key = os.path.normcase(os.path.abspath(str(f.path)))
                all_files[key] = f
        except (FileNotFoundError, PermissionError, ValueError) as e:
            logger.error(f"扫描跳过 [{root}]: {e}")
            console_print(f"[yellow]跳过 {root}: {e}[/]")

    return list(all_files.values())


def console_print(text: str) -> None:
    """延迟导入 console，避免循环依赖。"""
    from rich.console import Console

    Console().print(text)


def _network_path_error(path: Path) -> Exception:
    """为网络路径生成更详细的错误信息。"""
    parts = _parse_unc(path)
    if parts:
        server, share = parts
        msg = (
            f"无法访问网络共享: {path}\n"
            f"可能的原因：\n"
            f"  1. 主机不可达：检查 {server} 是否开机且在同一局域网\n"
            f"  2. 共享名不存在：确认 \\\\{server}\\{share} 是否正确\n"
            f"  3. 权限不足：需要有读取权限的账户凭据\n"
            f"  4. SMB 服务未启用：目标机器需开启文件和打印机共享"
        )
    else:
        msg = f"网络路径无法访问: {path}"
    return FileNotFoundError(msg)


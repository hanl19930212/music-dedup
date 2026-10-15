"""音频指纹提取模块（基于 Chromaprint/fpcalc）。"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from .config import FingerprintConfig
from .db import CacheDB
from .scanner import AudioFile
from .utils import logger


@dataclass
class FingerprintResult:
    file: AudioFile
    fingerprint: Optional[str]
    duration: Optional[float]
    error: Optional[str] = None


def ensure_tool_on_path(tool_path: str) -> None:
    """若配置了外部工具路径，将其所在目录加入 PATH。

    fpcalc 依赖 ffmpeg 解码，Windows 上 ffmpeg.exe 若与 fpcalc.exe 同目录，
    需保证该目录在 PATH 中（或通过此函数注入）。
    """
    if not tool_path:
        return
    p = Path(tool_path)
    bin_dir = p if p.is_dir() else p.parent
    if not bin_dir.exists():
        logger.warning(f"配置的工具路径不存在: {tool_path}")
        return
    current = os.environ.get("PATH", "")
    if str(bin_dir) not in current.split(os.pathsep):
        os.environ["PATH"] = str(bin_dir) + os.pathsep + current
        logger.debug(f"已将 {bin_dir} 加入 PATH")


def check_fpcalc(fpcalc_path: str) -> tuple[bool, str]:
    """前置检测 fpcalc 是否可用，返回 (是否可用, 版本或错误信息)。"""
    try:
        proc = subprocess.run(
            [fpcalc_path, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        return False, f"未找到 fpcalc 可执行文件: {fpcalc_path}"
    except subprocess.TimeoutExpired:
        return False, "fpcalc 响应超时"
    except OSError as e:
        return False, f"fpcalc 调用失败: {e}"

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
        return False, err

    version = (proc.stdout or proc.stderr or "").strip().splitlines()
    return True, version[0] if version else "unknown"


def _run_fpcalc(file_path: Path, fpcalc_path: str, duration: int) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """调用 fpcalc 提取指纹。

    返回 (fingerprint, duration, error)。
    """
    cmd = [fpcalc_path, "-json", "-length", str(duration), str(file_path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except FileNotFoundError:
        return None, None, f"fpcalc 未找到: {fpcalc_path}"
    except subprocess.TimeoutExpired:
        return None, None, "fpcalc 超时"
    except OSError as e:
        return None, None, f"fpcalc 调用失败: {e}"

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit code {proc.returncode}"
        return None, None, err

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, None, "fpcalc 输出解析失败"

    fingerprint = data.get("fingerprint")
    duration = data.get("duration")
    if not fingerprint:
        return None, None, "fpcalc 未返回指纹"

    return fingerprint, float(duration) if duration else None, None


def extract_fingerprints(
    files: list[AudioFile],
    config: FingerprintConfig,
    db: CacheDB,
) -> list[FingerprintResult]:
    """批量提取音频指纹，使用缓存加速。"""
    results: list[FingerprintResult] = []

    # 先查缓存，分离出需要实际提取的文件
    to_extract: list[AudioFile] = []
    cached: dict[str, FingerprintResult] = {}

    for f in files:
        cached_fp = db.get_fingerprint(f.path, f.size, f.mtime)
        if cached_fp:
            results.append(
                FingerprintResult(file=f, fingerprint=cached_fp[0], duration=cached_fp[1])
            )
        else:
            to_extract.append(f)

    if not to_extract:
        logger.info(f"全部命中缓存，共 {len(results)} 个文件")
        return results

    logger.info(f"缓存命中 {len(results)} 个，需提取指纹 {len(to_extract)} 个")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("提取指纹...", total=len(to_extract))

        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            futures = {
                executor.submit(
                    _run_fpcalc, f.path, config.fpcalc_path, config.duration
                ): f
                for f in to_extract
            }
            for future in as_completed(futures):
                f = futures[future]
                try:
                    fingerprint, duration, error = future.result()
                except Exception as e:
                    fingerprint, duration, error = None, None, str(e)

                if fingerprint:
                    db.set_fingerprint(f.path, f.size, f.mtime, fingerprint, duration or 0)
                    results.append(
                        FingerprintResult(file=f, fingerprint=fingerprint, duration=duration)
                    )
                else:
                    logger.warning(f"指纹提取失败 [{f.path.name}]: {error}")
                    results.append(FingerprintResult(file=f, fingerprint=None, duration=None, error=error))

                progress.advance(task)

    return results

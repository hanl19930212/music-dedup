"""音频指纹提取模块（基于 Chromaprint/fpcalc）。

fpcalc 默认输出的是**压缩格式**指纹：base64 编码的一串变长字节，
开头是 1 字节版本号 + 3 字节项数。它不能按 4 字节直接切开当 32 位整数用——
那样得到的是无意义的数值，两首不同的歌比较出来的相似度也会停在 0.55 上下。

真正能逐项比较的是 ``fpcalc -raw``：每个采样点一个 32 位整数，
本模块用它做去重比较，另外单独取一份压缩格式用于提交 AcoustID。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

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
    for candidate in (tool_path,):
        if not candidate:
            continue
        p = Path(candidate)
        # 纯命令名（"fpcalc"）交给系统 PATH 解析，不要把当前目录塞进 PATH
        if p.parent == Path("."):
            continue
        bin_dir = p if p.is_dir() else p.parent
        if not bin_dir.exists():
            logger.warning(f"配置的工具路径不存在: {candidate}")
            continue
        current = os.environ.get("PATH", "")
        if str(bin_dir) not in current.split(os.pathsep):
            os.environ["PATH"] = str(bin_dir) + os.pathsep + current
            logger.debug(f"已将 {bin_dir} 加入 PATH")


def ensure_tools_on_path(*tool_paths: str) -> None:
    """把多个外部工具所在目录加入 PATH。

    fpcalc 依赖 ffmpeg 解码；两者放在同一个目录时，需要把这个目录放进 PATH，
    否则 fpcalc 即使能被直接调用成功，也解不出音频。
    """
    for tool_path in tool_paths:
        ensure_tool_on_path(tool_path)


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


def _run_fpcalc(
    file_path: Path, fpcalc_path: str, duration: int, raw: bool = True
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """调用 fpcalc 提取指纹。

    raw=True 取 ``-raw`` 输出（每个采样点一个 32 位整数，用于两两比较）；
    raw=False 取默认的压缩格式（AcoustID 只认这个格式）。

    返回 (fingerprint, duration, error)。
    """
    cmd = [fpcalc_path, "-json", "-length", str(duration)]
    if raw:
        cmd.append("-raw")
    cmd.append(str(file_path))
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

    fingerprint_value = data.get("fingerprint")
    duration = data.get("duration")
    if not fingerprint_value:
        return None, None, "fpcalc 未返回指纹"
    if raw:
        if not isinstance(fingerprint_value, list):
            return None, None, "fpcalc 返回了非预期的指纹格式"
        fingerprint = encode_fingerprint([int(value) for value in fingerprint_value])
    else:
        if not isinstance(fingerprint_value, str):
            return None, None, "fpcalc 返回了非预期的指纹格式"
        fingerprint = fingerprint_value

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
                except (
                    OSError,
                    subprocess.SubprocessError,
                    json.JSONDecodeError,
                    struct.error,
                    ValueError,
                ) as e:
                    fingerprint, duration, error = None, None, str(e)
                except Exception:
                    # 编程错误不能伪装成「指纹提取失败」，否则会静默降级到文件名兜底
                    logger.exception(f"指纹提取出现未预期的程序错误 [{f.path.name}]")
                    raise

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


def extract_acoustid_fingerprint(
    file_path: Path, config: FingerprintConfig
) -> Optional[str]:
    """按需提取压缩格式指纹，供 AcoustID 查询使用。

    AcoustID 只接受 fpcalc 默认的压缩格式，而去重比较需要的是 -raw 的整数序列，
    两者没法互相还原。所以这里单独提供一个按需调用的入口：
    只有真正要查 AcoustID 时才多跑一次 fpcalc，平时扫描不受影响。
    """
    fingerprint, _, error = _run_fpcalc(
        file_path, config.fpcalc_path, config.duration, raw=False
    )
    if error:
        logger.warning(f"压缩格式指纹提取失败 [{file_path.name}]: {error}")
        return None
    return fingerprint


def encode_fingerprint(items: list[int]) -> str:
    """把 32 位整数序列编码成字符串（每个整数 4 字节小端 + base64）。"""
    if not items:
        return ""
    raw = struct.pack(f"<{len(items)}I", *items)
    return base64.b64encode(raw).decode("ascii")


def decode_fingerprint(fingerprint: str) -> list[int]:
    """把指纹字符串解码成 32 位整数序列。

    这里存的是 ``fpcalc -raw`` 的输出（每个采样点一个 32 位整数），
    不是 fpcalc 默认的压缩格式——压缩格式没法按项比较。
    """
    if not fingerprint:
        return []
    try:
        # validate=True：拒绝夹带非法字符的伪指纹，避免损坏/伪造值被当成有效指纹
        raw = base64.b64decode(fingerprint, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return []
    count = len(raw) // 4
    if count <= 0:
        return []
    return list(struct.unpack(f"<{count}I", raw[: count * 4]))


_MASK_CACHE: dict[int, int] = {}


def _mask(bits: int) -> int:
    """返回低 bits 位为 1 的掩码（带缓存，避免反复构造大整数）。"""
    mask = _MASK_CACHE.get(bits)
    if mask is None:
        mask = (1 << bits) - 1
        _MASK_CACHE[bits] = mask
    return mask


def _pack(items: list[int]) -> int:
    """把 32 位整数序列打包成一个大整数，便于用整型位运算做逐位比较。"""
    return int.from_bytes(struct.pack(f"<{len(items)}I", *items), "little")


def _aligned_score(
    packed_a: int,
    len_a: int,
    packed_b: int,
    len_b: int,
    offset: int,
    limit: int | None = None,
) -> tuple[float, int]:
    """按给定偏移对齐两段指纹，返回（逐位一致率, 重叠长度）。

    offset 为正表示第一段比第二段晚开始：比较 a[offset:] 与 b[:]。
    """
    start_a = max(0, offset)
    start_b = max(0, -offset)
    overlap = min(len_a - start_a, len_b - start_b)
    if limit is not None:
        overlap = min(overlap, limit)
    if overlap <= 0:
        return 0.0, 0
    diff = ((packed_a >> (32 * start_a)) ^ (packed_b >> (32 * start_b))) & _mask(32 * overlap)
    return 1.0 - diff.bit_count() / (32 * overlap), overlap


def fingerprint_similarity(
    first: str, second: str, max_offset: int = 40, threshold: float | None = None
) -> float:
    """两个指纹的相似度（0~1）。

    容忍最多 max_offset 个采样点的错位：同一录音的两次抓取起点可能差几秒
    （前面多一段静音、或不同版本的开头略有出入）。
    每个采样点约 0.13 秒，max_offset=40 大约能覆盖 5 秒。

    先用开头 64 个点粗筛，明显不像的偏移直接跳过，避免整段指纹的逐位比较；
    传入 threshold 时粗筛线取 threshold - 0.1。

    实测：同一录音的不同编码约 0.87~1.0，现场版与录音室版约 0.56，
    完全不同的歌约 0.55。
    """
    if not first or not second:
        return 0.0
    if first == second:
        # 相同字符串也要确认是可解码的有效指纹，避免空/损坏值被当成 100% 相同
        if not decode_fingerprint(first):
            return 0.0
        return 1.0
    left = decode_fingerprint(first)
    right = decode_fingerprint(second)
    if not left or not right:
        return 0.0

    packed_left = _pack(left)
    packed_right = _pack(right)
    minimum_overlap = max(16, int(min(len(left), len(right)) * 0.8))
    probe = 64
    cutoff = 0.5 if threshold is None else max(0.3, threshold - 0.1)
    best = 0.0
    for offset in range(-max_offset, max_offset + 1):
        rough, _ = _aligned_score(
            packed_left, len(left), packed_right, len(right), offset, limit=probe
        )
        if rough < cutoff:
            # 这一偏移下开头 64 个点就对不上，大概率不是同一录音
            continue
        score, overlap = _aligned_score(
            packed_left, len(left), packed_right, len(right), offset
        )
        if overlap < minimum_overlap:
            continue
        if score > best:
            best = score
            if best >= 0.999:
                break
    return best

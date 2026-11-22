"""最小 fpcalc 桩，用于离线端到端测试。

-version                   输出版本行，退出码 0
-raw -json -length N <file>  输出 JSON：duration 取自 WAV 头，
                            fingerprint 是由音频内容派生的 32 位整数数组
-json <file>              输出压缩格式指纹（base64 字符串），供 AcoustID 路径使用

指纹由音频内容派生：内容完全相同的文件指纹逐位相同；
内容有细微差异时，只有对应的块会变，得到的指纹相近但不相同。
这样既能触发"指纹完全相同"，也能触发"指纹相似"两条分组路径。
"""

from __future__ import annotations

import base64
import json
import sys
import wave

BLOCK = 256
# 每个块里取样的位置：改动其中任何一个，相应的指纹项就会变
PICK = (0, 85, 170, 255)


def _version() -> int:
    print("fpcalc version 1.6.2 (stub for tests)")
    return 0


def _samples(path: str) -> list[int]:
    """读取 16-bit PCM WAV 的全部采样值（多声道取平均）。"""
    try:
        with wave.open(path, "rb") as handle:
            if handle.getsampwidth() != 2:
                return []
            channels = max(1, handle.getnchannels())
            raw = handle.readframes(handle.getnframes())
    except Exception:
        return []

    values = []
    for index in range(0, len(raw) - 1, 2):
        sample = int.from_bytes(raw[index : index + 2], "little", signed=True)
        values.append(sample)
    if channels > 1:
        values = [
            sum(values[i : i + channels]) // channels
            for i in range(0, len(values) - channels + 1, channels)
        ]
    return values


def _fingerprint_items(path: str) -> list[int]:
    """按块能量派生出 32 位整数序列，格式与 fpcalc -raw 对齐。"""
    samples = _samples(path)
    if not samples:
        return []

    # 归一化基准取各块峰值的中位数：个别采样点的突变不会带偏整体标度，
    # 这样"细微差异"只会影响个别指纹项，而不是全部
    block_peaks = []
    for start in range(0, len(samples) - BLOCK + 1, BLOCK):
        chunk = samples[start : start + BLOCK]
        block_peaks.append(max((abs(value) for value in chunk), default=0))
    block_peaks.sort()
    peak = block_peaks[len(block_peaks) // 2] or 1
    quantized: list[int] = []
    for start in range(0, len(samples) - BLOCK + 1, BLOCK):
        for position in PICK:
            index = start + position
            if index >= len(samples):
                break
            value = samples[index] / peak * 63
            quantized.append(max(0, min(63, int(value + 0.5))))

    items: list[int] = []
    for start in range(0, len(quantized), 4):
        group = quantized[start : start + 4]
        group += [0] * (4 - len(group))
        item = 0
        for offset, value in enumerate(group):
            item |= (0x40 | value) << (24 - 8 * offset)
        items.append(item)
    return items


def _duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as handle:
            return round(handle.getnframes() / handle.getframerate(), 3)
    except Exception:
        return 0.0


def main(argv: list[str]) -> int:
    if "-version" in argv:
        return _version()

    args = argv[1:]
    path = args[-1]
    length = 120
    if "-length" in args:
        length = int(args[args.index("-length") + 1])

    items = _fingerprint_items(path)
    if "-raw" in args:
        fingerprint: object = items
    else:
        # 压缩格式：只是给 AcoustID 路径一个能用的字符串
        fingerprint = base64.b64encode(
            b"".join(item.to_bytes(4, "little") for item in items)
        ).decode("ascii")

    print(json.dumps({
        "duration": _duration(path),
        "fingerprint": fingerprint,
        "fingerprint_duration": length,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

